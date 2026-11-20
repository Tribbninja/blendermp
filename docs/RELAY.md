# Cross-network sessions with the relay

> For most users the free Cloudflare Worker hub in [../cloudflare/README.md](../cloudflare/README.md) is simpler: it provides the public directory and the WebSocket relay with no port forwarding. The Python relay and directory below remain the self-hosted alternative.

The relay lets two machines on different networks connect without port
forwarding. It is a **self-hosted, multiplexed TCP pipe**: the host keeps one
outbound connection to the relay, clients connect to the relay, and the host's
TLS 1.3 session is carried through it end to end. The relay never sees scene
data or keys, only ciphertext and connection metadata.

```
client ──TLS──┐
              ├── relay ── tunnel ── host (TLS server on 127.0.0.1)
client ──TLS──┘
```

## Run a relay

Any small VPS or spare machine works. Python 3.11+ is the only requirement.

```bash
python -m server.relay --host 0.0.0.0 --port 7100
```

Open **TCP 7100** in the firewall. With systemd:

```ini
[Unit]
Description=Blendermp relay
After=network.target

[Service]
WorkingDirectory=/opt/blendermp
ExecStart=/usr/bin/python3 -m server.relay --host 0.0.0.0 --port 7100
Restart=always
User=blendermp

[Install]
WantedBy=multi-user.target
```

Limits: 512 rooms, 32 concurrent streams per host, 256 KB frames, and idle
connections are dropped. The relay does not authenticate users; the host's
pinned TLS certificate does that.

## Host through the relay

1. Settings → **Relay** = `203.0.113.5:7100` (the relay's public address).
2. Enable **Host through the relay**.
3. Host the room. The room code now points at the relay and is flagged as relay
   mode; the addon opens the tunnel in the background.
4. Share the code.

Unlike the LAN path, the host does **not** bind a public port; Blender's TLS
listener stays on `127.0.0.1`, and the relay bridges to it.

## Join

Enter the code as usual. The addon sees the relay flag, opens a local loopback
endpoint, and performs the normal TLS handshake through the relay. Certificate
pinning and the verification code work exactly as on a LAN.

## What the relay can and cannot see

| Sees | Cannot see |
|---|---|
| Client and host IP addresses | Scene data |
| Room ids (a SHA-256 prefix of the code) | Chat |
| Connection timing and byte counts | Certificates' private keys, PINs |
| Whether a room has a host online | Anything inside TLS |

The room id is derived from the code and is not a secret. Anyone who knows the
code can attempt to join, which matches the LAN model. The host still verifies
the TLS certificate; relay operators cannot impersonate a host without the
private key.

## Limitations

* All traffic takes the relay path, so latency is `client ↔ relay ↔ host`.
* The relay is a single TCP endpoint; use a provider with decent bandwidth if
  sculpting heavily.
* No automatic NAT hole punching yet — direct LAN connections still bypass the
  relay, and a future release will try direct routes before falling back.
* The relay does not persist anything; if it restarts, the host tunnel
  reconnects automatically within a couple of seconds.

## Test locally

```bash
python -m server.relay --host 127.0.0.1 --port 7100
```

Set the relay to `127.0.0.1:7100` on the host and join from the same machine to
exercise the full path. The test suite does this automatically
(`tests/test_relay.py`).

## Public server directory

Run the directory next to the relay on the same VPS:

```bash
python -m server.directory --host 0.0.0.0 --port 7101
```

Open **TCP 7101** in the firewall. Set **Directory URL** to
`http://your-host:7101` on the host and on every client (Settings). The host
sets a server name, optionally a password, and enables **List in the public
server browser**. Clients open **Browse servers**, search by name and click
**Join**. The host can kick players from the member list.

The directory stores only the server name, host name, room endpoint, player
count, password flag and version, and forgets an entry after 60 seconds without
a refresh. It never sees scene data, passwords or certificates. Put it behind an
HTTPS reverse proxy if it is exposed to the public internet.
