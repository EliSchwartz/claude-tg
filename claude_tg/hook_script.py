"""Tiny hook stub installed as the PreToolUse command.

Usage: claude-tg-hook <socket-path> <endpoint>

Reads hook input JSON on stdin, forwards to wrapper over unix socket,
writes response JSON on stdout. Defaults to deny if the socket is unreachable.
"""

from __future__ import annotations

import json
import socket
import sys


SOCKET_TIMEOUT_SEC = 2.0


def main() -> None:
    if len(sys.argv) != 3:
        print(json.dumps({"decision": "deny", "reason": "bad hook stub args"}))
        return
    socket_path, endpoint = sys.argv[1], sys.argv[2]
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    request = json.dumps({"endpoint": endpoint, "payload": payload}) + "\n"

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(SOCKET_TIMEOUT_SEC)
    try:
        s.connect(socket_path)
        s.sendall(request.encode())
        s.shutdown(socket.SHUT_WR)
        buf = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        response = buf.decode().splitlines()[0] if buf else ""
        if not response:
            print(json.dumps({"decision": "deny", "reason": "empty hook response"}))
            return
        print(response)
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError):
        print(json.dumps({"decision": "deny", "reason": "wrapper unreachable"}))
    finally:
        s.close()


if __name__ == "__main__":
    main()
