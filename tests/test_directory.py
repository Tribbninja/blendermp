import json
import threading
import urllib.error
import urllib.request

import pytest

from server.directory import Directory, DirectoryError, create_server


@pytest.fixture()
def directory_server():
    server = create_server("127.0.0.1", 0, Directory(ttl=60))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def call(base, method, path, body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, response.headers, json.loads(response.read())


def entry(**extra):
    body = {
        "id": "a" * 32,
        "name": "Sculpt Room",
        "host": "isaac",
        "code": "K7M2Q-X9ABC-DEFGH",
        "players": 2,
        "max": 16,
        "password": True,
        "relay": True,
        "version": "0.1.0",
    }
    body.update(extra)
    return body


def test_announce_list_search_and_remove(directory_server):
    status, headers, body = call(directory_server, "POST", "/announce", entry())
    assert status == 200 and body["ok"]
    assert headers["Access-Control-Allow-Origin"] == "*"

    status, _, body = call(directory_server, "GET", "/servers")
    assert status == 200
    servers = body["servers"]
    assert len(servers) == 1
    assert servers[0]["name"] == "Sculpt Room"
    assert servers[0]["code"] == "K7M2QX9ABCDEFGH"
    assert servers[0]["password"] is True
    assert servers[0]["relay"] is True
    assert "id" not in servers[0]

    _, _, body = call(directory_server, "GET", "/servers?q=sculpt")
    assert len(body["servers"]) == 1
    _, _, body = call(directory_server, "GET", "/servers?q=missing")
    assert body["servers"] == []

    _, _, body = call(directory_server, "POST", "/announce", entry(players=5, name="Updated"))
    assert body["ok"]
    _, _, body = call(directory_server, "GET", "/servers")
    assert body["servers"][0]["name"] == "Updated"
    assert body["servers"][0]["players"] == 5

    _, _, body = call(directory_server, "POST", "/remove", {"id": "a" * 32})
    assert body["ok"] is True
    _, _, body = call(directory_server, "GET", "/servers")
    assert body["servers"] == []


def test_directory_rejects_bad_entries(directory_server):
    bad = [
        entry(id="short"),
        entry(name=""),
        entry(code="not-a-code"),
        entry(players=99),
        entry(players=20, max=10),
    ]
    for body in bad:
        with pytest.raises(urllib.error.HTTPError) as exc:
            call(directory_server, "POST", "/announce", body)
        assert exc.value.code == 400


def test_directory_health_and_cors(directory_server):
    status, headers, body = call(directory_server, "GET", "/health")
    assert status == 200 and body["ok"] is True
    assert headers["Access-Control-Allow-Origin"] == "*"


def test_directory_expiry_and_limit():
    directory = Directory(ttl=10, max_servers=2)
    assert directory.announce(entry(id="a" * 32), now=100)["ok"]
    assert directory.announce(entry(id="b" * 32), now=100)["ok"]
    assert len(directory.servers(now=105)) == 2
    with pytest.raises(DirectoryError):
        directory.announce(entry(id="c" * 32), now=105)
    assert directory.servers(now=111) == []
