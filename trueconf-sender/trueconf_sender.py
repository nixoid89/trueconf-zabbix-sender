#!/usr/bin/env python3
"""
TrueConf Zabbix Message Sender

Sends TrueConf chat messages on behalf of the Zabbix monitoring system.
Recipient email addresses are automatically converted to TrueConf IDs using
the domain mapping configured in config.toml.

SERVICE MODE — recommended for production use (persistent connection + queue):
    python trueconf_sender.py --service

    When the service is running, Zabbix alertscripts write message tasks to the
    queue/ directory alongside this script. The service picks them up within
    ~1 second and delivers them without reconnecting. This mode is the right
    choice whenever message bursts are expected.

QUEUE MODE — called by the Zabbix alertscript when the service is installed:
    python trueconf_sender.py --queue "user1@example.com user2@example.com" "Alert"

    Converts emails to TrueConf IDs, writes a task file to queue/, and exits
    immediately. The running service delivers the message asynchronously.

DIRECT MODE — one-shot send without a running service (fallback):
    python trueconf_sender.py "user1@example.com user2@example.com" "Alert message"

    Connects, sends, and disconnects. Suitable for infrequent alerts (<1/min).
    send-trueconf-message.sh falls back to this mode when queue/ does not exist.

The configuration file path defaults to config.toml in the same directory as
this script and can be overridden via the TRUECONF_CONFIG environment variable.
"""

__version__ = "1.0.1"

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Python 3.11+ ships tomllib in the standard library; Python 3.10 needs tomli.
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib          # type: ignore[no-redef]
    except ImportError:
        sys.exit("Error: install 'tomli' for Python 3.10 support:  pip install tomli")
import httpx

from trueconf import Bot, Dispatcher, Router, Message
from trueconf.methods.create_p2p_chat import CreateP2PChat
from trueconf.methods.send_message import SendMessage
from trueconf.exceptions import ApiErrorException


# ─── Constants ────────────────────────────────────────────────────────────────

# config.toml is expected next to this script unless overridden via env var
_DEFAULT_CONFIG = Path(__file__).parent / "config.toml"

# Queue directory (sibling of this script).
# Created by install.sh when the service is set up. When it exists,
# send-trueconf-message.sh uses --queue mode instead of direct mode.
_QUEUE_DIR = Path(__file__).parent / "queue"

# Process exit codes (mirrors common Unix conventions)
EXIT_OK           = 0
EXIT_USAGE        = 1
EXIT_CONFIG       = 2
EXIT_SEND_FAILED  = 3


# ─── Configuration helpers ────────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    """Load and return the TOML configuration file."""
    if not path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def setup_logging(config: dict) -> None:
    """Configure the root logger from the [sender] section of the config."""
    sender   = config.get("sender", {})
    level    = getattr(logging, sender.get("log_level", "INFO").upper(), logging.INFO)
    log_file = sender.get("log_file", "")

    handler: logging.Handler = (
        logging.FileHandler(log_file) if log_file
        else logging.StreamHandler(sys.stderr)
    )
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[handler],
        force=True,
    )


# ─── Email → TrueConf ID conversion ──────────────────────────────────────────

def email_to_trueconf_id(email: str, config: dict) -> str:
    """
    Convert a plain e-mail address to a TrueConf ID using the domain mapping
    specified in [email_mapping] of the config file.

    Example (default mapping):
        alice@example.com  ->  alice@tconf.example.com

    If the address domain does not match from_domain, it is returned unchanged
    (it may already be a TrueConf ID such as user@tconf.example.com).
    """
    email = email.strip()
    if not email or "@" not in email:
        raise ValueError(f"Not a valid e-mail address: {email!r}")

    mapping     = config.get("email_mapping", {})
    from_domain = mapping.get("from_domain", "")
    to_domain   = mapping.get("to_domain", "")

    username, domain = email.split("@", 1)

    if from_domain and to_domain and domain.lower() == from_domain.lower():
        return f"{username}@{to_domain}"

    # No mapping applies — pass through as-is
    return email


# ─── Queue support ────────────────────────────────────────────────────────────

def write_to_queue(trueconf_ids: list[str], message: str, parse_mode: str) -> bool:
    """
    Atomically write a message task to the queue directory.
    The running service will pick it up and deliver it within ~1 second.

    Uses a write-to-tmp-then-rename pattern so the service never sees a
    partially written file.

    Returns True on success, False if the queue directory is not accessible.
    """
    if not _QUEUE_DIR.is_dir():
        logging.error("Queue directory not found: %s", _QUEUE_DIR)
        return False

    task = {
        "trueconf_ids": trueconf_ids,
        "message":      message,
        "parse_mode":   parse_mode,
        "created_at":   time.time(),
    }

    # Filename includes microseconds so simultaneous writes get unique names
    filename   = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".json"
    tmp_path   = _QUEUE_DIR / (filename + ".tmp")
    final_path = _QUEUE_DIR / filename

    tmp_path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.rename(final_path)  # atomic on Linux (same filesystem)

    logging.info("Queued for %d recipient(s): %s", len(trueconf_ids), filename)
    return True


async def _send_to_user(bot: Bot, user_id: str, message: str, parse_mode: str) -> None:
    """
    Open (or reuse) a P2P chat with user_id and deliver one message.
    createP2PChat is idempotent — it returns the existing chat if one exists.
    Raises on any delivery failure so the caller can handle it.
    """
    p2p = await bot(CreateP2PChat(user_id=user_id))
    await bot(SendMessage(chat_id=p2p.chat_id, text=message, parse_mode=parse_mode))
    logging.info("  Queue: delivered -> %s", user_id)


async def _watch_and_send(bot: Bot, parse_mode: str) -> None:
    """
    Watch the queue directory and deliver pending message tasks.
    Runs indefinitely (until cancelled by the service loop).

    Processing order: filename sort (= timestamp order).
    Per-task recipients are sent in parallel via asyncio.gather.
    Failed tasks are renamed to ERROR_<name> for manual inspection.
    """
    logging.info("Queue watcher started (polling %s)", _QUEUE_DIR)

    while True:
        for task_file in sorted(_QUEUE_DIR.glob("*.json")):
            try:
                task = json.loads(task_file.read_text(encoding="utf-8"))
                message = task.get("message", "")
                pm = task.get("parse_mode", parse_mode)

                # === НОВАЯ ЛОГИКА: Проверяем, есть ли channel_id ===
                channel_id = task.get("channel_id")

                if channel_id and message:
                    # Отправка в канал
                    try:
                        await bot(SendMessage(
                            chat_id=channel_id,
                            text=message,
                            parse_mode=pm
                        ))
                        logging.info(f"  Channel message sent -> {channel_id}")
                        task_file.unlink()  # Удаляем файл после успешной отправки
                    except Exception as e:
                        logging.error(f"  Failed to send to channel {channel_id}: {e}")
                        task_file.rename(_QUEUE_DIR / f"ERROR_{task_file.name}")
                    continue  # Переходим к следующему файлу

                # === СУЩЕСТВУЮЩАЯ ЛОГИКА ДЛЯ ЛИЧНЫХ СООБЩЕНИЙ ===
                recipients = task.get("trueconf_ids", [])

                if not recipients or not message:
                    logging.warning("Queue: malformed task, skipping: %s", task_file.name)
                    task_file.rename(_QUEUE_DIR / f"ERROR_{task_file.name}")
                    continue

                logging.info(
                    "Queue: processing %s (%d recipient(s))",
                    task_file.name, len(recipients),
                )

                # Send to all recipients concurrently within the same task
                results = await asyncio.gather(
                    *[_send_to_user(bot, uid, message, pm) for uid in recipients],
                    return_exceptions=True,
                )

                errors = [r for r in results if isinstance(r, Exception)]
                if errors:
                    for err in errors:
                        logging.error("Queue: delivery error: %s", err)
                    task_file.rename(_QUEUE_DIR / f"ERROR_{task_file.name}")
                else:
                    task_file.unlink()

            except asyncio.CancelledError:
                raise  # Propagate — the service loop is shutting down

            except Exception as exc:
                logging.error("Queue: failed to process %s: %s", task_file.name, exc)
                with contextlib.suppress(Exception):
                    task_file.rename(_QUEUE_DIR / f"ERROR_{task_file.name}")

        await asyncio.sleep(1.0)  # 1-second poll interval


# ─── Bot factory ──────────────────────────────────────────────────────────────

def _get_auth_token(
    server:     str,
    username:   str,
    password:   str,
    port:       int,
    verify_ssl: bool,
    timeout:    float = 15.0,
) -> str:
    """
    Request a TrueConf chatbot OAuth token.

    This intentionally lives in the project instead of importing
    trueconf.utils.get_auth_token: that helper is an internal implementation
    detail of python-trueconf-bot and is not exported consistently across
    package versions. Keeping the token request here makes installation
    predictable for users while still passing the custom HTTPS port correctly.

    httpx is already installed as a public dependency of python-trueconf-bot,
    and it matches the HTTP stack the library itself uses for token requests.
    """
    url = f"https://{server}:{port}/bridge/api/client/v1/oauth/token"
    payload = {
        "client_id":  "chat_bot",
        "grant_type": "password",
        "username":   str(username),
        "password":   str(password),
    }

    try:
        with httpx.Client(timeout=timeout, verify=verify_ssl) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"Token request failed: HTTP {exc.response.status_code}: {exc.response.text}"
        ) from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"Token request failed: {exc}") from exc

    token = data.get("access_token")
    if not token:
        raise RuntimeError("Token response did not contain access_token")
    return token

def _make_bot(
    server:     str,
    username:   str,
    password:   str,
    port:       int,
    verify_ssl: bool,
    dispatcher: Dispatcher | None = None,
) -> Bot:
    """
    Obtain an OAuth token from the TrueConf Server and return a Bot instance.

    Using a local token request (instead of Bot.from_credentials) lets us pass
    a non-standard HTTPS port for both the token request and the subsequent
    WebSocket connection.  Bot.from_credentials always uses port 443.

    Note: the library's Bot.__init__ still hard-codes self.port = 443 when
    https=True, which affects file-upload and domain-name REST calls.  Those
    code paths are not used by this sender, so web_port alone is sufficient.
    """
    token = _get_auth_token(
        server=server,
        username=username,
        password=password,
        port=port,
        verify_ssl=verify_ssl,
    )
    return Bot(
        server,
        token,
        web_port=port,
        verify_ssl=verify_ssl,
        dispatcher=dispatcher,
    )


# ─── Direct send: single attempt ──────────────────────────────────────────────

async def _try_send_once(
    server:          str,
    username:        str,
    password:        str,
    port:            int,
    verify_ssl:      bool,
    trueconf_ids:    list[str],
    message:         str,
    parse_mode:      str,
    connect_timeout: float,
) -> bool | None:
    """
    Authenticate, deliver the message to every recipient, then disconnect.

    Returns:
        True   – all messages delivered successfully
        False  – delivery attempted but some messages failed (no retry)
        None   – connection/authentication failed before any delivery (retry)
    """
    # Obtain a JWT token and create the bot instance (synchronous HTTP call)
    try:
        bot = _make_bot(
            server=server,
            username=username,
            password=password,
            port=port,
            verify_ssl=verify_ssl,
        )
    except Exception as exc:
        logging.error("Authentication failed: %s", exc)
        return None  # Will be retried by the caller

    failure_count      = 0
    delivery_attempted = False

    # Start the persistent WebSocket loop in the background
    run_task = asyncio.create_task(bot.run(), name="bot_run")

    try:
        # Block until the bot successfully authenticates on the server
        await asyncio.wait_for(bot.authorized_event.wait(), timeout=connect_timeout)

        delivery_attempted = True
        for user_id in trueconf_ids:
            try:
                # createP2PChat is idempotent: returns the existing chat if one exists
                p2p = await bot(CreateP2PChat(user_id=user_id))
                await bot(SendMessage(
                    chat_id=p2p.chat_id,
                    text=message,
                    parse_mode=parse_mode,
                ))
                logging.info("  delivered -> %s", user_id)
            except ApiErrorException as exc:
                logging.error("  failed    -> %s  (API %s: %s)", user_id, exc.code, exc)
                failure_count += 1
            except Exception as exc:
                logging.error("  failed    -> %s  (%s)", user_id, exc)
                failure_count += 1

        return failure_count == 0

    except asyncio.TimeoutError:
        logging.warning(
            "Connection/authorization timed out after %.0f s", connect_timeout
        )
        return None  # Caller will retry

    except ApiErrorException as exc:
        logging.warning("API error %s during connection: %s", exc.code, exc)
        return None  # Caller will retry

    except Exception as exc:
        logging.error("Unexpected error: %s", exc)
        return None  # Caller will retry

    finally:
        # Ask the bot to close the WebSocket gracefully
        with contextlib.suppress(Exception):
            await bot.shutdown()

        # Wait for run_task to finish; force-cancel after 3 s if it lingers.
        # Always retrieve the task result/exception afterwards to prevent
        # asyncio's "Task exception was never retrieved" warning: this warning
        # fires when bot.run() raises internally (e.g. websockets open_timeout)
        # before our own connect_timeout expires, leaving run_task done-but-
        # unread while we continue waiting for authorized_event.
        if not run_task.done():
            try:
                await asyncio.wait_for(run_task, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                # wait_for already cancelled run_task on TimeoutError;
                # other exceptions are acceptable here (e.g. connection reset)
                pass
        # Retrieve stored exception (if any) so asyncio does not log a warning
        with contextlib.suppress(Exception):
            run_task.result()


# ─── Direct send: with retry ──────────────────────────────────────────────────

async def direct_send(
    server:          str,
    username:        str,
    password:        str,
    port:            int,
    verify_ssl:      bool,
    trueconf_ids:    list[str],
    message:         str,
    parse_mode:      str   = "text",
    connect_timeout: float = 30.0,
    max_retries:     int   = 5,
    retry_delay:     float = 15.0,
) -> bool:
    """
    Connect to TrueConf, deliver the message to all recipients, and disconnect.
    Retries on connection failures up to max_retries times.

    Returns True only if every message was delivered successfully.
    """
    for attempt in range(1, max_retries + 1):
        logging.info(
            "Sending to %d recipient(s) (attempt %d/%d)",
            len(trueconf_ids), attempt, max_retries,
        )

        result = await _try_send_once(
            server=server,
            username=username,
            password=password,
            port=port,
            verify_ssl=verify_ssl,
            trueconf_ids=trueconf_ids,
            message=message,
            parse_mode=parse_mode,
            connect_timeout=connect_timeout,
        )

        if result is not None:
            # Delivery was attempted: True = all ok, False = partial failure
            return result

        # result is None: connection failed before any delivery — try again
        if attempt < max_retries:
            logging.info("Will retry in %.0f s...", retry_delay)
            await asyncio.sleep(retry_delay)

    logging.error("All %d attempts exhausted", max_retries)
    return False


# ─── Service mode (primary for production) ───────────────────────────────────

async def service_mode(config: dict) -> None:
    """
    Run as a long-lived chatbot service connected to TrueConf Server.

    The service maintains a single persistent WebSocket connection and
    concurrently runs a queue watcher that delivers messages written to the
    queue/ directory by the Zabbix alertscript. Recipients within the same
    alert are delivered in parallel; alerts are processed in arrival order.

    On token expiry or network interruption the service reconnects automatically,
    and unprocessed queue files are picked up on the next connection.
    Failed deliveries are renamed to ERROR_<name> for manual review.
    """
    server_cfg = config.get("server", {})
    creds      = config.get("credentials", {})
    sender_cfg = config.get("sender", {})

    parse_mode      = sender_cfg.get("parse_mode", "text")
    retry_delay     = float(sender_cfg.get("retry_delay", 15.0))
    connect_timeout = float(sender_cfg.get("connect_timeout", 30.0))
    port            = int(server_cfg.get("port", 443))

    # Ensure the queue directory exists (install.sh creates it, but be safe)
    _QUEUE_DIR.mkdir(exist_ok=True)

    router = Router()
    dp     = Dispatcher()
    dp.include_router(router)

    @router.message()
    async def on_message(msg: Message) -> None:
        """Log any message received by the bot. Extend here for interactive replies."""
        logging.info(
            "Message received from %s: %s",
            msg.from_user.id,
            msg.text or "[non-text content]",
        )

    logging.info(
        "TrueConf bot service starting on %s:%d (queue: %s)",
        server_cfg["host"], port, _QUEUE_DIR,
    )

    # Reconnect loop — runs until a clean shutdown (SIGTERM / KeyboardInterrupt)
    attempt = 0
    while True:
        attempt += 1
        run_task   = None
        queue_task = None

        try:
            bot = _make_bot(
                server=server_cfg["host"],
                username=creds["login"],
                password=creds["password"],
                port=port,
                verify_ssl=server_cfg.get("verify_ssl", True),
                dispatcher=dp,
            )

            # Start the WebSocket loop in the background
            run_task = asyncio.create_task(bot.run(), name="bot_run")

            # Wait until the bot is authorized before starting the queue watcher
            await asyncio.wait_for(bot.authorized_event.wait(), timeout=connect_timeout)
            logging.info("Connected and authorized (attempt %d)", attempt)

            # Run queue watcher concurrently with the bot connection
            queue_task = asyncio.create_task(
                _watch_and_send(bot, parse_mode), name="queue_watcher"
            )

            # Block until the bot disconnects (run_task finishes or raises)
            await run_task
            logging.info("Bot disconnected cleanly — stopping service")
            break  # SIGTERM or clean shutdown requested

        except asyncio.CancelledError:
            # Service process is being shut down (e.g. systemd stop)
            logging.info("Service shutdown requested")
            raise

        except asyncio.TimeoutError:
            logging.warning(
                "Authorization timed out on attempt %d; reconnecting in %.0f s...",
                attempt, retry_delay,
            )

        except ApiErrorException as exc:
            if exc.code == 203:
                logging.warning(
                    "Token expired on attempt %d; reconnecting in %.0f s...",
                    attempt, retry_delay,
                )
            else:
                logging.error(
                    "API error %s on attempt %d: %s; reconnecting in %.0f s...",
                    exc.code, attempt, exc, retry_delay,
                )

        except Exception as exc:
            logging.error(
                "Connection lost on attempt %d: %s; reconnecting in %.0f s...",
                attempt, exc, retry_delay,
            )

        finally:
            # Cancel queue watcher and bot task if still running
            for task in (queue_task, run_task):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task

        await asyncio.sleep(retry_delay)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> int:
    """Parse CLI arguments, load configuration, and dispatch to the selected mode."""
    # ── Argument parsing ──────────────────────────────────────────────────────
    if len(sys.argv) == 2 and sys.argv[1] in ("--service", "service"):
        mode = "service"
    elif len(sys.argv) == 4 and sys.argv[1] == "--queue":
        # Queue mode: write task file and exit (service delivers asynchronously)
        mode        = "queue"
        emails_arg  = sys.argv[2]
        message_arg = sys.argv[3]
    elif len(sys.argv) == 3:
        # Direct mode: connect, send, disconnect (fallback when service not installed)
        mode        = "send"
        emails_arg  = sys.argv[1]
        message_arg = sys.argv[2]
    else:
        print(__doc__)
        return EXIT_USAGE

    # ── Configuration loading ─────────────────────────────────────────────────
    config_path = Path(os.environ.get("TRUECONF_CONFIG", str(_DEFAULT_CONFIG)))
    try:
        config = load_config(config_path)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    setup_logging(config)

    server_cfg = config.get("server", {})
    creds      = config.get("credentials", {})
    sender_cfg = config.get("sender", {})

    # Fail early on obviously missing required keys
    if not server_cfg.get("host"):
        logging.error("Missing required config key: server.host")
        return EXIT_CONFIG
    if not creds.get("login") or not creds.get("password"):
        logging.error("Missing required config keys: credentials.login / credentials.password")
        return EXIT_CONFIG

    # ── Service mode ──────────────────────────────────────────────────────────
    if mode == "service":
        logging.info("Starting in service mode (press Ctrl+C to stop)")
        try:
            asyncio.run(service_mode(config))
        except KeyboardInterrupt:
            logging.info("Service stopped by user")
        except Exception as exc:
            logging.error("Service exited with error: %s", exc)
            return EXIT_SEND_FAILED
        return EXIT_OK

    # ── Shared: parse recipient list (used by both queue and direct modes) ─────
    emails = [e.strip() for e in emails_arg.split() if e.strip()]
    if not emails:
        logging.error("No recipient addresses provided")
        return EXIT_USAGE

    trueconf_ids: list[str] = []
    for email in emails:
        try:
            tid = email_to_trueconf_id(email, config)
            if tid != email:
                logging.debug("Mapped: %s -> %s", email, tid)
            trueconf_ids.append(tid)
        except ValueError as exc:
            logging.error("Invalid address: %s", exc)
            return EXIT_USAGE

    # ── Queue mode (service is running, write task file and exit) ─────────────
    if mode == "queue":
        success = write_to_queue(
            trueconf_ids=trueconf_ids,
            message=message_arg,
            parse_mode=sender_cfg.get("parse_mode", "text"),
        )
        return EXIT_OK if success else EXIT_SEND_FAILED

    # ── Direct send mode (fallback: connect, send, disconnect) ────────────────
    success = asyncio.run(direct_send(
        server          = server_cfg["host"],
        username        = creds["login"],
        password        = creds["password"],
        port            = int(server_cfg.get("port", 443)),
        verify_ssl      = server_cfg.get("verify_ssl", True),
        trueconf_ids    = trueconf_ids,
        message         = message_arg,
        parse_mode      = sender_cfg.get("parse_mode", "text"),
        connect_timeout = float(sender_cfg.get("connect_timeout", 30)),
        max_retries     = sender_cfg.get("max_retries", 5),
        retry_delay     = float(sender_cfg.get("retry_delay", 15)),
    ))

    if success:
        logging.info("All messages delivered successfully")
        return EXIT_OK

    logging.error("One or more messages failed to deliver")
    return EXIT_SEND_FAILED


if __name__ == "__main__":
    sys.exit(main())
