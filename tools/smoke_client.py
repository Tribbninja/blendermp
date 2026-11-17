"""Connect a headless client to a running host and send a chat line.

Usage:
    python tools/smoke_client.py <room-code> [message]

Reads nothing from the addon; it exercises the same SessionService the addon
uses, over the real TLS listener.
"""

from __future__ import annotations

import json
import queue
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon" / "blender_multiplayer"))

import session  # noqa: E402


def wait_for(predicate, timeout=12.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: smoke_client.py <room-code> [message]")
        return 2
    code = sys.argv[1]
    message = sys.argv[2] if len(sys.argv) > 2 else "hello from the smoke client"
    inbox: queue.Queue = queue.Queue()
    client = session.SessionService(inbox, host=False, code=code, name="smoke", color=(0.9, 0.5, 0.2, 1.0))
    client.start()
    try:
        if not wait_for(lambda: client.state == "connected"):
            print(json.dumps({"ok": False, "state": client.state, "error": client.failure_reason}))
            return 1
        client.send_chat(message)
        time.sleep(1.5)
        events = []
        while not inbox.empty():
            events.append(inbox.get_nowait())
        print(json.dumps({"ok": True, "events": events[:12], "stats": client.stats()}, default=str))
        return 0
    finally:
        client.stop()


if __name__ == "__main__":
    raise SystemExit(main())
