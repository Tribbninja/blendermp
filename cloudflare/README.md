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
