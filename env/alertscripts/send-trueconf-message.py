#!/usr/bin/env python3
import sys
import json
import urllib.request
import urllib.error

API_URL = "http://trueconf-api:8081/send"

def send_alert(sendto, message):
    """Отправка алерта в API"""
    payload = json.dumps({"sendto": sendto, "message": message}).encode('utf-8')
    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode('utf-8'))
            print(f"Task queued: {result.get('task_id', 'unknown')}")
            return True
    except urllib.error.URLError as e:
        print(f"Connection error: {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return False

def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <sendto> <message>", file=sys.stderr)
        sys.exit(1)
    
    sendto = sys.argv[1]
    message = sys.argv[2]
    
    if send_alert(sendto, message):
        sys.exit(0)
    else:
        sys.exit(3)

if __name__ == "__main__":
    main()
