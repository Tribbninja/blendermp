# Blendermp hub on Cloudflare Workers

This Worker is the always-on public directory **and** the WebSocket relay. It
lets the desktop app's **Public** checkbox work across networks without port
forwarding or a VPS.

## Deploy

Requires a Cloudflare account with Workers enabled. The free plan includes the
SQLite-backed Durable Object used here.

```powershell
cd cloudflare
npx wrangler login
npx wrangler deploy
```

Wrangler prints a URL like:

```
https://blendermp-hub.<your-subdomain>.workers.dev
```

Put that URL in **Settings -> Directory URL** on every PC (host and friends).
No other setup is needed.

## Local development

```powershell
cd cloudflare
npx wrangler dev --local --port 8787
```

Use `http://127.0.0.1:8787` as the directory URL; the app derives the
`ws://127.0.0.1:8787` relay endpoint from it.

## What it stores

The directory stores server name, host name, room endpoint, player count,
password flag, relay flag and version for 60 seconds after the last announce.
It never receives the server password or scene data. The relay forwards binary
WebSocket messages between the host tunnel and clients; TLS remains end-to-end
between Blender instances.

## Host authentication

A relay host must register a per-install host key with `/announce` and send the
same key in the `X-BMP-Host-Token` header when it opens the
`/relay/<room>?role=host` WebSocket. The Worker stores only a SHA-256 hash, never
returns it from `/servers`, and compares it in constant time. A connection with
no key or the wrong key is rejected with 403, so a client who can see the public
room code cannot replace the host or kick everyone. Joining clients do not need
the key; the host's TLS certificate pinning and optional password still
authenticate them.
