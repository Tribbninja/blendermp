"""Print every Blendermp bridge message to the console.

Usage:
    python console_receiver.py [host] [port]

This is the smallest possible receiver: it proves the bridge is running and
shows the exact JSON the engine integrations consume.
"""

import json
import socket
import sys

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 7103


def main() -> None:
    with socket.create_connection((HOST, PORT), timeout=5) as sock:
        stream = sock.makefile("r", encoding="utf-8")
        for line in stream:
            line = line.strip()
            if not line:
                continue
            message = json.loads(line)
            kind = message.get("type", "?")
            if kind == "object":
                print(
                    f"object {message.get('name')} "
                    f"{message.get('position')} visible={message.get('visible')}"
                )
            elif kind == "scene":
                print(
                    f"scene: {len(message.get('objects', []))} objects, "
                    f"{len(message.get('materials', []))} materials"
                )
            elif kind == "remove":
                print(f"remove {message.get('uid')}")
            else:
                print(json.dumps(message)[:400])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
