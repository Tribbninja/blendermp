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
* **Cross-network sessions through a self-hosted relay** (`docs/RELAY.md`). The
  host keeps one outbound tunnel, clients join with the same code, and TLS stays
  end-to-end so the relay only forwards ciphertext.
* **TLS 1.3 everywhere**, self-signed host certificate, SHA-256 fingerprint
  pinning and a short verification code (no custom crypto).
* **Host-authoritative protocol**: validated message types, size/rate limits,
  finite transform checks, mesh bounds, lease ownership, host-assigned names.
* **Chunked mesh deltas** for dense meshes: per-chunk XOR hashing, changed-vertex
  deltas, int16 quantisation, `foreach_set` application with no geometry rebuild.
  Structural edits fall back to a full geometry snapshot (vertices, edges, loops,
  polygons, UVs, smooth flags, material indices).
* **Object creation and deletion**: new mesh/empty/camera/light/curve/text objects
  are sent with a full geometry snapshot (mesh) and appear on the other side,
  respecting the source collection name when it exists; deleting sends a UUID
  tombstone. Transforms stream at 30 Hz and multiple tracked meshes round-robin
  at 12 Hz.
* **Basic material sync**: Principled BSDF base colour, metallic, roughness and
  emission, plus the viewport colour, for the active object. Node graphs and
  textures are not replicated yet.
* **Modifier stack sync**: common modifiers (Subdivision, Mirror, Array, Solidify,
  Bevel, Decimate, Triangulate, Smooth, Weld, Screw) and geometry-nodes group
  references, with visibility flags, for the active object.
* **Image sharing**: send the images used by the active object materials as packed
  PNG/JPEG/EXR/etc. assets; receivers get a packed image datablock.
* **Edit leases with request/grant**: request edit on the active object, the owner
  gets a notification and can grant it from the panel or with `/grant <user>`.
* **Presence, chat and 3D pings** between the addon UI and the desktop app, with
  latency and bandwidth telemetry, pause-sync, follow-host frame, and chat
  commands (`/who`, `/ping`, `/leave`, `/me`).
* **Desktop shell** (Tauri 2, Rust + static HTML/CSS/JS): launches Blender,
  writes the control request, docks Blender into the stage, shows room code,
  participants, chat, diagnostics and a live verification code.

The protocol and engine have **97 passing tests**, including a real TLS
host/client session, certificate-mismatch rejection, relay chat over a
multiplexed tunnel, and 1,000-vertex delta round-trips under loss/reordering.

## Cross-network

```bash
# on a VPS or spare machine
python -m server.relay --host 0.0.0.0 --port 7100
```

Set **Relay** to `your-host:7100` in Settings and enable **Host through the
relay**. Room codes then point at the relay; joining is unchanged and the TLS
certificate is still pinned to the host. See [docs/RELAY.md](docs/RELAY.md).

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

### Packaged build

```powershell
scripts/package.ps1
```

Produces:

* `dist/Blendermp-<version>-Setup.exe` — **Windows installer** (per-user, Start
  menu/desktop shortcuts, uninstaller, bundled addon).
* `dist/blendermp-windows-x64-<version>.zip` — portable app + addon + docs.
* `dist/blender_multiplayer-<version>.zip` — Blender extension (validated),
  optional if you want to launch Blender outside the app.

Silent install: `Blendermp-0.1.0-Setup.exe /S`.

The installer places the addon next to the app, so **you do not need to install
the Blender extension** when using the desktop app. Install the extension only if
you want the addon available in a normal Blender window.

Extension install:

```powershell
scripts/install_extension.ps1
```

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
  net/   UDP transport + reliable link, LAN discovery, relay agents
  sync/  mesh mirror, object transforms, leases
  session.py  host/client state machine, validation, leases
  control.py  validated local control files shared with the app
  ui.py       N-panel
desktop/ui/                    static UI (no bundler)
desktop/src-tauri/             Tauri 2 shell + Win32 embedding
server/relay.py                self-hosted cross-network relay
tests/                         protocol, chunker, leases, TLS, relay, live session
tools/                         icon generator, mesh round-trip, smoke client
scripts/                       bootstrap, dev launcher, packaging
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
3. **Structural edits**: when counts change or a mesh is newly tracked, the addon
   sends a full geometry snapshot. Receivers rebuild vertices, edges, loops,
   polygons, UVs, smooth flags and material indices, then resume deltas.
4. **Presence/chat/pings** are JSON control frames; the host rewrites identities
   so names cannot be spoofed.
5. **Relay mode** wraps the same TLS connection in a multiplexed tunnel; the host
   keeps one outbound connection, so no public port is needed.
6. **The app and addon talk through validated JSON files** in
   `%LOCALAPPDATA%\Blendermp`, which keeps the integration dependency-free.

See `docs/PLAN.md` for the full architecture, performance targets and roadmap.

## Verified in this environment

* Blender 5.3 loads the addon headlessly and hosts on a real TLS listener.
* A headless client joins, exchanges presence and chat, and leaves cleanly.
* The same chat path works through the relay (`tests/test_relay.py`).
* A geometry snapshot round-trips through Blender's mesh API (vertices, edges,
  loops, UVs, smooth flags).
* The packaged extension validates and installs with
  `blender --command extension install-file`.
* The app launches Blender, embeds its window, and reports the live room code,
  fingerprint and verification code.
* `pytest`: 97 passed.

## Limitations

* **No NAT hole punching.** Cross-network sessions need the relay (or a VPN).
  Direct LAN connections still bypass it.
* Multiple tracked meshes share one 12 Hz polling budget, so many large meshes
  update more slowly.
* Geometry-node graphs, images, animation and full node graphs are not replicated yet.
* Modifier sync covers the common parameter set; unsupported modifier types only
  replicate type/name/visibility.
* New objects are replicated for meshes, empties, cameras and lights; curves,
  font objects and modifiers are not, and collections/parenting are only
  partially covered.
* Windows-only window embedding; other platforms run Blender in its own window.
* Multires is not supported.
* Alpha software: back up work before a session.

## Security

Read [SECURITY.md](SECURITY.md). There is no custom cryptography: TLS 1.3 does
the transport, certificates are pinned, and the host validates every action.
