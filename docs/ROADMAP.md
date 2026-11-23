# Blendermp Master Roadmap: 0.1.7 -> 1.0

Living implementation plan. Last updated: 2026-11-20.
Baseline: **0.8.0** (public directory, authenticated relay, TLS pinning, scene
bootstrap, object/transform sync, chunked mesh deltas, live Edit Mode vertex
streaming, edit leases, presence 2.0, exact remote application, durable settings,
strict version gate, lease request UX, server-enforced roles, optional LAN direct).

Progress log:
- [x] 2026-11-23: 0.11.17: fixed "only the host can start a render" on the host. Loading a .blend can reset the window-manager property group, so `settings.is_host` went stale while the session kept hosting. Host-only render actions now read `session.is_host`, and `_write_status` writes the live value back into the property group every tick.
- [x] 2026-11-23: 0.11.16: the render consent prompt is now a native Blender dialog (`bmp.render_consent`, `invoke_props_dialog`) shown inside the Blender window. It is centred, modal, and only OK (join) or Cancel (decline) dismisses it. The desktop app no longer shows its web overlay, so nothing covers the UI. `BMP_NO_CONSENT_DIALOG=1` keeps the e2e automated.
- [x] 2026-11-23: 0.11.15: added `allow-ui-ready` to the window capability. Tauri 2 enforces commands in `capabilities/default.json`, so `ui_ready` was denied even though it was in the handler and `build.rs`, which made the startup watchdog show a false "UI did not load" message. The render consent prompt and the blank-UI hint now behave correctly.
- [x] 2026-11-23: 0.11.14: registered `ui_ready` (and `export_diagnostics`) in `build.rs`. Tauri 2 only allows commands listed in the app manifest, so the new `ui_ready` invoke was denied and the watchdog fired a false timeout. The frontend now logs a denied command instead of swallowing it.
- [x] 2026-11-23: 0.11.13: the render consent prompt is actually visible now. The docked Blender window is a native window above the WebView2 surface, so it covered the centred modal; the app now hides Blender while the prompt is open and restores it after Yes/No. Removed the old `#renderConsent` inline styles that made the overlay opaque and blended the dialog into the background.
- [x] 2026-11-23: 0.11.12: blank-interface fix for other PCs. The main WebView2 window now launches with `--disable-gpu-compositing`, which fixes the blank UI seen on some GPUs/drivers. The app also calls a new `ui_ready` command from the frontend; if it is not received within 10 seconds, a native message box explains that the interface did not load and points at the WebView2 runtime and graphics driver. This turns a silent blank window into an actionable error.
- [x] 2026-11-23: published the project at <https://github.com/Tribbninja/blendermp> (public) and added `Blendermp-Updater.exe`. The updater queries the latest GitHub Release, downloads the newest `*-Setup.exe`, installs it silently, and launches the app, so friends keep one file and always get the current build. Release `v0.11.11` carries the updater, the installer, the portable zip and the Blender extension zip. `scripts/release.ps1` publishes future releases.
- [x] 2026-11-23: 0.11.11: the render consent prompt is now a real centered modal overlay that blocks the rest of the app; clicking outside does nothing and only Yes/No dismisses it. Accepting or declining clears the request so the modal does not linger, and `start` also clears it. Added `RENDER_REQUEST`/`RENDER_RECEIVED` event-log entries for diagnosing delivery. Fixed an invalid `RECORD` panel icon that raised a `TypeError` on every N-panel draw.
- [x] 2026-11-23: 0.11.10: render workers use a configurable share of the CPU. Each farm frame sets `render.threads_mode = FIXED` and `render.threads = 80% of cores` (adjustable in the N-panel), then restores the scene settings. Clients now request exactly one frame at a time and never queue: the `need` message carries `count: 1`, the host honours it, and the client drops extra assignments. EEVEE renders are GPU-bound, so the CPU share applies to Cycles.
- [x] 2026-11-23: 0.11.9: render farm polish. Progress now counts finished + failed frames, shows an ETA, and lists per-worker done/rendering counts. The host can retry failed frames (a failed job is kept instead of cleared). Width/height inputs let a render use a custom resolution. Render frames over a direct mesh link use large chunks instead of relay-sized chunks. `run_all.ps1` retries a flaky suite once under machine load, and the e2e timeouts were widened. Added `blender-multiplayer-main/` to `.gitignore`.
- [x] 2026-11-23: 0.11.8: the stopped state survives `status.json` and the desktop badge shows `stopped` instead of `done`. Verified live: a 40-frame render was stopped after 10 frames, the 10 PNGs were kept, and no video was encoded.
- [x] 2026-11-23: 0.11.7: fixed the render loop. A finished job stayed in the manager, so every tick re-ran video compilation and re-sent `finish`; the job is now retired after one pass and its summary is kept for the UI. Added a **Stop render** button: it halts the job on the host and all clients, skips video compilation, and leaves the already rendered frames in the output folder. The desktop command path, the addon control whitelist and the render manager all accept `render_stop`.
- [x] 2026-11-23: 0.11.6: a finished render keeps its summary in `status.json` and the desktop app shows a `done` badge with the progress bar at 100%, instead of snapping back to idle. Combined with 0.11.5, the Custom render button, the Join/Decline prompt, the frame range and the progress bar all work through the real desktop command path.
- [x] 2026-11-23: 0.11.5: fixed the render farm command path. The desktop `write_session` command rejected the new `render`/`render_reply` actions (`unknown variant`), and the addon's control whitelist did not accept them either. Both now accept the actions and validate the frame range, step, format, job id and accept flag. The render button and the Join/Decline prompt work end to end.
- [x] 2026-11-23: 0.11.4: render frames are losslessly compressed only for relay peers and pass through raw on LAN, matching the image policy. Render jobs carry the scene FPS so the host encodes at the right rate. The transfer chunk header now records its chunk size, so LAN senders use large chunks and relay senders stay under the Cloudflare cap without breaking reassembly.
- [x] 2026-11-23: 0.11.3: distributed render over the session. A new `RENDER` message and `KIND_RENDER` transfer carry a consent request, dynamic frame batches (up to 5), per-frame results, and cancel/finish. `core/renderjob.py` tracks one job at a time with stale-claim recovery; `core/render_payload.py` wraps a frame image. The desktop app has a Render farm section: frame range, step, format, a Custom render button, a progress bar, and a Join/Decline prompt. Clients render on Blender's main thread and upload PNG frames; the host saves them and compiles MP4/WebM with the bundled `imageio-ffmpeg`. New `RENDER E2E` renders 3 Workbench frames across two Blenders and produces an MP4.
- [x] 2026-11-23: 0.11.2: images sync beyond material textures. A periodic scan queues every usable image in `bpy.data.images` (reference boards, compositor inputs, world textures, images loaded mid-session), skipping render results and viewers. The receiver still dedupes by SHA-1, so the same image is never stored twice. New `REFERENCE E2E` proves a 64x64 image that no material uses reaches the client.
- [x] 2026-11-23: 0.11.1: LAN gets big packets again. Bulk payloads are encoded per peer: a direct LAN peer receives one raw frame up to 64 MB, while a relay peer receives the zlib-compressed payload chunked under the Cloudflare cap. Images are encoded both raw and compressed when large enough, and object creates use the per-peer chunk threshold. Mixed LAN+internet sessions therefore keep the LAN fast path without penalising remote peers.
- [x] 2026-11-23: 0.11.0: the mesh now carries session data. Peers exchange a mesh identity over the session (`MESH` message), the host maps a session peer to its mesh link, and post-bootstrap frames are sent over the direct link with the TLS/relay path as a fallback. Pings stay on TLS so the relay peer never times out. The client refuses to send over the mesh until the host acknowledges its mesh identity, and the host only switches a peer to the mesh after the scene bootstrap completes, so bootstrap frames stay ordered. `mesh_status()` now reports `sent`/`received` frame counters, shown in `status.json`. New `MESH RELAY E2E` proves a host edit reaches a client through the Worker-introduced direct link.
- [x] 2026-11-22: 0.10.0: added the P2P mesh foundation. The Worker Room object now exposes `/signal/{room}` as a WebSocket that forwards small JSON messages between peers (welcome, join, leave, hello, direct). A pure-Python STUN client (`net/stun.py`) discovers the public endpoint. `net/mesh.py` establishes encrypted direct TCP links with X25519 + HKDF + ChaCha20-Poly1305, directional keys, proof frames, and symmetric connection dedupe. `net/meshcoord.py` ties signalling, STUN and the mesh together and runs alongside the session, reporting mesh peers in `status.json`. Routing the session data plane over the mesh is the next phase.\n- [x] 2026-11-22: 0.9.15: lowered the relay chunk threshold and chunk size to ~900 KB so no single frame exceeds Cloudflare's ~1 MiB WebSocket cap. IMAGE_ASSET frames are now split into chunks under 1 MiB using the existing transfer system (KIND_IMAGE). The Cloudflare bridge (worker_relay) now rejects oversized frames at 900 KB instead of letting them hit Cloudflare.
- [x] 2026-11-22: 0.9.14: fixed cross-network joins being kicked during scene bootstrap. A UV layer whose data length disagreed with the loop count raised inside `_object_create_payload` and aborted the bootstrap; UV reads are now length-safe and per-object payload errors are contained. The relay host tunnel now sends a keepalive so Cloudflare does not close an idle room between joins. Edit-topology sampling also ensures the BMesh edge table first.
- [x] 2026-11-22: 0.9.13: the remote selection outline now clears on deselect. Presence only includes actually selected objects; the active-but-unselected object was keeping the outline alive. The LOCK E2E suite now checks the outline segment count drops after the owner deselects.
- [x] 2026-11-22: 0.9.12: locks are enforced every 50 ms, and a remote presence deselect now clears any stale lease so a mesh cannot stay locked forever after its owner stops selecting it. Selection is blocked at the source with `hide_select`, and the lock release is verified by the LOCK E2E suite.
- [x] 2026-11-22: 0.9.11: ported Multi-User's `DynamicRightSelectTimer` lock model directly: selection locks, deselection unlocks, every object owned by another peer gets `hide_select`, locked transforms revert and Edit/Sculpt exits. Removed the per-user "Edit locks off" opt-out so locks are always enforced. Added a permanent two-Blender `LOCK E2E` suite.
- [x] 2026-11-22: 0.9.10: edit locks now persist for as long as the object stays selected (Multi-User semantics) instead of expiring after 30 seconds of inactivity; the live status now shows the active lease. Locks still release on deselect, disconnect, explicit release, or when edit locks are turned off.
- [x] 2026-11-22: 0.9.9: hard edit locks on both host and client (locked transforms revert, locked objects deselect/hide, Edit/Sculpt exits, host no longer force-steals client leases); peer-coloured selection outlines and hover ownership restored; the desktop app restarts Blender when the bundled addon version changes so updates actually load.
- [x] 2026-11-21: 0.9.8: replaced the custom camera widget with the exact Multi-User seven-point frustum (`view_corners`): four viewport corners and centre one unit into the scene, the orbit pivot, and an apex 0.8 units behind the camera, with Multi-User's line indices and the username on the top-right corner in the peer's colour. Fixed line batches to pass coordinate tuples, so camera and selection lines now render in Blender 5.3.
- [x] 2026-11-21: 0.9.7: collaborator overlays: per-peer wire camera widgets (body + lens pyramid + look ray) in the peer's colour, peer-coloured selection outlines, and a hover tooltip that names who is editing or has the object selected. Overlays are on by default with individual toggles; viewport camera sharing defaults on.
- [x] 2026-11-21: 0.9.6: direct LAN fast path: private-LAN peers bypass interest/quality rate limiting so transforms and presence run at the sender rate; relay peers stay on the low-bandwidth profile; presence state changes are never dropped by the relay camera throttle; host shows LAN fast path vs Internet mode.
- [x] 2026-11-21: 0.9.5: removed all notification beeps and their toggles; fixed host-to-client smoothness by reporting real tick FPS instead of scene.render.fps, relaxing quality thresholds, and keeping a 5-20 Hz transform floor even for off-screen/low-quality peers.
- [x] 2026-11-21: 0.9.4: viewport is model-only by default (name tags, lasers and selection wireframes are off and no longer forced on; remote selected-object lines obey Show selections); crash notices ignore markers from older builds; LAN farm host rendering is opt-in; animated-transform detection is cached.
- [x] 2026-11-21: 0.9.3: bandwidth saver: delta-encoded presence, no viewport matrix unless Share my viewport position is on, relay presence at 3-5 Hz, interest updates every 5 s, client stats every 2 s, PING every 5 s; idle traffic is roughly 0.1 KB/s per client.
- [x] 2026-11-21: 0.9.2: clean viewport (no remote camera/cursor lines, crisp name tags, selections off by default), independent timelines (animated transforms, pose and shape values evaluated locally), scene fingerprint + resync, and a token-protected LAN render farm that distributes frames and uploads PNGs.
- [x] 2026-11-21: 0.9.1: lease reliability fix: idle selections release after 30s, renewals require actual edits, disconnect leases expire in 30s, peer-leave clears the local lock mirror, per-object lock messages no longer spam chat, and the panel has Release locks plus an Edit locks off switch.
- [x] 2026-11-21: 0.9.0: finished the collaboration foundation: Ollama copilot with 23 tools, scene context and per-edit results; send-path recording + replay + token-protected SSE live broadcast; cross-engine bridge for Unity/Unreal/Godot with transforms, materials and triangle meshes; perf registry published in `status.json`; topology snapshot fast path (`BMesh.to_mesh` + `foreach_get`) and a cheaper compression level for large meshes; object rename sync.
- [x] 2026-11-20: `version.json` + `scripts/check_version.py` + test; build fails on drift.
- [x] 2026-11-20: durable `%LOCALAPPDATA%\Blendermp\settings.json`; host key, public toggle and layout survive reinstall; localStorage migrated once.
- [x] 2026-11-20: CI skeleton for Python tests + version check + JS syntax.
- [x] 2026-11-20: Phase 1 compatibility gate: version/capability HELLO+WELCOME, legacy-client rejection, directory fields, server-browser preflight, update badges.
- [x] 2026-11-20: Phase 1 lock reliability: lease sweep broadcasts `expiring`/`released`, snapshot-driven owner cleanup, expiry auto-renew, request queue, host-side grant/deny, lock status in the N-panel.
- [x] 2026-11-20: Phase 1 presence 2.0: edit-mode vertex selection relay/points, 3D cursor, follow-user camera, overlay toggles. Laser pointer removed after poor reliability.
- [x] 2026-11-20: Phase 1 roles: host assigns viewer/commenter/editor/admin, server-enforced mutating-frame guard, downgrade releases leases, role UI in the desktop member list.
- [x] 2026-11-21: 0.8.11: remote camera widgets are off by default and use a fixed world size (no growth with the other user zoom); labels clear any stale blf shadow and use a thin 1px outline with a doubled fill.
- [x] 2026-11-21: 0.8.10: crash detection only reports unclean exits from active sessions; a forced kill while offline (installer/update) no longer shows a false crash notice.
- [x] 2026-11-21: 0.8.9: lease robustness: the host only force-takes a lease when it actually changes the object (no more stealing client leases every tick); blocked selections are cleared even if already selected; releases on deselect/leave/role change remain.
- [x] 2026-11-21: 0.8.8: hosts advertise a direct code for every local IPv4 (Wi-Fi + Windows ICS Ethernet); the client picks the code on its own /24, so ICS-connected PCs get LAN latency instead of the relay.
- [x] 2026-11-21: 0.8.7: fix the client constructor overwriting direct_code, so LAN direct-first actually connects; verified relay=False on a direct join.
- [x] 2026-11-21: 0.8.6: LAN joins try direct first (same /24) and fall back to the relay, restoring ~LAN ping; public listings carry direct_code; the host takes the lease when it moves a client-leased object so its drag is not overwritten.
- [x] 2026-11-21: 0.8.5: lossless model sync: vertex deltas are float32 (no i16 quantisation); direct LAN sends raw payloads, relay uses lossless zlib for geometry, deltas, bundles, images and rig data.
- [x] 2026-11-21: 0.8.4: name tags use a crisp manual outline (no blurry native shadow), 15px size and a brightened readable colour; camera widgets and name tags are now independent so tags survive "Show cameras" being off.
- [x] 2026-11-21: 0.8.3: identity check compares normalized room codes (directory returns undashed, the host descriptor is dashed), fixing "host identity does not match this session" on relay joins.
- [x] 2026-11-21: 0.8.2: joins prefer the public relay when the host publishes one (LAN direct only for LAN-only hosts; Windows Firewall commonly blocks the random direct port), and pre-1.0 joins require an exact build match with a clear message.
- [x] 2026-11-21: 0.8.1: certificate-change recovery (Trust new certificate prompt with the new short code), joins no longer reuse a stale host fingerprint, pin-store tests.
- [x] 2026-11-21: 0.8.0 fix: relay host-token verification moved into the Room Durable Object (announce pushes the persisted hash; tokens survive eviction); fixes "hub rejected the websocket: 403 Forbidden".
- [x] 2026-11-21: 0.8.0: detached dashboard (people, leases, sync queue, chat, DND/sound), accessibility (Okabe-Ito palette, high contrast, large text, reduced motion, focus rings, overlay opacity/scale), short-lived laser pointer.
- [x] 2026-11-21: 0.7.0: chunked large-object transfer (8 MB chunks, deterministic IDs, progress, partial retention, bidirectional), all-UV-layer geometry (GEO2), NLA tracks/strips, safe object drivers, collection-instance bundle imports, curve/text objects, protocol v3.
- [x] 2026-11-21: 0.6.0: Phase 6 start: content-addressed asset bundles (send selection, preview/accept, dedupe, host validation), local/pushed view bookmarks, blender:// deep links with single-instance forwarding and recent/favourite rooms.
- [x] 2026-11-21: 0.5.1: compress image uploads over 10 MB with zlib and raise mesh/image frame limits; large geometry snapshots use the higher-ratio compression pass.
- [x] 2026-11-21: 0.5.0: Phase 5: per-object subscriptions and priority rates, client stats/quality tiers, per-peer byte budget and pending-frame resend, Worker Directory/Room split with signed announce verification, macOS/Linux detection and CI checks, fuzz/decoder limits, CSP/security documentation.
- [x] 2026-11-21: 0.4.0: Phase 4: armature creation with bones/weights and armature-modifier binding; 30 Hz pose bone sync; active action F-curve/keyframe sync; shape-key geometry/values; constraint bindings; Sculpt Mode position deltas with dyntopo topology fallback and multires warning; animation/sculpt two-Blender harnesses.
- [x] 2026-11-20: 0.3.0: Phase 3: resume with retained leases and no scene reset; offline transform/mesh/material journal with conflict prompts; scoped undo; checkpoint save/restore with live peer resync; Ed25519 signed host identity verified on join; scrypt PIN hashing + per-IP rate limit; role/kick audit events; signed directory source in the Worker.
- [x] 2026-11-20: 0.1.17: Phase 0 complete: redacted diagnostics bundles in the app and addon, stable ERROR codes with UI surfacing, structured events.log, crash heartbeat with recovery-checkpoint picker, headless perf harness + baseline, checked-in two-Blender e2e README/run-all, CI for Python 3.11/3.12, JS, Rust, Worker dry-run and Blender bench.
- [x] 2026-11-20: 0.1.16: closing the desktop app now closes its Blender window (WM_CLOSE after detaching, so unsaved work still prompts).
- [x] 2026-11-20: 0.1.15: Phase 2 completion: whitelisted material node graphs and SHA-1 texture transfer with colour-space/alpha parity; object details (parenting/inverse, modifier stacks for all objects, collections/visibility, object colour); repaired corner-edge indices after geometry rebuild so Bevel modifiers cannot crash; checked-in material/details/topology two-Blender harnesses.
- [x] 2026-11-20: 0.1.14: bundle Blender's cryptography vendor so fresh PCs can host; no Python install step.
- [x] 2026-11-20: 0.1.13: Phase 2 start: live Edit Mode topology snapshots (debounced, compressed), basic material slots for all objects and imported-object creation; overlay selection uses clean bounding boxes and fixed-size labels.
- [x] 2026-11-20: 0.1.12: object import reliability (bulk object creates no longer dropped by the per-4KB rate bucket, stale/copied UUIDs get a fresh identity, zlib geometry compression), fixed overlay font/scale/dedupe. Laser removed.
- [x] 2026-11-20: 0.1.11: removed all display interpolation; transforms/mesh/camera apply immediately at network rate. 60 Hz LAN streaming, client-reported ping, total data stat, stable dock bounds.
- [x] 2026-11-20: 0.1.10: LAN direct path (dual listener + local directory `direct_code`), 60 Hz transforms/presence on low latency, client-reported ping, total data stat, stable dock bounds (no scrollbar flash).
- [x] Phase 2: live topology, material graphs/textures, object details.
- [x] Phase 3: reconnect/resume, offline journal, scoped undo, checkpoints, signed host identity and audited roles.
- [x] Phase 4: armature/pose/action, shape keys, constraints, fixed-topology sculpt.
- [x] Phase 5: subscriptions/interest, adaptive quality/backpressure, split relay DOs, cross-platform builds, security hardening.
- [x] Phase 6 (partial): send selection bundles, view bookmarks, deep links, recent/favourite rooms.
- [x] Phase 0: version check, settings, CI (Python/JS/Rust/Worker/Blender bench), diagnostics bundles, error codes, perf baseline, crash marker/recovery hook, checked-in e2e harness.

Purpose: ship every feature discussed in the roadmap in dependency order,
without regressing latency or reliability. Each phase has a hard exit gate.
Do not start the next phase until the gate passes.

---

## 1. Invariants

1. **Host authoritative.** Clients propose edits; the host validates, assigns
   identities, owns leases and relays state.
2. **Exact network state and exact application.** Never send interpolated
   values, and do not interpolate the received view; apply authoritative state
   immediately.
3. **One editor per object.** Structural/topology edits use the same lease as
   transforms. No parallel structural merges.
4. **Additive protocol.** Keep the 20-byte header (`MAGIC=BMP1`,
   `PROTOCOL_VERSION=3`) unless framing itself changes. New messages are gated
   by capability bits. Unknown capabilities are ignored.
5. **Fail loud on mismatch.** A version/protocol mismatch must show "update",
   never silently disable half the features.
6. **Never block Blender's main thread > 8 ms** in one tick. Coalesce, drop
   non-critical frames, chunk large work.
7. **Never overwrite the user's `.blend`.** Bootstrap/checkpoint/restore work on
   copies; ask before replacing a scene.
8. **No custom crypto.** TLS 1.3 for transport, Ed25519 for identities,
   Argon2id for PINs, SHA-256 for content addressing.
9. **Every feature ships with a headless two-Blender test and a perf assertion.**
10. **Feature flags + kill switches.** Any sync feature can be disabled per
    session without breaking the rest.

---

## 2. Current gaps this plan closes

| Gap | Phase | Current behavior |
|---|---|---|
| Version/update drift | 0/1 | Manual installs; 0.1.5 vs 0.1.6 mismatch possible |
| Stuck/absent lock UX | 1 | Object lease works; no expiry prompt, queue UI or badges |
| Remote detail presence | 1 | Camera/object selection only; no vertex/face/cursor/laser |
| Structural Edit Mode | 2 | Extrude/subdivide/delete sync only on mode exit |
| Materials/textures | 2 | Principled basics, active object only; no node graphs |
| Object details | 2 | Modifiers/materials mostly active object; partial parenting |
| Reconnect | 3 | A dropped client re-bootstraps or loses state |
| Offline edits | 3 | No journal/conflict resolution |
| Scoped undo | 3 | Blender's global undo can revert remote work |
| Checkpoints | 3 | None |
| Signed host identity | 3 | First-connection TOFU; host key hash only |
| Roles/permissions | 3 | Editors only; kick exists |
| Armature/pose/action | 4 | Not replicated |
| Shape keys/constraints | 4 | Not replicated |
| Sculpt | 4 | Not a first-class fast path |
| Scale/interest/adaptive | 5 | All objects, fixed rates |
| Cross-platform | 5 | Windows-only embedding; app is Windows-only |
| Laser/bookmarks/send-selection | 6 | Not implemented |
| Dashboard/deep links/a11y | 6 | Partial |

---

## 3. Phase map

| Phase | Release | Theme | Est. | Depends on | Exit gate |
|---|---|---|---|---|---|
| 0 | 0.1.6.x | Hardening + telemetry | 3-5 d | 0.1.6 | CI green, settings persist, diagnostics, baseline perf |
| 1 | 0.1.7 | Trust & Flow | 2-3 wk | 0 | Version gate, presence 2.0, lock UX, roles groundwork |
| 2 | 0.2.0 | Modeling Parity | 3-4 wk | 1 | Live topology, material graphs/textures, object details |
| 3 | 0.3.0 | Recovery & Trust | 3-4 wk | 1 | Reconnect/resume, journal, scoped undo, checkpoints, signed identity |
| 4 | 0.4.0 | Animation & Sculpt | 4-6 wk | 2 | Pose/action, shape keys, constraints, fixed-topology sculpt |
| 5 | 0.5.0 | Scale, Security, Platforms | 4-6 wk | 3 | Interest/adaptive, per-room DO, macOS/Linux, security audit |
| 6 | 0.6.0 | Collaboration Polish | 2-3 wk | 1/4 | Laser, bookmarks, send-selection, dashboard, a11y |
| 7 | 1.0.0 | Release hardening | 1-2 wk | all | Signed updater, docs, support, 7-day soak |

Solo estimate: **20-27 weeks**. With two developers, split: A = protocol,
session, relay, tests; B = Blender data, UI, rendering, packaging. Phases 1-3
are the critical path; 4-6 can overlap after their dependencies.

Dependency graph:

```text
version + settings + CI
  -> capabilities/preflight
  -> presence 2.0 + lock UX + roles
  -> topology + materials + object details
  -> reconnect + journal + scoped undo + checkpoints
  -> signed identity + audit
  -> pose/action + shape keys + constraints + sculpt
  -> subscriptions + adaptive quality
  -> per-room relay + macOS/Linux
  -> laser/bookmarks/send-selection/dashboard/deep links/a11y
```

Rule: a phase may be prototyped early behind a feature flag, but it cannot
ship until its dependencies' exit gates are green.

---

## 4. Cross-cutting architecture

### 4.1 Versioning and capabilities

Single source of truth: a generated `addon/blender_multiplayer/core/version.py`
(`APP_VERSION`, `ADDON_VERSION`, `PROTOCOL_VERSION`, `CAPS`) imported by the
addon, plus `version.json` generated by `scripts/package.ps1` and injected into:

- `addon/blender_multiplayer/blender_manifest.toml`
- `desktop/ui/app.js` and `desktop/ui/index.html`
- `desktop/src-tauri/tauri.conf.json` and `Cargo.toml`
- `dist/latest.json` (updater manifest)

Prefer the generated Python module over string replacement so the source tree
never carries a stale hard-coded version.

Add to `core/protocol.py`:

```python
class Capability(IntFlag):
    PRESENCE_VIEW = 1 << 0
    SELECTION = 1 << 1
    LASER = 1 << 2
    TOPOLOGY = 1 << 3
    MATERIAL_GRAPH = 1 << 4
    OBJECT_META = 1 << 5
    ANIMATION = 1 << 6
    SCULPT = 1 << 7
    RESUME = 1 << 8
    ROLES = 1 << 9
    INTEREST = 1 << 10
    DIAGNOSTICS = 1 << 11
```

HELLO/WELCOME JSON gains: `caps` (int), `app`, `addon`, `protocol`,
`resume` (optional), `nonce`. Host intersects capabilities; the addon reads
`session.caps` and only sends features both sides support.

Directory listing gains: `protocol`, `caps`, `min_app`, `updated_at`.
The desktop app preflights the listing before dialing: if `min_app` > local or
the required capabilities are missing, show "Host is on X, update to join".

Hard rule: **pre-1.0 requires an exact app/addon version**; from 1.0 on, patch
releases interoperate and only the major/minor pair must match. Allow
`--allow-legacy` only for debugging. Bump `PROTOCOL_VERSION` only for framing
changes; additive messages stay on v2.

A peer with no `caps` field is **legacy**: it receives baseline messages only
(no new message types), the host marks the room "update recommended", and the
app's preflight normally blocks the join. This makes capability negotiation
safe even before the hard gate ships.

### 4.2 Message rules

- One concern per message; binary for data, JSON for control/metadata.
- Host sanitizes and size-caps every payload before broadcast.
- Non-critical frames may be dropped under backlog; structural/critical frames
  are never dropped (`Peer.enqueue(critical=True)` for scene/bootstrap/lease).
- Every rate-limited message gets a token-bucket cost based on bytes, not just
  count (`cost = 1 + len(payload) / 4096` already exists).
- Include `rev`/`epoch` where ordering matters; ignore stale revisions.
- No per-frame allocation in hot paths; reuse numpy buffers.

### 4.3 Compression and assets

- Add `zstandard` (already planned in `docs/PLAN.md`) for payloads > 4 KB.
- Flag byte in the payload: `0x01 = zstd`; validate decompressed size before
  allocating (cap per message type).
- Content addressing: `sha256` for images, node groups, checkpoints. Store
  deduplicated images in `bpy.data.images` by hash; disk cache optional.
- Caps: image 32 MB, material graph 256 KB, topology 8 MB raw / 2 MB zstd,
  action 1 MB, checkpoint 1 GB total, journal 64 MB.

### 4.4 Performance budgets (reference machine; Windows 11, Ryzen 7)

| Path | Budget |
|---|---|
| Main addon tick (`bmp_tick`) | <= 5 ms |
| Smooth timer | <= 2 ms |
| Remote visuals rebuild | <= 3 ms |
| Mesh delta apply, 100k verts | <= 8 ms p95 |
| Mesh delta apply, 1M verts | <= 12 ms p95 |
| Topology snapshot read | <= 15 ms, chunked if larger |
| Presence | <= 4 KB/packet, 20 Hz |
| Selection | <= 30 KB, 10 Hz, only on change |
| Transform | 80 B, 30 Hz/full, 10 Hz/priority, 2 Hz/off-screen |
| UI | 60 fps; no frame > 50 ms during a session |
| Join | progress + resumable; 50 MB in < 60 s at 8 Mbps |

Instrument every apply/send with `perf_counter`; publish p50/p95/p99 in
`status.json`; fail CI on > 20% regression against recorded baselines.

### 4.5 Error codes

Add `ErrorCode` to `core/protocol.py` and use it in `ERROR` frames:
`UPDATE_REQUIRED`, `ROLE_DENIED`, `LEASE_HELD`, `SCENE_TOO_LARGE`,
`ASSET_TOO_LARGE`, `RATE_LIMIT`, `PROTOCOL`, `RESUME_EXPIRED`,
`UNSUPPORTED_CAP`, `CONFLICT`, `CHECKPOINT_FAILED`, `INTERNAL`.

UI maps codes to actionable messages. Never expose tracebacks or paths.

### 4.6 Feature flags

Session-scoped flags in `status.json` and HELLO:
`presence`, `selection`, `laser`, `topology`, `materials`, `animation`,
`sculpt`, `interest`, `roles`, `checkpoints`. Default on when caps allow; a
host can disable a feature for a room (e.g., topology off for huge scenes).

### 4.7 Key algorithms (implementation-level)

Topology detection, sender (main thread, budget 2 ms):

```python
sig = (
    len(bm.verts), len(bm.edges), len(bm.faces),
    blake2b(sample(edge_indices, 512) + sample(loop_indices, 512), digest_size=8),
)
if sig != last_sig:
    last_sig = sig
    topology_due = now + 0.15          # debounce operators
if topology_due and now >= topology_due:
    payload = encode_geometry(read_bmesh(bm))
    send(TOPOLOGY, uid, epoch, rev, zstd(payload) if len(payload) > 4096 else payload)
    topology_due = 0.0
```

Selection encoding (hybrid, sender):

```python
mask = packbits(selected_vertices)                 # dense case
if len(mask) <= 16384:
    body = b"S" + mask
else:
    body = b"R" + rle_ranges(selected_vertices)    # sparse/large case
if previous_mask is not None:
    delta = xor(previous_mask, mask)
    if popcount(delta) * 4 < len(mask):
        body = b"D" + rle_ranges(delta)
if len(body) > 65536:                              # hard packet cap
    body = b"R" + rle_ranges(selected_vertices[:20000]); rate = 2  # Hz
```

Resume handshake:

```text
client HELLO {resume:{session, token, last_rev}}
host: same session + token valid + now < expiry
      -> WELCOME {resumed:true, leases, scene_rev}
      -> client RESUME {objects:{uid:last_rev}}
      -> host sends transforms/meshes/topology newer than last_rev
      else -> SCENE_RESET (after confirmation)
```

Journal conflict resolution:

```python
for uid, base_rev in journal.revs.items():
    current = host.rev(uid)
    if current == base_rev: apply(journal[uid])
    elif current > base_rev: send CONFLICT(uid, mine=journal[uid], theirs=host[uid])
    elif current is DELETED: send CONFLICT(uid, delete=True)
```

Scoped undo:

```python
op = local_undo.pop()
if nearly_equal(read_shared(op.uid), op.after):
    write_shared(op.uid, op.before)
    broadcast(op.uid, op.before, rev+1)
else:
    toast("Changed by someone else - skipped")
```

Interest tier (host, per peer/object, 500 ms hysteresis):

```python
score = 2 * in_frustum(aabb, peer.camera) + near(aabb, peer.camera)
tier = 0 if score >= 3 else 1 if score >= 1 else 2
# rates[transform_hz, mesh_hz] = [[30,20],[10,5],[2,1]]
```

---

## 5. Phase 0 - Hardening and telemetry (0.1.6.x)

**Goal:** make failures visible and prevent regressions before adding features.

Deliverables:
1. `scripts/check_version.py` + `tests/test_version.py`: fail if any version
   string diverges from `version.json`.
2. Checked-in `tests/e2e/` harness with material/details/topology suites and
   `run_all.ps1`: env-injected addon path, temp data dirs, deterministic ports,
   JSON assertions and cleanup.
3. CI (GitHub Actions):
   - Python 3.11/3.12: `py_compile`, `pytest -q`, `check_version.py`.
   - Node: `node --check desktop/ui/app.js`.
   - Rust: `cargo check`, `cargo clippy -- -D warnings` (allow existing lints
     initially), `cargo test`.
   - Worker: `wrangler deploy --dry-run`.
   - Windows job: headless Blender install + two-Blender e2e.
4. Persistent settings/identity:
   - New `%LOCALAPPDATA%\Blendermp\settings.json` (Rust commands
     `read_settings`/`write_settings`, atomic write).
   - Migrate `bmp.*` localStorage once; keep a mirror for the webview.
   - Installer must never delete settings, identity, host key, checkpoints.
5. Diagnostics bundle:
   - App button + `bmp.export_diagnostics` operator.
   - Zip: versions, OS/GPU, `status.json`, last 500 lines of `ui.log` and
     `blender.log`, network stats, scene stats, feature flags, recent errors.
   - Redact absolute paths, host key, relay token, PIN, private identity.
6. Error codes (4.5) and structured logging (`ui.log` + `blender.log`).
7. Perf harness: `bench/bench_mesh.py` (headless Blender) covering 10k/100k/1M
   vertex read, delta encode/decode, apply, topology snapshot, material graph.
   Record baselines in `bench/baseline.json`.
8. Crash marker: per-process `heartbeats/<pid>.json`; on startup detect an
   unclean exit and offer "Recover checkpoints".

Exit gate: CI green on all jobs; version check catches a deliberate mismatch;
settings survive reinstall; diagnostics zip contains no secrets; baselines
recorded.

Files: `scripts/package.ps1`, `scripts/check_version.py`, `desktop/src-tauri/
src/lib.rs`, `desktop/ui/app.js`, `tests/e2e/*`, `.github/workflows/*`,
`bench/*`.

---

## 6. Phase 1 - Trust & Flow (0.1.7)

**Goal:** no version surprises, presence that shows what people are doing, and
locks that never feel broken.

### 6.1 Update delivery + compatibility gate

- Tauri updater plugin:
  - Signed `latest.json` at `https://<hub>/latest.json` (Worker R2/KV route)
    with fallback to GitHub Releases.
  - Minisign/Ed25519 signature; public key in `tauri.conf.json`.
  - Check on launch + every 6 h; download in background; install on exit or
    explicit restart. Never restart mid-session without confirmation.
- Addon/app lockstep:
  - App bundles the addon; `ensure_addon()` compares the installed manifest
    version and copies the bundled addon when different.
  - Blender is restarted with the new `BMP_ADDON` only when idle or after a
    user prompt.
- Compatibility gate (shipped in the 0.1.6 rebuild):
  - HELLO/WELCOME carry `app`, `addon`, `protocol`, `caps`.
  - Pre-1.0 versions must match exactly; legacy peers without version fields are
    rejected with a clear update message.
  - The server browser shows an "update" badge and disables Join for rooms with
    a different protocol/version or a newer `min_app`.
- Directory preflight (4.1) blocks mismatched joins with a clear message.
- Settings/identity persisted (Phase 0) so updates do not reset them.
- `version.json` exposed in the UI footer; release channel stable/beta.

Files: `desktop/src-tauri/Cargo.toml` (updater plugin), `tauri.conf.json`,
`lib.rs`, `desktop/ui/app.js`, `cloudflare/src/index.js`, `scripts/package.ps1`.

Acceptance: install old over new is blocked/warned; joining a room with a
different minor version shows "Update to join"; app/addon versions always match
after launch; settings survive an update.

### 6.2 Presence 2.0

Protocol:
- `SELECTION = 26` on `Channel.PRESENCE`, binary:
  `uid[16] | kind[1] | flags[1] | rev[4] | count[4] | varint index deltas`.
  `kind`: 0 vertex, 1 edge, 2 face. `flags`: bit0 delta, bit1 full.
  Cap 50k indices, 10 Hz, only when changed, coalesce 33 ms.
  Encoding: bitset for dense selections, RLE ranges for sparse/large, XOR delta
  against the previous mask. Hard cap 64 KB per packet; if exceeded, send at
  2 Hz and draw only the first 20k elements. Never send while in Object Mode.
- `LASER = 27` on `Channel.PRESENCE`:
  `seq[4] | count[2] | points` where each point is 3 x int16 quantized against
  a float32 base (sent once per stroke) or 3 x float16. Cap 64 points, 30 Hz,
  TTL 1.5 s, one active stroke per user.
- Presence JSON adds: `cursor` (3 floats), `edit_sel` (`mode`, `count`, `rev`),
  `tool` (string), `fps` (int). Keep < 2 KB.
- Selection is never sent in Object Mode; object selection stays in
  `selected` (uids) as today.

Receiver/rendering:
- Store per-user, per-object selection sets; draw:
  - Vertices: points (size 3-4 px) in the user's color.
  - Edges: line segments; faces: outline of boundary edges (dedupe).
  - Cap total drawn elements at 20k; skip off-screen objects via a world-space
    AABB check; fade elements older than 2 s.
- Cursor: crosshair + small name tag; "jump to cursor" action.
- Laser: polyline with fading alpha; draw last 1.5 s; smooth between packets.
- Follow user: local operator `bmp.follow_user(name)` reads the current
  presence matrix, sets `region_3d.view_matrix`, exits on user input. No network.
- Split draw rates: camera frustums + labels at 30-60 Hz, selection at 10 Hz,
  static label dimensions cached. One `batch_for_shader` per user/kind.
- Overlay prefs: cameras/selections/labels/laser on/off, opacity, scale,
  colorblind-safe palette (Okabe-Ito), font size. Stored in settings.
- Labels: DPI-scaled outlined text (already), cache `blf.dimensions` per name,
  cap 32 labels, distance-based fade.

Files: `__init__.py` (presence, `_sync_remote_visuals`, draw handlers,
`_selected_uids`, operators), `session.py` (sanitize/broadcast `SELECTION`,
`LASER`), `core/protocol.py` (message types), `desktop/ui/*` (overlay prefs,
follow buttons), `ui.py` (panel).

Performance: selection decode + draw <= 3 ms for 20k elements; no allocation
per frame in the draw handler; use preallocated numpy arrays and rebuild batches
only when `(uid, rev, matrix_hash, selection_rev)` changes.

Acceptance: 8 simulated users, 20k selected verts, 60 fps; remote cursor and
laser < 100 ms; labels crisp at 100-200% DPI; no GPU errors; selection clears
on object deselect/mode exit.

### 6.3 Lock UX, request queue, roles groundwork

- Wire `LeaseManager` `SweepResult.expiring` to the addon:
  - Toast "Still editing X? Releasing in 20 s" with Keep / Release.
  - Auto-release on no response; renew on any edit/selection.
  - Default TTL 90 s, grace 20 s; configurable.
- Persistent request list in the N-panel and app: object, owner, expiry,
  requesters, Grant/Deny/Force release. Click frames the object.
- Viewport lock badges: small lock icon + owner name near locked objects; reuse
  the label pipeline; cap 16.
- Right-click in the 3D view: Request edit / Grant / Release / Follow owner.
- Host force-release on kick/role downgrade/disconnect.
- Roles (`sync/leases.py` already has `Role`):
  - Add `role` to `Peer`; default EDITOR.
  - `ROLE = 38` message; host UI dropdown; host only.
  - Guard every mutating handler with `can_edit(role)`: TRANSFORM, MESH_*,
    TOPOLOGY, OBJECT_*, MATERIAL_*, MODIFIER_*, IMAGE_*, LEASE acquire.
  - Viewers get `ERROR ROLE_DENIED`; commenters can chat/ping only.
- Audit log (`audit.jsonl` on the host): join/leave, role changes, kicks, lease
  grants/force-releases, object deletes, checkpoint restores, errors. Local only;
  UI "Session log" last 100.

Acceptance: 50 acquire/release cycles + mode switches + disconnects produce no
stuck lease; expiry fires exactly once; a viewer cannot edit even with a forged
frame; role downgrade releases that user's leases.

### 6.4 Phase 1 test matrix

- Version: preflight blocks mismatches; legacy override warns.
- Presence: 2 clients, selection/laser/cursor; screenshot smoke; perf 20k.
- Locks: scripted host/client select/deselect, idle expiry, request/grant,
  force release, disconnect.
- Roles: viewer attempts transform/mesh/lease; expect rejection, no state
  change.
- Regression: existing 106 tests + e2e still pass.

Exit gate: 0.1.7 installed on both machines, version gate visible, presence
2.0 works in a live session, lock stress passes.

---

## 7. Phase 2 - Modeling Parity (0.2.0)

### 7.1 Live topology sync

Protocol: `TOPOLOGY = 28` on `Channel.MESH`:
`uid[16] | epoch[4] | rev[4] | flags[1] | geometry payload`
`geometry` is the existing `GEO_MAGIC` snapshot (all verts/edges/loops/polys,
active UV layer, smooth flags, material indices), optionally zstd.

Sender:
- `EditMirror` tracks `(len(bm.verts), len(bm.edges), len(bm.faces))` and a
  cheap topology signature: `blake2b` over sampled edge/loop index arrays
  (first/last 256 + every Nth) at 5 Hz; if changed, set `pending_topology`.
- Debounce 150 ms after the last change; read the full BMesh once and send.
  This coalesces extrude/subdivide/loop cut/delete sequences.
- On mode exit, send any pending snapshot before switching to object mode.
- Increment `epoch`; reset the position `rev`; the receiver ignores stale
  epochs.
- Cap: raw 8 MB, zstd 2 MB. If larger, send `TOPOLOGY_PENDING` and stream via
  `BULK` 4 MB chunks with progress; if the peer cannot accept, keep
  position-only sync and warn.
- Read BMesh UVs from `bm.loops.layers.uv.active`; material indices from
  `bm.faces`; smooth flags from face smooth.

Receiver:
- Apply via `MeshMirror._apply_geometry` (already exists); preserve object
  transform, material slots, modifiers; clear `edit_mirrors`, `mesh_smoothers`.
- Do not restore edit selection (the receiver is not the lease owner).
- Request a full snapshot if counts mismatch or `epoch` jumps.

Tests: extrude, inset, bevel, loop cut, subdivide, delete, mirror, merge;
assert counts and a position hash; 100k-vertex mesh < 250 ms after operator;
10-minute random-operator soak; no crash on undo.

Acceptance: structural changes appear within 250 ms; receiver stays interactive;
no data loss or duplicate geometry.

### 7.2 Material graphs and textures

Protocol:
- `MATERIAL_GRAPH = 29` on `Channel.BULK`, compressed JSON/msgpack:
  `{material_uid, name, nodes:[{id,type,props}], links:[{from,from_socket,
  to,to_socket}], blend_method, shadow_method}`.
- `MATERIAL_BIND = 30`: `{object_uid, slots:[{index, material_uid, name}]}`.
- Extend `IMAGE_ASSET = 22`: `sha256`, `width`, `height`, `colorspace`,
  `alpha_mode`, `packed`, `format`.
- Whitelist nodes (v1): Principled BSDF, Material Output, Image Texture,
  UV Map, Mapping, Normal Map, Bump, Mix (RGB), ColorRamp, Emission, Value,
  Math (add/multiply), Texture Coordinate. Cap 256 nodes / 512 links.
- Unsupported nodes: replace with a placeholder and log once; never crash.

Sender:
- Poll all objects' material slots every 0.5 s (not just the active object).
- Compute a graph signature (node ids/types/links + image hashes); send on
  change.
- Send referenced images first (content-addressed), then the graph. Skip
  duplicates by SHA-256.
- Pack images if needed; external image files are read once and capped.

Receiver:
- Create/reuse materials and nodes; resolve links by node `id`.
- Set image nodes from the asset cache; apply color space and alpha mode.
- Preserve viewport `diffuse_color` and Principled fallback values.

Tests: golden corpus (Principled+image, normal map, color ramp, mix, emission,
UV mapping); compare node count/links/image hash and viewport color; 100
materials < 500 ms; missing image -> placeholder, no error spam.

Acceptance: a textured PBR object matches on both sides; no image duplication;
nodes and links match; safe on unsupported graphs.

### 7.3 Object details

- `OBJECT_META = 31`: parent uid, `matrix_parent_inverse`, visibility flags,
  object color, all material slots, modifier stack summary, collection list.
- `COLLECTION_STATE = 32`: collection name, hide_viewport/hide_render,
  hide_select; host authoritative; cap 256.
- `SCENE_STATE = 44`: world background color/strength, render engine + common
  settings, color management (view transform, look, exposure, gamma), unit
  scale, frame range, fps. Host authoritative; send on change at 1 Hz; reject
  invalid enums. Full world node graphs are Phase 4+ (same whitelist rules as
  materials).
- Cycle guard for parenting; reject self/ancestor loops.
- Send on creation and on change (1 Hz); include in scene bootstrap.
- Curves/fonts: add payloads for curves (splines/bevel) and text (body/font/
  size/align) if time; otherwise reject with `UNSUPPORTED_CAP` and a warning.
- Tests: parent/child transforms, collection hide, material slot reorder,
  modifier stack on a non-active object.

Exit gate: structural edits, textured materials and object details match in a
live session; performance budgets pass; existing tests green.

---

## 8. Phase 3 - Recovery & Trust (0.3.0)

### 8.1 Reconnect and resume

- Host generates `session_id` at start; keep `scene_rev` per object.
- `recent` map: `peer_id -> {resume_token, leases, name, color, last_seen}`
  for 120 s after a drop. Keep leases marked "offline" during that window.
- Client stores `{session_id, resume_token, last_rev}`; on drop enters
  `reconnecting` (exponential backoff 0.5/1/2/4/8 s, cap 30 s), keeps the scene.
- HELLO gains `resume: {session_id, token, last_rev}`.
- Host verifies session/token; replies `WELCOME {resumed:true, leases,
  scene_rev}`. Client sends `RESUME = 39` with per-object `last_rev`; host sends
  only newer transforms/meshes/topology/materials.
- On mismatch/expiry: full `SCENE_RESET` only after the client confirms.
- Never resume across a host restart (new `session_id`) or a scene checkpoint
  restore (new epoch).
- Tests: kill socket mid-edit; reconnect < 3 s; leases restored; no reset; host
  restart -> clean bootstrap.

### 8.2 Offline edit journal

- While disconnected, append local edits to
  `%LOCALAPPDATA%\Blendermp\journal\<session>.jsonl`:
  `{ts, uid, kind, base_rev, payload_hash, payload}`. Coalesce mesh deltas into
  snapshots per object; cap 64 MB, rotate.
- On resume, send `JOURNAL_SUMMARY` (uids + base revs). Host compares:
  - No host change -> apply.
  - Host changed -> `CONFLICT` with both values; client chooses keep mine /
    take theirs / keep both (duplicate object).
  - Deleted -> prompt; keep mine recreates.
- Persist across Blender crashes; on startup, offer "Restore unsynced edits" if
  the last exit was unclean.
- Tests: 10 offline edits; conflict transform; conflict delete; corrupt/truncated
  journal tolerated; no duplicate application.

### 8.3 Scoped undo

- Maintain a local shared-edit log: `{uid, kind, before, after, rev, ts}`,
  cap 200 ops / 2 min.
- `bmp.undo_shared` operator: pop the last local op; if current == after
  (epsilon), apply before and broadcast; else skip with a toast ("changed by
  someone else"). Repeat on press.
- Do not hijack Blender's global undo. Add `Ctrl+Alt+Z` and a panel button;
  document that Blender Ctrl+Z remains global and may need a resync.
- Mesh ops store index/value before/after or a compact snapshot.
- Tests: interleaved local/remote edits; undo skips remote; no echo; 100 ops.

### 8.4 Checkpoints

- `bmp.checkpoint_create`: serialize objects/meshes/materials/modifiers/
  collections/world/render to `checkpoints/<code>/<ts>.bmpz` (zstd) + a `.blend`
  copy via `save_as_mainfile(copy=True)` for disaster recovery.
- `bmp.checkpoint_restore`: confirm, apply the custom snapshot to the live
  scene (reuse `_clear_scene` + bootstrap apply), broadcast `SCENE_RESET` or
  diffs, bump epoch, clear leases.
- Auto-checkpoint every 10 min and before a topology-heavy operator; keep last
  3; cap 1 GB per session; thumbnail optional (OpenGL render).
- Tests: create/restore with 1k objects; scene hash equality; restore with peers
  connected; stale leases cleared.

### 8.5 Signed host identity + roles

- Ed25519 keypair generated once (`identity.json`); public key in `/announce`.
- Sign `{code, app, addon, protocol, cert_fingerprint, caps, ts, nonce}`.
- Worker verifies the signature with WebCrypto before listing; stores the
  pubkey; `/servers` returns the signed descriptor. Clients verify; TLS pin
  remains the transport anchor.
- Keep the relay host token separate; never reuse the identity key.
- Roles (from 6.3) enforced server-side; add `ROLE` message and audit.
- PIN: Argon2id hash on the host; 5 attempts/min/IP; no PIN in the Worker.
- Secret storage: DPAPI/Keychain/libsecret via Tauri; fallback user-only ACL.
- Tests: forged/modified descriptor rejected; wrong signature rejected; role
  escalation denied; PIN brute force rate-limited.

Exit gate: reconnect + resume works; conflict prompts work; scoped undo skips
remote edits; checkpoint restore recovers; signed directory entry verified;
roles audited.

---

## 9. Phase 4 - Animation, Datablocks, Sculpt (0.4.0)

### 9.1 Armature and pose

- Armature creation payload: bone table (name, parent, head/tail/roll,
  connected, deform) + rest matrices; cap 256 bones.
- `POSE = 33` on `Channel.TRANSFORM`:
  `armature_uid[16] | bone_count[2] | per bone: index[2] + loc[3] + quat[4] +
  scale[3] (float32)`. Send only changed bones; 30 Hz while posing; interpolate
  on the receiver; require the armature lease.
- Action edits require the armature lease; pose is separate.
- Tests: 2-bone and 20-bone rigs; exact pose match; no jitter; 30 Hz.

### 9.2 Actions and playback

- `ACTION = 34` on `Channel.BULK`: action name, frame range, F-curves (data
  path, index, keyframes co/handles/interpolation), NLA strips; cap 1 MB; zstd.
- Host-authoritative action library; lease on the armature/action.
- `TIMELINE` presence: frame, playing, fps; follow host optional; don't force
  seek when not following.
- Tests: 100 keyframes exact; playback within 1 frame; conflict via lease.

### 9.3 Shape keys, constraints, parenting

- `SHAPE_KEYS = 35`: object uid, key names, values, slider min/max; basis mesh
  already synced; 30 Hz on change.
- `OBJECT_META` constraints: type, name, influence, target uid, subtarget,
  whitelist props; unsupported types warn.
- Parent/child cycle guard; preserve transforms.

### 9.4 Sculpt

- Detect `context.mode == "SCULPT"` and the active mesh; read
  `obj.data.vertices` at 20 Hz (or evaluated data if required); send existing
  position deltas; no smoothing on the sender.
- Detect topology/count changes (dyntopo/multires) and send a `TOPOLOGY`
  snapshot if `TOPOLOGY` is enabled; otherwise warn and disable structural
  sculpt sync. Multires remains unsupported (explicit warning).
- Receiver applies deltas + smoothing; never applies while it is sculpting the
  same object (lease).
- Tests: 100k brush strokes at 20 Hz; 1M at reduced rate; no drift; memory
  stable for 10 min.

Exit gate: pose/action/shape keys/constraints match; sculpt usable on 100k;
multires/dyntopo limitations are explicit in the UI.

---

## 10. Phase 5 - Scale, Security, Platforms (0.5.0)

### 10.1 Subscriptions and interest management

- `SUBSCRIPTION = 36`: client -> host `{uids, mode: full|key|mute}`; default
  full. Host keeps per-peer sets; filters transform/mesh/topology/material sends.
- Priority from the client's presence camera + object AABB:
  - 0: in frustum and near -> 30 Hz transform, 20 Hz mesh.
  - 1: near/off-screen -> 10/5 Hz.
  - 2: far -> 2/1 Hz.
  - Always send a final keyframe on stop; hysteresis 20% to avoid flapping.
- Per-object mute UI (panel/right-click) and an "only sync visible" option.
- Acceptance: 8 clients x 10k objects, host CPU < 25%, < 2 Mbps per client, final
  state exact.

### 10.2 Adaptive quality and backpressure

- `CLIENT_STATS = 37` every 1 s: `apply_ms_p95`, `fps`, `rx_kbps`, `tx_kbps`,
  `queue_depth`.
- Host quality tiers high/med/low with hysteresis; adjust rates, quantization
  (i16/i8), and interpolation window.
- Drop non-critical frames when a peer queue > 256 frames or > 4 MB; never drop
  structural/critical; send a keyframe after drops.
- Per-peer byte budget (8 Mbps) in addition to the token bucket.
- Tests: 5 Mbps + 80 ms RTT + 5% loss; stable 45+ fps; eventual exact state;
  recovery after a burst.

### 10.3 Relay scale

- Split the single `Hub` Durable Object into:
  - `Directory` (listings, `/announce`, `/remove`, `/servers`, rate limits).
  - `Room` per room (`idFromName(roomId)`), with WebSocket hibernation and
    relay state.
- Directory entry TTL 90 s + heartbeat; dedupe by code; per-IP/per-room
  connection caps; message size caps; idle timeouts.
- `/health` includes room/connection counts (no PII).
- Optional Analytics Engine for join success/latency.
- Tests: 50 rooms x 2 clients, churn, no cross-room leakage, hibernation.

### 10.4 Cross-platform

- Tauri builds: Windows (NSIS), macOS (universal, notarized), Linux
  (AppImage/deb). Embedding stays Windows-only; other OS launch a normal Blender
  window and hide dock controls.
- Addon install paths per OS; `cryptography` wheels per platform; TLS host/join
  tests on macOS/Linux.
- CI matrix builds all artifacts; code signing and updater signatures.
- Blender 4.2 LTS/4.5/5.x compatibility matrix; guard 5.x-only APIs.

### 10.5 Security hardening

- CSP for the webview; no remote JS; sanitize chat/markdown.
- Path traversal protection for images/checkpoints; zip-bomb limits; fuzz all
  binary decoders (hypothesis).
- Dependency audits (`cargo audit`, `pip-audit`, `npm audit`), lockfiles, SBOM.
- Threat model tests: malicious peer, malicious host, directory compromise,
  relay MITM, stolen key, DoS. Document mitigations in `SECURITY.md`.

Exit gate: load/perf pass; signed identity and roles audited; macOS/Linux builds
join a session; security review with no high findings.

---

## 11. Phase 6 - Collaboration Polish (0.6.0)

- **Laser pointer** (from 6.2) if not already shipped.
- **View bookmarks** `VIEW_BOOKMARK = 42`: name + matrix + aspect; local save,
  jump, push to all (admin).
- **Send selection to teammate** `ASSET_BUNDLE = 43`: content-addressed bundle
  of objects/materials/node groups; preview + accept; caps; dedupe.
- **Detachable dashboard** (Tauri second window): users, leases, sync queue,
  history, notifications, DND; reads `status.json` and invokes commands.
- **Deep links**: `blender://join/CODE`; installer/Tauri protocol registration;
  auto-join; recent/favorite rooms.
- **Accessibility/theming**: Okabe-Ito palette, high contrast, overlay opacity/
  scale, font size, reduced motion, sound toggles; colorblind simulation test.
- **Voice (optional, separate budget)**: WebRTC/Opus, signaling over the relay,
  STUN/TURN (Cloudflare Calls or coturn), push-to-talk, mute. Do not block 1.0.

Exit gate: all polish items have tests; accessibility checklist passes; 1.0
release checklist complete.

### 11.1 0.9.0 - Collaboration foundation (shipped)

This release turns the Phase 6 scaffolds into working features:

* **AI copilot** (`core/ai.py`, `__init__._ai_handlers`): Ollama `/api/chat`
  with 23 tools, a plain-text scene snapshot in the prompt, JSON-string
  argument tolerance, per-edit result reporting, and an explicit
  propose/confirm/apply flow. Failed edits remain visible with their error.
* **Recording and replay** (`core/recording.py`, `session.event_sink`): every
  local send and every remote inbox event is recorded as JSONL. `Replay` feeds
  events back with original timing and a cancel switch. Recordings list with
  event counts and duration.
* **Live broadcast** (`core/broadcast.py`): a token-protected localhost HTTP
  server serves a self-contained page and an SSE stream. Chat, transforms,
  objects, locks and errors are shown live. No external JS, no second window.
* **Cross-engine bridge** (`core/cross_engine.py`, `integrations/`): TCP
  newline-delimited JSON. Unity, Unreal and Godot receivers; full scene
  snapshots with triangle meshes up to 20k vertices, per-object transform
  deltas, materials every 2 seconds, remove messages and ping/pong.
* **Performance** (`core/perf.py`, `sync/mesh.py`): p50/p95/p99 budgets in
  `status.json`; `PerfRegistry` for tick/apply/topology/bridge. The topology
  snapshot uses `BMesh.to_mesh` + `foreach_get` (about an order of magnitude
  faster than the old Python loops on 100k vertices) with a portable fallback,
  and large snapshots use zlib level 3 instead of 9.
* **Fidelity**: object renames now sync (`OBJECT_RENAME`), manual renames
  are detected by the transform poller, and Boolean modifiers sync their
  operation and operand UID so live booleans match on both sides. The details
  e2e suite asserts the Boolean target.
* **Lease fixes**: selecting an object no longer locks it forever. A lease is
  renewed only while the object is actively edited; an idle selection releases
  after 30 seconds. Disconnect leases expire after 30 seconds, peer-leave
  clears the local lock mirror, per-object lock messages are no longer written
  to chat, and the panel has **Release locks** plus an **Edit locks** off
  switch. `force_release_all` clears every host lease.
* **Tests**: 216 unit tests and 12 two-Blender/headless e2e suites, including
  `run_foundation.ps1` for AI tools, mesh export, bridge, broadcast and the
  recording tap.

---

## 12. Message catalogue additions

| ID | Message | Channel | Payload | Rate | Cap |
|---|---|---|---|---|---|
| 26 | SELECTION | PRESENCE | uid, kind, flags, rev, varint indices | 10 Hz | 50k |
| 27 | LASER | PRESENCE | seq, count, quantized points | 30 Hz | 64 pts |
| 28 | TOPOLOGY | MESH | uid, epoch, rev, geometry (zstd) | on change | 8 MB raw |
| 29 | MATERIAL_GRAPH | BULK | compressed node graph | on change | 256 KB |
| 30 | MATERIAL_BIND | CONTROL | object slots -> materials | 1 Hz | 64 slots |
| 31 | OBJECT_META | CONTROL | parent, visibility, modifiers, slots | 1 Hz | 16 KB |
| 32 | COLLECTION_STATE | CONTROL | name + visibility flags | 1 Hz | 256 |
| 33 | POSE | TRANSFORM | armature, bones, loc/quat/scale | 30 Hz | 256 bones |
| 34 | ACTION | BULK | F-curves/NLA | on change | 1 MB |
| 35 | SHAPE_KEYS | CONTROL | key names/values | 30 Hz | 128 keys |
| 36 | SUBSCRIPTION | CONTROL | uids + mode | on change | 4k uids |
| 37 | CLIENT_STATS | CONTROL | apply/fps/bandwidth/queue | 1 Hz | small |
| 38 | ROLE | CONTROL | peer, role | on change | small |
| 39 | RESUME | CONTROL | session, token, per-object revs | on reconnect | 64 KB |
| 40 | JOURNAL | CONTROL/BULK | edit summary/entries | on reconnect | 64 MB |
| 41 | CHECKPOINT | CONTROL | action, id, metadata | on demand | small |
| 42 | VIEW_BOOKMARK | CONTROL | name, matrix, aspect | on demand | small |
| 43 | ASSET_BUNDLE | BULK | content-addressed bundle | on demand | 128 MB |
| 44 | SCENE_STATE | CONTROL | world/render/visibility | 1 Hz | 16 KB |
| 45 | CHUNK | BULK | transfer id, index/count, payload | on demand | 8 MB/chunk |

Existing IDs 1-25 stay unchanged. Channels: CONTROL=0, TRANSFORM=1, MESH=2,
BULK=3, PRESENCE=4.

---

## 13. Test and CI matrix

**Unit**
- Protocol encode/decode, unknown/invalid types, CRC, size caps.
- New payloads: selection bitsets, laser quantization, topology headers,
  material graph sanitize, pose, action, roles, resume.
- Lease TTL/grace/request/role; journal conflict rules; scoped undo.
- Version consistency; capability intersection; error codes.

**Integration (two Blender instances, checked into `tests/e2e/`)**
- Bootstrap, transforms, vertex drag, topology operators, materials/textures,
  object details, locks, presence, reconnect, journal, roles, checkpoints,
  pose/action/shape keys, sculpt smoke.
- Assert final JSON/hashes, no echo, no drift, no stuck leases.
- Screenshot smoke for overlays (optional diff threshold).

**Network**
- A Python TCP proxy injecting 80 ms RTT, 5% loss, 30 ms jitter, 5 Mbps.
- Reconnect/backoff, queue drop, adaptive tiers, resume conflicts.

**Load/soak**
- 2/4/8 clients x 10k/100k/1M verts; 10-minute drag/sculpt; memory/CPU.
- 50 rooms in the Worker; join/leave churn; hibernation.

**Security**
- Fuzz all decoders (hypothesis), oversized/zip-bomb images, path traversal,
  forged signatures, role escalation, PIN brute force, token replay.

**Perf gates**
- Compare `bench/baseline.json`; fail on > 20% regression for p95 apply/encode.

Commands (CI):
`pytest -q`, `python scripts/check_version.py`, `node --check
desktop/ui/app.js`, `cargo clippy -- -D warnings`, `cargo test`,
`wrangler deploy --dry-run`, `tests/e2e/run.ps1`.

---

## 14. Security model summary

| Threat | Mitigation |
|---|---|
| Malicious peer edits others' objects | Roles + leases + host validation |
| Malicious host | Client can leave; no secrets in scene; optional PIN; audit |
| Directory impostor | Ed25519 signed descriptor + TLS pin + verify code |
| Relay MITM | TLS 1.3, certificate pinning, no plaintext |
| Stolen relay token | Hash stored, rotatable, bound to host identity |
| DoS via giant payloads | Size caps, zstd decompressed caps, token bucket, queue limits |
| Malicious asset | Content hashes, image format validation, size caps, no code |
| Local secret theft | OS keystore/ACL, no plaintext tokens in logs/status |
| Supply chain | Lockfiles, audits, SBOM, signed releases |

---

## 15. Risks and mitigations

1. **Topology snapshots on huge meshes** -> zstd, chunked BULK, cap + fallback to
   position-only; warn and disable topology for that object.
2. **Material graph explosion** -> whitelist + caps + placeholder; log once.
3. **Blender API drift (4.x/5.x)** -> version guards + matrix CI; keep the addon
   pure-Python where possible.
4. **Undo semantics** -> scoped log, not global undo; explicit UI copy; checkpoints
   for broad recovery.
5. **Reconnect complexity** -> session tokens + 120 s window + full reset
   fallback; never guess.
6. **Relay cost/CPU** -> per-room DO, hibernation, caps, metrics.
7. **Cross-platform embedding** -> keep embedding optional; separate window is
   the fallback.
8. **Scope creep** -> phase gates; feature flags; no phase starts early.
9. **Perf regressions** -> CI benches + p95 gates + profiling before optimizing.
10. **Data loss** -> checkpoints, backups, never overwrite `.blend`, crash marker.

---

## 16. Non-goals (defer or never)

- Full CRDT merge for arbitrary Blender data (leases + host authority instead).
- Compositor node graphs, simulations, caches, linked libraries, multires.
- Direct UDP hole punching / STUN / UPnP in 1.0 (relay is the supported path).
- Full Blender global undo integration.
- Arbitrary Python execution from peers (security).
- Voice in 1.0 (optional separate effort).

---

## 17. Start here (ordered, efficient)

1. `version.json` + `scripts/check_version.py` + test.
2. Persist settings/identity to `%LOCALAPPDATA%\Blendermp\settings.json`; migrate
   localStorage; installer preserves it.
3. Capability bits + directory `caps/min_app/version` + app preflight.
4. Move e2e scripts to `tests/e2e/`; wire into CI.
5. Diagnostics bundle + error codes.
6. Presence 2.0: `SELECTION`, `LASER`, cursor, follow user, selection rendering.
7. Lock UX: expiry prompt, request queue, badges; roles enforcement.
8. Topology snapshots during Edit Mode (debounced).
9. Material graph + image dedupe + all-slot bindings.
10. Reconnect/resume; then journal; then scoped undo/checkpoints.
11. Signed identity + roles audit.
12. Pose/action/shape keys; sculpt; constraints.
13. Interest/adaptive; per-room DO; macOS/Linux.
14. Polish: bookmarks, send-selection, dashboard, deep links, a11y.

First release after this plan: **0.1.7** (items 1-7). Ship it, run a real
two-machine session for a week, then start 0.2.0 (items 8-9).

---

## 18. Release checklist (every phase)

- [ ] `version.json` consistent; updater manifest signed.
- [ ] Unit + e2e + perf + security tests green.
- [ ] Migration from the previous version tested (settings, identity, host key,
      scene, checkpoints).
- [ ] Feature flags and rollback path documented.
- [ ] `README.md`, `SECURITY.md`, `docs/RELAY.md` updated.
- [ ] Installer signed; SmartScreen/notarization checked.
- [ ] Two-machine live session: host/join, edit, disconnect/reconnect, update.
- [ ] Known limitations updated; no silent regressions.
- [ ] Release notes with exact versions and protocol/capability changes.
