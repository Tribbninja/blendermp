import asyncio
import json

import pytest

import control
from core import tls
from core.roomcode import encode_room


@pytest.fixture()
def appdir(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path


def write_session(appdir, body):
    path = control.session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")


def test_valid_host_request(appdir):
    code = encode_room("127.0.0.1", 51999)
    write_session(appdir, {"action": "host", "nonce": "n" * 16, "name": "ada", "port": 51999, "code": code, "pin": "1234"})
    request = control.read_session()
    assert request["action"] == "host"
    assert request["code"].replace("-", "") == code.replace("-", "")
    assert request["pin"] == "1234"


def test_invalid_requests_are_ignored(appdir):
    cases = [
        {"action": "run", "nonce": "x" * 16},
        {"action": "host", "nonce": "short"},
        {"action": "join", "nonce": "x" * 16, "code": "NOTACODE"},
        {"action": "join", "nonce": "x" * 16},
        {"action": "host", "nonce": "x" * 16, "port": 99999},
        {"action": "host", "nonce": "x" * 16, "name": "bad\nname"},
    ]
    for body in cases:
        write_session(appdir, body)
        assert control.read_session() is None, body


def test_relay_request(appdir):
    write_session(
        appdir,
        {
            "action": "host",
            "nonce": "r" * 16,
            "name": "ada",
            "port": 51999,
            "use_relay": True,
            "relay": "203.0.113.5:7100",
        },
    )
    request = control.read_session()
    assert request["use_relay"] is True
    assert request["relay"] == "203.0.113.5:7100"

    for bad in ("not-an-address", "203.0.113.5:99", "host.example:7100", "203.0.113.5"):
        write_session(
            appdir,
            {"action": "host", "nonce": "r" * 16, "use_relay": True, "relay": bad},
        )
        assert control.read_session() is None, bad


def test_oversized_control_file_rejected(appdir):
    write_session(appdir, {"action": "host", "nonce": "x" * 16, "pad": "z" * 10000})
    assert control.read_session() is None


def test_worker_relay_url_request(appdir):
    write_session(
        appdir,
        {
            "action": "join",
            "nonce": "w" * 16,
            "code": encode_room("127.0.0.1", 51999),
            "relay_url": "wss://blendermp-hub.example.workers.dev",
        },
    )
    request = control.read_session()
    assert request["relay_url"] == "wss://blendermp-hub.example.workers.dev"

    for bad in ("https://example.com", "blendermp-hub.example", "wss://bad\nurl"):
        write_session(
            appdir,
            {
                "action": "join",
                "nonce": "w" * 16,
                "code": encode_room("127.0.0.1", 51999),
                "relay_url": bad,
            },
        )
        assert control.read_session() is None, bad


def test_ack_nonce_roundtrip(appdir):
    assert control.read_ack() == ""
    control.write_ack("a" * 16)
    assert control.read_ack() == "a" * 16
    control.write_ack("short")
    assert control.read_ack() == "a" * 16


def test_status_is_bounded_and_atomic(appdir):
    control.write_status(
        {
            "state": "hosting",
            "code": "ABCDE-FGHJK-MNPQR",
            "users": [{"name": "a" * 500, "color": [0.1, 0.2, 0.3, 1.0], "ping_ms": 10, "host": True}],
            "bytes_in": 10**30,
            "error_code": "PROTOCOL",
            "scene": {"objects": 12, "meshes": 8, "vertices": 5000, "materials": 4, "images": 2, "collections": 3},
            "broadcast_url": "http://127.0.0.1:7102/?token=abc",
            "bridge_url": "tcp://127.0.0.1:7103",
            "bridge_clients": 2,
            "recording": "session.jsonl",
            "perf": {
                "violations": ["tick"],
                "budgets": {"tick": {"max_ms": 8.0, "p95_ms": 9.5, "ok": False}},
            },
            "unknown": "ignored",
        }
    )
    body = json.loads(control.status_path().read_text(encoding="utf-8"))
    assert body["state"] == "hosting"
    assert "unknown" not in body
    assert len(body["users"][0]["name"]) <= 48
    assert body["bytes_in"] == (1 << 62)
    assert body["error_code"] == "PROTOCOL"
    assert body["scene"]["vertices"] == 5000
    assert body["broadcast_url"].startswith("http://127.0.0.1:7102")
    assert body["bridge_clients"] == 2
    assert body["recording"] == "session.jsonl"
    assert body["perf"]["budgets"]["tick"]["p95_ms"] == 9.5
    assert body["perf"]["violations"] == ["tick"]


def test_status_bounds_mesh_fields(appdir):
    control.write_status(
        {
            "state": "hosting",
            "mesh": {
                "active": True,
                "peers": ["a" * 200, 42, "b" * 200],
                "public_address": "203.0.113.5:" + "9" * 300,
                "error": "x" * 1000,
            },
        }
    )
    body = json.loads(control.status_path().read_text(encoding="utf-8"))
    mesh = body["mesh"]
    assert mesh["active"] is True
    assert len(mesh["peers"]) == 2
    assert all(len(peer) <= 64 for peer in mesh["peers"])
    assert len(mesh["public_address"]) <= 128
    assert len(mesh["error"]) <= 256


def test_tls_certificate_and_pinning(appdir):
    cert, key, fingerprint = tls.load_or_create_certificate(control.certs_dir())
    assert cert.exists() and key.exists()
    assert len(fingerprint) == 64
    again = tls.load_or_create_certificate(control.certs_dir())
    assert again[2] == fingerprint
    assert tls.verify_fingerprint(fingerprint, fingerprint.upper())
    assert not tls.verify_fingerprint(fingerprint, "0" * 64)
    assert len(tls.short_fingerprint(fingerprint)) == 19


def test_tofu_store(appdir):
    code = encode_room("10.1.2.3", 51999)
    assert tls.known_fingerprint(control.data_dir(), code) is None
    tls.remember_fingerprint(control.data_dir(), code, "a" * 64)
    assert tls.known_fingerprint(control.data_dir(), code) == "a" * 64
    tls.forget_fingerprint(control.data_dir(), code)
    assert tls.known_fingerprint(control.data_dir(), code) is None


def test_tls_loopback_and_fingerprint(appdir):
    cert, key, fingerprint = tls.load_or_create_certificate(control.certs_dir())

    async def scenario():
        server_context = tls.server_context(cert, key)

        async def handler(reader, writer):
            data = await reader.read(64)
            writer.write(b"echo:" + data)
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=server_context)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", port, ssl=tls.client_context(), server_hostname="blendermp"
        )
        assert tls.verify_fingerprint(tls.peer_fingerprint(writer), fingerprint)
        writer.write(b"hi")
        await writer.drain()
        assert await reader.read(64) == b"echo:hi"
        writer.close()
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())
