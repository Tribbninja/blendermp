# Blendermp

Low-latency collaborative Blender sessions, with a desktop app that launches
Blender and **docks the live window over the app viewport** (a native owned
window, not video capture, so it keeps full GPU performance and avoids the black
surface that cross-process re-parenting causes on some GPUs).

> **Status: 0.9.14 collaboration foundation.** Hosting, the public server
> browser, TLS pinning, presence, chat, object transforms, mesh streaming, live
> Edit Mode topology, animation, sculpt, materials/textures, asset bundles,
> roles, checkpoints, an Ollama copilot, session recording/replay, a live
> broadcast page and a cross-engine bridge all ship in this build.
> Pre-1.0 builds require an exact version match on both PCs.

![Blendermp UI](docs/ui-preview.jpeg)

## What works today

* **Public server browser.** Hosts set a server name, publish it to a directory,
  and optionally add a password. Everyone can search by name and click to join;
  room codes stay internal. Hosts can kick players from the member list. The
  directory is a small self-hosted JSON service; entries expire after 60 seconds.
* **Cross-network without port forwarding.** A free Cloudflare Worker
  (`cloudflare/`) provides the public directory **and** the WebSocket relay. The
  host ticks Public and clients anywhere find it in the browser. TLS stays
  end-to-end; the Worker only forwards ciphertext. A self-hosted Python
  relay/directory (`docs/RELAY.md`) is the fallback.
* **Scene bootstrap on join**: the host streams its existing objects, meshes,
  materials, modifiers, collections, active camera, world background and render
  settings; the joining Blender replaces its local scene with that snapshot
  before live sync starts, so both start identical.
* **Live Edit Mode topology**: vertex drags stream as deltas, and structural
  changes (extrude, subdivide, bevel, delete) send a debounced full topology
  snapshot after the operation settles. The receiver rebuilds the mesh while
  remaining in Object Mode. Large snapshots use a C-side read and a cheaper
  compression pass.
* **Collaborator overlays**: each peer's viewport is drawn as the same
  seven-point camera frustum used by the Multi-User extension: the four viewport
  corners and centre projected one unit into the scene, the orbit pivot, and an
  apex 0.8 units behind the camera. The frustum and the username tag use that
  peer's colour, and the name sits on the top-right corner. Objects selected by a
  peer get a wireframe outline in that peer's colour. Hover any outlined object
  to see who is editing or has it selected. Every overlay has its own toggle
  (Show remote cameras, Show remote selections, Show name tags, Show who is
  editing on hover).
* **Independent timelines**: animated object transforms, pose and shape values
  are evaluated locally from the shared action, so each user can scrub to a
  different frame. Turn on **Sync live pose/shape values** only if you want
  manual rig posing shared.
* **Scene fingerprint + resync**: presence carries a cheap structural hash of
  objects, types, mesh counts and material slots. If a client differs, the panel
  warns and the host can press **Resync scene to everyone**.
* **LAN render farm**: split a frame range across the room. Peers claim a frame,
  render it from their local scene and upload the PNG back to the host. It is
  LAN-only, token-protected, and rendering blocks Blender while a frame runs.
  Fluid caches stay local; frame rendering is the distributed part.
* **AI copilot (local Ollama)**: a chat panel proposes structured edits
  (transforms, primitives, materials, modifiers, booleans, scene settings) with
  a scene snapshot for context. Nothing is applied until you confirm; failed
  edits stay in the list with their error.
* **Recording, replay and live broadcast**: every local send and remote event is
  tapped into a JSONL recording. Replay feeds the events back at their original
  timing. A token-protected localhost page streams the same events to a browser
  over Server-Sent Events for debugging or a live audience.
* **Cross-engine bridge**: stream objects, transforms, materials and triangle
  meshes to Unity, Unreal or Godot over newline-delimited JSON. Reference
  receivers live in `integrations/`.
* **Measured performance**: the addon publishes p50/p95/p99 budgets for tick,
  apply, topology reads and the bridge in `status.json`; the topology snapshot
  uses `BMesh.to_mesh` + `foreach_get` instead of per-element Python loops.
* **One editor per object**: selecting an object claims an edit lease. Other
  players see it locked (unselectable) and cannot edit it until the owner
  deselects, stops editing for 30 seconds, or leaves. Idle selections release
  themselves; **Release locks** in the panel clears them immediately, and
  **Edit locks** can be turned off entirely. The active object name is shown
  for each player.
* **Remote presence in the viewport**: each player's viewport camera is drawn as
  a colored view pyramid with a look-direction ray and their name above it, and
  their selected objects are wireframed in their color with a name tag. Camera
  poses stream at up to 30 Hz on low-latency links, apply immediately, and are
  normalized so orthographic zoom cannot distort the widget. Name
  tags use a DPI-scaled
  outlined font so they stay crisp. Each user gets a stable color derived from
  their name if they have not chosen one.
* **Presence 2.0**: remote Edit Mode vertex selections as colored points, a
  remote 3D cursor crosshair, follow a user's viewport, and per-overlay
  visibility toggles.
* **Server-enforced roles**: the host assigns viewer, commenter, editor or admin
  from the member list. Mutating frames and lease acquisition are rejected for
  viewers/commenters; downgrading releases their leases.
* **Authenticated relay hosting**: each installation has a random host key; the
  Worker stores only its hash and requires it for the host WebSocket. A public
  room cannot be taken over by another host, and the key is never listed.
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
  tombstone. Imported or duplicated objects with stale/copied IDs are assigned a
  fresh identity. Model data is never quantised: direct LAN sessions send raw payloads, and relay sessions use lossless zlib for geometry, vertex deltas, bundles and images. Transforms stream at up to 60 Hz on low-latency/LAN links (30 Hz
  otherwise) and multiple tracked meshes round-robin at 20 Hz. A LAN-discovered
  host advertises a direct code that bypasses the relay.
* **Immediate remote application**: received transforms and vertex deltas are
  applied exactly at network rate (up to 60 Hz on LAN). There is no display
  interpolation, so the remote view never lags behind the authoritative state.
* **Material graph sync**: a whitelisted shader graph (Principled BSDF, Material
  Output, Image Texture, UV Map, Mapping, Normal Map, Bump, Mix, ColorRamp,
  Emission, procedural textures, colour/vector maths) is replicated for every
  object's material slots, with node values, links and viewport colour.
* **Texture transfer**: images referenced by material nodes are sent with a
  SHA-1 digest and packed on the receiver; duplicates are skipped, colours and
  alpha modes are preserved, and a late image binds into its pending node.
* **Object details**: parenting with `matrix_parent_inverse`, modifier stacks,
  collection membership and collection visibility, object colour, and
  viewport/render visibility, for every object rather than the active one.
* **Edit leases with request/grant**: request edit on the active object, the owner
  gets a notification and can grant it from the panel or with `/grant <user>`.
* **Reconnect and resume**: a dropped client keeps its scene and reconnects with
  backoff, retaining its leases and identity. The host resends authoritative
  state without a scene reset; a host restart falls back to a clean bootstrap.
* **Offline edit journal**: transforms, mesh snapshots and material changes made
  while disconnected are recorded locally, replayed on resume, and surfaced as
  a conflict prompt when the host also changed the object.
* **Scoped undo**: `Ctrl+Alt+Z` / **Undo my edit** reverts only the local user's
  last transform if nobody else has changed it since; remote edits are skipped.
* **Checkpoints**: create a `.bmpz` scene snapshot plus a `.blend` copy, restore
  it live with peers connected, clear stale leases, and resync clients. Automatic
  checkpoints run every ten minutes and keep the last three.
* **Signed host identity and roles**: an Ed25519 host key signs the session
  descriptor, clients verify it on join, the Worker verifies signed directory
  announcements, PINs are scrypt-hashed with a per-IP attempt limit, and role
  changes/kicks are audited.
* **Armature, pose and actions**: bones, rest transforms, vertex weights and
  armature modifier binding; pose bone location/quaternion/scale changes at
  30 Hz; active action F-curves and keyframes; shape-key names/values and
  constraint bindings.
* **Sculpt streaming**: plain Sculpt Mode writes position deltas at 20 Hz and
  sends a full topology snapshot when dyntopo/counts change. Multires sculpt is
  detected and reported as unsupported instead of silently drifting.
* **Subscriptions and interest**: per-object full/key/mute modes, per-client
  camera/AABB priority, 30/10/2 Hz transform tiers and 20/5/1 Hz mesh tiers, a
  per-peer 8 Mbps byte budget, and final-frame resend after rate drops.
* **Adaptive quality**: clients report apply p95, FPS, RX/TX and queue depth once
  per second; the host lowers rates in high/medium/low tiers with hysteresis.
* **Split relay**: the Worker now uses a Directory Durable Object for listings
  and a per-room Room Durable Object for WebSockets, with signed announce
  verification, 90-second listings and room/host token checks.
* **Cross-platform shell**: Windows NSIS plus macOS/Linux Tauri builds, platform
  Blender detection, separate-window fallback outside Windows, and CI checks on
  all three desktop platforms.
* **Security hardening**: strict webview CSP, command-scoped capabilities,
  path/zip-bomb limits for checkpoints, and randomized decoder fuzz tests for
  geometry, images, objects and materials.
* **Large uploads**: images over 10 MB are zlib-compressed before transfer when
  that reduces size, and large mesh geometry snapshots use the higher-ratio
  compression pass. Image payloads are capped at 32 MB and frames at 64 MB.
* **Chunked large-object transfer**: object creation payloads above 32 MB are
  split into 8 MB chunks with deterministic content hashes, progress events and
  partial-transfer retention. Retried transfers fill missing chunks instead of
  restarting. The same path works host-to-client and client-to-host.
* **Import fidelity**: all mesh UV layers (not just the active one), object and
  armature NLA tracks/strips with their actions, safe object-transform drivers,
  collection instances with their instanced objects, and curve/text objects.
* **Send selection**: package selected objects plus their materials and images
  into a content-addressed bundle, send it to one teammate, preview the summary,
  and import it with fresh object IDs. Bundles are validated and re-packed by the
  host before relay.
* **View bookmarks**: save named viewport bookmarks locally, jump between them,
  and let a host or admin push one to everyone. Incoming pushes can be disabled.
* **Deep links and recent rooms**: `blender://join/CODE` (and `blendermp://`)
  invite links register with the installer, open the running app, and join the
  room. The server browser keeps recent and saved rooms.
* **Detachable dashboard**: a separate browser window mirrors people, edit leases,
  per-peer sync queues, recent chat and errors, with role/kick/chat/leave
  controls and optional sound or do-not-disturb notifications.
* **Laser pointer**: hold the pointer over the 3D view and move the mouse to send
  a bright, short-lived crosshair to everyone. It is separate from selection and
  camera overlays and can be hidden per client.
* **Accessibility and theming**: Okabe-Ito colour-blind-safe palette, high
  contrast, larger text, reduced motion, overlay opacity/scale, focus rings and
  notification sound toggles. Preferences are shared between the main app and
  dashboard.
* **Presence, chat and 3D pings** between the addon UI and the desktop app, with
  latency and bandwidth telemetry, pause-sync, follow-host frame, and chat
  commands (`/who`, `/ping`, `/leave`, `/me`).
* **Desktop shell** (Tauri 2, Rust + static HTML/CSS/JS): launches Blender (and closes it when the app closes),
  writes the control request, docks Blender into the stage, shows the server
  browser, participants, chat, diagnostics and a live verification code.
* **Support diagnostics and crash recovery**: the app and addon export a
  redacted zip (versions, OS/GPU, logs, network/scene stats, settings/session
  without keys or PINs). Each Blender writes a heartbeat; an unclean exit shows
  a recovery notice and can open the newest local checkpoint.
* **Modular tile workspace with fixed docks**: click **Layout** to enter edit
  mode, then drag Session, Chat or Diagnostics by its top bar onto the **Left**,
  **Right** or **Bottom** dock. Panels reflow live as you hover a dock, swap
  cleanly when a dock is occupied, and settle with smooth transition when you
  release. The **viewport always stays in the main area**. Splitters resize each
  dock, the rail toggles panels, and every arrangement is saved. **Reset**
  restores the default layout (Session left, Chat right, empty bottom) with the
  same animated transition. Blender is hidden while arranging and returns to the
  viewport when you press Done or Escape.

The protocol and engine have **216 passing unit tests** and **12 two-Blender or
headless e2e suites**, including a real TLS host/client session,
certificate-mismatch rejection, relay chat, password rejection and host kick
over a multiplexed tunnel, the public directory API, 1,000-vertex delta
round-trips under loss/reordering, live Boolean/modifier sync, and the AI,
bridge, broadcast and recording foundation check. The Cloudflare Worker hub was
also exercised locally with `wrangler dev`: announce/list, host and client
through the WebSocket relay, and kick.

## AI copilot (Ollama)

1. Install [Ollama](https://ollama.com/) and pull a tool-capable model:
   `ollama pull qwen2.5:7b`.
2. In Blender's Blendermp panel, enable **AI Copilot**, set the URL
   (`http://127.0.0.1:11434`), model and timeout.
3. Press **Test** to confirm the server and model are available.
4. Type a request, press **Ask**, review the proposed edits, then **Apply all**.

The model receives a compact scene snapshot (objects, types, locations,
selection, materials) so small local models do not have to guess names. Edits
are applied only after confirmation, through the same sync/lease path as a
manual edit. A slow first load is normal; raise the timeout if the GPU is busy.

## Live view and cross-engine

* **Recording & Replay**: the Recording box writes every local send and remote
  event to `%LOCALAPPDATA%\Blendermp\recordings`. Replay feeds it back at the
  original rate and speed.
* **Live Broadcast**: start the live view and open the token-protected
  localhost page. It shows chat, transforms, object creates/deletes, locks and
  errors over Server-Sent Events. No external JS or second window.
* **Cross-engine Bridge**: choose Unity, Unreal or Godot and press Start. The
  TCP bridge sends transforms, visibility, materials and triangle meshes.
  Reference receivers are in `integrations/` (Unity C#, Unreal Python, Godot 4
  GDScript, plus a console receiver).

## Viewport, timeline and LAN render farm

* **Same models, clean viewport.** Remote camera widgets, look rays and cursor
  crosses are removed. Name tags, lasers and selection wireframes are off by
  default and are not forced on at session start. Remote selected-object lines
  now obey `Show selections`, so the red stray lines are gone too. A structural
  scene hash warns if a client is missing objects or has different mesh counts,
  and the host can resync.
* **Crash notice hygiene.** A crash marker from an older addon build is ignored,
  so updating/installing no longer produces a false “closed unexpectedly”
  notice. The LAN farm host does not render on the host PC unless
  `This PC renders too` is explicitly enabled.
* **Separate timelines.** Object transforms driven by actions/NLA/drivers,
  armature poses and shape-key values are evaluated locally. Everyone shares the
  same action and models but can sit on a different frame. **Follow host frame**
  remains optional.
* **LAN render farm.** Start a host, copy the URL and token, and join from the
  other PCs. The host splits the frame range into one chunk per frame. Workers
  render their claimed frame from the synced scene and upload a PNG. Results
  land in `render-<timestamp>/frame_XXXXX.png` on the host. The server binds the
  LAN interface, requires a random token, caps uploads at 64 MB and rejects
  non-PNG results. Rendering blocks that worker's Blender while a frame runs.

Fluid/simulation caches are not distributed yet: Blender keeps point caches
local, so a shared-cache workflow is a separate feature.

## Bandwidth

Internet sessions use a deliberately different profile from direct LAN sessions:

* **Presence is delta-encoded.** Only changed fields are sent. An idle session
  sends no presence at all except a small full snapshot every 30 seconds.
* **Viewport position is opt-in.** `Share my viewport position` is off by
  default, so the 16-float camera matrix and cursor are not transmitted. Turn it
  on only if you want remote view tags.
* **Relay profile:** presence at 5 Hz while moving (3 Hz on high RTT), interest
  updates every 5 s, client stats every 2 s, PING every 5 s. Direct LAN keeps
  30 Hz presence and 60 Hz transforms.
* **Idle cost:** roughly 0.1 KB/s per client (stats, an occasional ping and the
  30 s presence snapshot), instead of the old 10–20 KB/s from 20–30 Hz full
  presence.
* **Edits still use data:** transforms, mesh deltas, materials, textures and
  asset bundles are sent when they change. Large assets and chunked transfers
  are the dominant cost during real work, not idle time.
* **Smooth host-to-client motion:** `CLIENT_STATS` now reports the addon tick
  rate, not `scene.render.fps`. A 24 fps scene no longer tricks the host into the
  lowest quality tier. Transform updates keep a 5–20 Hz floor even for
  off-screen or low-quality peers; mesh updates are the part that drops.
* **Direct LAN fast path:** peers connecting from a private LAN address bypass
  interest/quality rate limiting completely. Transforms and presence go at the
  sender’s full rate, as fast as the socket allows. Relay peers keep the reduced
  internet profile, and state-changing presence updates are never dropped by the
  relay camera throttle.
* **Silent by default:** notification beeps and their toggles were removed from
  the app and dashboard.

## Cross-network

### Cloudflare Worker hub (recommended, no port forwarding)

Deploy the Worker once:

```powershell
cd cloudflare
npx wrangler login
npx wrangler deploy
```

Wrangler prints a URL like
`https://blendermp-hub.<your-subdomain>.workers.dev`. Put it in **Settings ->
Hub URL** on the host and every friend, with **Use the hub as the WebSocket
relay** checked. That is the only setup. The host ticks **Public**, the server
appears in everyone's browser, and connecting goes through the Worker relay.
See [cloudflare/README.md](cloudflare/README.md).

### Self-hosted Python relay and directory

```bash
# on a VPS or spare machine
python -m server.relay --host 0.0.0.0 --port 7100
python -m server.directory --host 0.0.0.0 --port 7101
```

Set **Hub URL** to `http://your-host:7101`, uncheck the Worker relay, and use
the advanced **Relay** field (`your-host:7100`) with **Host through the
advanced relay**. Published entries point at the relay; joining from the browser
is unchanged and the TLS certificate is still pinned to the host. See
[docs/RELAY.md](docs/RELAY.md).

## Quick start (Windows)

```powershell
# 1. Install toolchains + Blender + the vendored TLS dependency.
powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1

# 2. Build and run the desktop app.
powershell -ExecutionPolicy Bypass -File scripts/dev.ps1
```

`dev.ps1` builds with the GNU Rust toolchain and starts the app. Set a server
name and password, then click **Host server**; the app launches Blender, the
addon starts hosting, and the window docks into the stage. With a **Hub URL**
configured and **List in the public server browser** enabled, the server appears
for everyone using that hub.

To start in a room immediately:

```powershell
scripts/dev.ps1 -Autohost
```

Joining: click **Browse servers**, search by name, and press **Join**. If the
server has a password, enter it in the prompt. The first connection pins the
host certificate and shows a verification code; compare it with the host's. If
the host's certificate changed (reinstall or the 30-day rotation), the panel
shows the new short code and a **Trust new certificate** button; compare it with
the host before trusting. Joins use the public relay whenever the host publishes one; LAN-direct is only used for LAN-only hosts because the host's random direct port is often blocked by Windows Firewall. The host can kick anyone from the member list.

### Packaged build

```powershell
scripts/package.ps1
```

Produces:

* `dist/Blendermp-<version>-Setup.exe` - **Windows installer** (per-user, Start
  menu/desktop shortcuts, uninstaller, bundled addon). It checks for Blender;
  if none is found it offers to download the official Blender 5.3 MSI from
  blender.org, verifies the pinned SHA-256, installs it silently, and then
  installs Blendermp. Silent setup (`/S`) installs Blendermp only.
* `dist/blendermp-windows-x64-<version>.zip` â€” portable app + addon + docs.
* `dist/blender_multiplayer-<version>.zip` â€” Blender extension (validated),
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
* Blender 4.2 LTS or newer (5.x supported)
* Python 3.11+ for tests, with `numpy`, `pytest`, `cryptography`

Blender's Python disables the user site, so the installer bundles a cp313
`cryptography` vendor tree into `%LOCALAPPDATA%\Blendermp\python`, which the
addon adds to `sys.path` at load time. The desktop app also sets `PYTHONPATH`
and copies the vendor from a portable install if it is missing. Without it the
addon still loads and reports a clear error instead of crashing.

## Layout

```
addon/blender_multiplayer/     Blender 4.2+ extension (importable package)
  core/  protocol, varint, room codes, chunker, TLS/certificate pinning
  net/   UDP transport + reliable link, LAN discovery, relay agents
  sync/  mesh mirror, object transforms, leases
  session.py  host/client state machine, validation, leases
  control.py  validated local control files shared with the app
  ui.py       N-panel
desktop/ui/                    static UI (no bundler)
desktop/src-tauri/             Tauri 2 shell + Win32 window docking
server/relay.py                self-hosted cross-network relay
server/directory.py            self-hosted public server directory
cloudflare/                    free Worker hub: public directory + WS relay

tests/                         protocol, chunker, leases, TLS, relay, live session
tools/                         icon generator, mesh round-trip, smoke client
scripts/                       bootstrap, dev launcher, packaging
```

## How the sync works

1. **Transforms**: the addon polls `matrix_world`, diffs against a shadow, and
   sends 16 float32 values over the transform channel. The host validates and
   relays; receivers apply to `matrix_world` and update their shadow so a remote
   move is never echoed back.
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
5. **Scene bootstrap**: when a client joins, the host sends `SCENE_RESET`, then
   each object (mesh geometry, materials, modifiers) over a few ticks, then
   `SCENE_READY`. The client clears its scene, applies the snapshot and seeds all
   shadows so nothing is echoed back.
6. **Live Edit Mode**: the editor's BMesh is sampled at up to 30 Hz and only
   changed vertices are sent. Receivers in Object Mode write them through
   `foreach_set`; receivers already in Edit Mode apply them through BMesh.
7. **Relay mode** wraps the same TLS connection in a multiplexed tunnel; the host
   keeps one outbound connection, so no public port is needed.
8. **The app and addon talk through validated JSON files** in
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
* The app launches Blender, docks its window, and reports the live server
  status, fingerprint and verification code.
* Two real Blender 5.3 instances: the host's scene, material graph, packed
  texture, modifier stack, parenting, collection, camera, world and render
  settings bootstrap arrives on the client; object moves and live Edit Mode
  vertex drags propagate **both ways** without snapping back; the host's
  selection locks the object for the client until the host releases it.
* Checked-in two-Blender harnesses cover textured material graphs
  (`run_material.ps1`), object details (`run_details.ps1`), live topology
  (`run_topology.ps1`), resume/journal (`run_resume.ps1`), checkpoint restore
  (`run_checkpoint.ps1`), scoped undo (`run_undo.ps1`), armature/pose/action/
  shape keys (`run_animation.ps1`), sculpt (`run_sculpt.ps1`), collaboration bundles/bookmarks (`run_collab.ps1`) and chunked large-object transfer (`run_chunk.ps1`);
  `run_all.ps1` runs all ten.
* Headless `bench/bench_mesh.py` records read/delta/topology/material timings in
  `bench/baseline.json`.
* The production Worker relay was exercised with the host key: no token and a
  wrong token are rejected, a valid host tunnel and client join succeed.
* `pytest`: 160 passed.

## Limitations

* **No direct NAT hole punching.** Cross-network sessions use the Cloudflare
  Worker hub or the self-hosted relay. Direct LAN connections still bypass it.
* Multiple tracked meshes share the object-mode polling budget (20 Hz), so many
  large meshes update more slowly. The actively edited mesh has its own 30 Hz
  fast path.
* Two people cannot select/edit the same object at once: the first selector
  holds the lease. Deselecting or disconnecting releases it, including a short
  debounce so mode switches do not release accidentally.
* Remote camera widgets and selection wireframes are viewport overlays in the
  local Blender; they are not scene objects, are not rendered, and are not
  synced back to other clients. Camera poses update at up to 30 Hz and are
  applied immediately; their zoom scale is stripped so orthographic views cannot stretch
  them into stray lines. Selection outlines skip oversized/corrupt edges and
  fall back to a bounding box above 120 edges.
* Edit Mode streams vertex positions live. Structural changes (extrude,
  subdivide, bevel, delete) send a debounced full topology snapshot while the
  editor is still in Edit Mode; the receiver rebuilds the mesh in Object Mode.
* Worlds are limited to background colour/strength. Node groups, scripts and
  other unsafe shader nodes are skipped rather than approximated; the rest of a
  material graph still applies. Compositor nodes, simulations and linked
  libraries are not replicated yet.
* Action sync covers the active action plus NLA tracks/strips and their actions.
  Shape-key values and coordinates sync. Object-transform drivers are replicated
  only when their expression is a safe arithmetic/math subset; other drivers are
  skipped.
* Constraint sync covers type/name/influence/target/subtarget, not every
  constraint-specific property.
* Multires sculpt is not supported; dyntopo sends full topology snapshots,
  which are heavier than plain position deltas.
* Modifier sync covers the common parameter set; unsupported modifier types only
  replicate type/name/visibility.
* New objects are replicated for meshes, empties, cameras, lights, curves and
  text. Collection-instance contents are included in bundles; normal scene sync
  only references collections that already exist on the receiver.
* Chunked transfers are capped at 256 MB per object and 20M vertices. Partial
  transfers survive for five minutes and a retried send resumes, but an
  interrupted transfer is not automatically requested after reconnect.
* Scoped undo currently covers object transforms. Offline journaling covers
  transforms plus the active object's mesh and materials; background objects
  with offline mesh/material edits are not journaled.
* Checkpoint files use zip (`.bmpz`) rather than zstd, and the automatic
  interval/retention are fixed at 10 minutes and three files. The scene JSON is
  capped at 64 MB, so very large scenes may exceed the checkpoint limit even
  though live transfer supports larger objects.
* Signed directory verification is implemented in `cloudflare/src/index.js`;
  the public Worker must be redeployed for it to take effect. LAN/self-hosted
  directories store the signature but do not verify it.
* PINs are scrypt-hashed (stdlib) rather than Argon2id, and the identity private
  key uses owner-only file permissions rather than DPAPI because Blender owns
  the key file.
* Interest priority is a distance/frustum approximation, not full occlusion
  culling. Adaptive quality uses apply p95 and a render-FPS estimate; queue depth
  is currently reported as zero by the addon.
* macOS/Linux builds are wired into CI and the shell, but this environment only
  validates the Windows build. Docking remains Windows-only.
* The split Worker Directory/Room layout and signed announce verification need
  a `wrangler deploy`; existing deployments continue to use the old single Hub.
* Dependency audits are advisory in CI, not yet a release blocker.
* Asset bundles carry objects, materials and images, not shader node groups;
  unsupported graph branches are flattened or skipped by the existing material
  sanitizer. A bundle is capped at 64 objects, 64 images and 56 MB on the wire.
* The detached dashboard is a token-protected localhost page opened in the system browser; it requires the desktop app's directory listener.
* Laser markers expire after about 1.2 seconds and are not scene objects.
* Deep-link protocol registration is performed by the Windows installer; portable
  or manually copied builds need the `blender`/`blendermp` URL handler registered
  separately. The app also accepts invite links from the server browser.
* Windows-only window docking; other platforms run Blender in its own window.
* Multires is not supported.
* Alpha software: back up work before a session.

## Diagnostics

- In the app, open **Diagnostics â†’ Export diagnostics bundle**. The redacted zip
  is written to `%LOCALAPPDATA%\Blendermp\diagnostics\`.
- In Blender, **N-panel â†’ Blendermp â†’ Export diagnostics bundle** writes the
  same kind of zip through a file picker.
- The bundle contains versions, OS/GPU, `status.json`, heartbeats, recent
  network/scene stats and the last 500 lines of `ui.log`, `blender.log` and
  `events.log`. User paths, host keys, relay tokens and PINs are redacted.
- A heartbeat is written every two seconds. If Blender did not shut down
  cleanly, the panel shows a crash notice and a **Recover** button that opens the
  newest local checkpoint (once checkpoints exist).
- Performance can be re-measured with
  `blender --background --factory-startup --python bench/bench_mesh.py -- --write-baseline`.

## Troubleshooting

If Blender does not start or the session stays offline, check
`%LOCALAPPDATA%\Blendermp\blender.log` (Blender and addon output) and
`%LOCALAPPDATA%\Blendermp\ui.log` (desktop app). Blendermp requires Blender 4.2 LTS or newer;
on a PC without it, the installer offers to install Blender 5.3 automatically.

## Security

Read [SECURITY.md](SECURITY.md). There is no custom cryptography: TLS 1.3 does
the transport, certificates are pinned, and the host validates every action.
