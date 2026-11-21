import queue
import time
import types

from core import protocol
import session as session_mod


def test_lan_ip_detection():
    assert session_mod._is_lan_ip("192.168.1.20")
    assert session_mod._is_lan_ip("10.0.0.5")
    assert session_mod._is_lan_ip("172.16.4.9")
    assert not session_mod._is_lan_ip("127.0.0.1")
    assert not session_mod._is_lan_ip("8.8.8.8")
    assert not session_mod._is_lan_ip("not-an-ip")


def test_lan_peer_bypasses_interest_rate_limits():
    service = session_mod.SessionService(
        queue.Queue(), host=True, code="ABCDE-FGHJK-MNPQR", name="host", color=(1, 1, 1, 1)
    )
    uid = "11111111-1111-1111-1111-111111111111"
    frame = protocol.Frame(
        type=protocol.MessageType.TRANSFORM,
        payload=session_mod.encode_transform(uid, [1.0] * 16),
    )
    now = time.monotonic()
    lan_peer = types.SimpleNamespace(
        lan=True,
        authenticated=True,
        subscriptions={},
        priorities={uid: 2},
        quality=2,
        last_sent={},
    )
    assert service._peer_accepts(lan_peer, frame, now)
    # A LAN peer gets every frame, even with the worst priority and quality.
    assert service._peer_accepts(lan_peer, frame, now)

    relay_peer = types.SimpleNamespace(
        lan=False,
        authenticated=True,
        subscriptions={},
        priorities={uid: 2},
        quality=2,
        last_sent={},
    )
    assert service._peer_accepts(relay_peer, frame, now)
    assert not service._peer_accepts(relay_peer, frame, now)

    presence = protocol.Frame(type=protocol.MessageType.PRESENCE, payload=b"{}")
    relay_peer.last_sent = {}
    assert service._peer_accepts(relay_peer, presence, now)
    assert not service._peer_accepts(relay_peer, presence, now)
    assert service._peer_accepts(lan_peer, presence, now)
    assert service._peer_accepts(lan_peer, presence, now)


def test_has_lan_peer():
    service = session_mod.SessionService(
        queue.Queue(), host=True, code="ABCDE-FGHJK-MNPQR", name="host", color=(1, 1, 1, 1)
    )
    assert not service.has_lan_peer()
    service._lan_peer_count = 1
    assert service.has_lan_peer()
    service._lan_peer_count = 0
    assert not service.has_lan_peer()
