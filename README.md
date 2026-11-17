# Blendermp

Low-latency collaborative Blender sessions, with a desktop app that launches
Blender and **docks the live window inside the app** (native re-parenting, not
video capture, so it keeps full GPU performance).

> **Status: alpha vertical slice.** Hosting, joining by room code, TLS pinning,
> presence, chat, object transforms, mesh streaming and window embedding work.
> Cross-network NAT traversal, edit-mode topology sync and the long feature list
> in the plan are not implemented yet.

![Blendermp UI](docs/ui-preview.jpeg)

## What works today

* **Zero-config LAN sessions.** The host's machine is the server; room codes
  encode the endpoint, so there is no external service.
* **TLS 1.3 everywhere**, self-signed host certificate, SHA-256 fingerprint
  pinning and a short verification code (no custom crypto).
* **Host-authoritative protocol**: validated message types, size/rate limits,
  finite transform checks, mesh bounds, lease ownership, host-assigned names.
* **Chunked mesh deltas** for dense meshes: per-chunk XOR hashing, changed-vertex
  deltas, int16 quantisation, `foreach_set` application with no geometry rebuild.
  The engine is tested up to 1M-vertex-shaped buffers.
* **Object transforms** stream at 30 Hz; the active sculpt/edit mesh streams at
  12 Hz while you sculpt.
* **Presence and chat** between the addon UI and the desktop app, with latency
  and bandwidth telemetry.
* **Desktop shell** (Tauri 2, Rust + static HTML/CSS/JS): launches Blender,
  writes the control request, docks Blender into the stage, shows room code,
  participants, chat, diagnostics and a live verification code.

The protocol and engine have **66 passing tests**, including a real TLS
host/client session, certificate-mismatch rejection and a 1,000-vertex delta
round-trip under loss/reordering.

## Quick start (Windows)

```powershell
# 1. Install toolchains + Blender + the vendored TLS dependency.
powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1

# 2. Build and run the desktop app.
powershell -ExecutionPolicy Bypass -File scripts/dev.ps1
```

`dev.ps1` builds with the GNU Rust toolchain and starts the app. Click **Host
room**; the app launches Blender, the addon starts hosting, and the window docks
into the stage.

To start in a room immediately:

```powershell
scripts/dev.ps1 -Autohost
```

Joining: enter the host's code (or paste the invite code) under **Join with
code**. The first connection pins the host certificate and shows a verification
code; compare it with the host's.

## Build details

The repo path contains a space, and `windres` (used by Tauri's resource step)
cannot handle that. The scripts build with a target directory outside the repo:

```powershell
$env:CARGO_TARGET_DIR = "$env:LOCALAPPDATA\Temp\bmp-target"
cargo build --manifest-path desktop/src-tauri/Cargo.toml
```

Tool prerequisites:

* Node.js LTS (installed by bootstrap; the UI has no build step)
* Rust + the `x86_64-pc-windows-gnu` toolchain
* MinGW-w64 / WinLibs (`gcc`, `windres`)
* Blender 5.x
* Python 3.11+ for tests, with `numpy`, `pytest`, `cryptography`

Blender's Python disables the user site, so the bootstrap installs
`cryptography` into `%LOCALAPPDATA%\Blendermp\python`, which the addon adds to
`sys.path` at load time. Without it the addon still loads and reports a clear
error instead of crashing.

## Layout

```
addon/blender_multiplayer/     Blender 5.x extension (importable package)
  core/  protocol, varint, room codes, chunker, TLS/certificate pinning
  net/   UDP transport + reliable link, LAN discovery
  sync/  mesh mirror, object transforms, leases
  session.py  host/client state machine, validation, leases
  control.py  validated local control files shared with the app
  ui.py       N-panel
desktop/ui/                    static UI (no bundler)
desktop/src-tauri/             Tauri 2 shell + Win32 embedding
tests/                         protocol, chunker, leases, TLS, live session
tools/                         icon generator, headless smoke client
scripts/                       bootstrap + dev launcher
```

## How the sync works

1. **Transforms**: the addon polls `matrix_world`, diffs against a shadow, and
   sends 16 float32 values over the transform channel. The host validates and
   relays; receivers apply to `matrix_world`.
2. **Meshes**: positions are read with `foreach_get` into preallocated numpy
   buffers, split into 8,192-vertex chunks, hashed with a vectorised XOR reduce,
   and only changed chunks are diffed. Deltas carry varint index jumps and either
   float32 or int16-quantised coordinates. Receivers write the shadow buffer and
   `foreach_set` once, with no `clear_geometry`.
3. **Presence/chat** are JSON control frames; the host rewrites identities so
   names cannot be spoofed.
4. **The app and addon talk through validated JSON files** in
   `%LOCALAPPDATA%\Blendermp`, which keeps the integration dependency-free.

See `docs/PLAN.md` for the full architecture, performance targets and roadmap.

## Verified in this environment

* Blender 5.3 loads the addon headlessly and hosts on a real TLS listener.
* A headless client joins, exchanges presence and chat, and leaves cleanly.
* The app launches Blender, embeds its window, and reports the live room code,
  fingerprint and verification code.
* `pytest`: 66 passed.

## Limitations

* **LAN only for now.** Cross-network rooms need a reachable host (port forward,
  IPv6 or a VPN overlay) until the self-hosted relay lands.
* Edit-mode **topology** changes are not streamed yet; position-only deltas are.
* One tracked mesh (the active object) is streamed at a time.
* No multires, images, materials or animation sync in this slice.
* Windows-only window embedding; other platforms run Blender in its own window.
* Alpha software: back up work before a session.

## Security

Read [SECURITY.md](SECURITY.md). There is no custom cryptography: TLS 1.3 does
the transport, certificates are pinned, and the host validates every action.
