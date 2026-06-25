#!/usr/bin/env python3
import sys
import json
import urllib.request
import urllib.error

API_URL = "http://trueconf-api:8081/send"
CHANNEL_API_URL = "http://trueconf-api:8081/send_channel"

def is_channel_id(value):
    """Проверяет, является ли значение ID канала"""
    return len(value) == 40 and all(c in '0123456789abcdef' for c in value.lower())

def send_alert(sendto, message):
    # Если sendto похож на ID канала
    if is_channel_id(sendto):
        url = CHANNEL_API_URL
        payload = {"channel_id": sendto, "message": message}
    else:
        url = API_URL
        payload = {"sendto": sendto, "message": message}

    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'}, method='POST')

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode('utf-8'))
            print(f"Task queued: {result.get('task_id', 'unknown')}")
            return True
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return False

def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <sendto> <message>", file=sys.stderr)
        sys.exit(1)

    if send_alert(sys.argv[1], sys.argv[2]):
        sys.exit(0)
    else:
        sys.exit(3)

if __name__ == "__main__":
    main()
