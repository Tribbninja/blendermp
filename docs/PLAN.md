# Blender Multiplayer â€” Rebuild Plan

Clean-room, high-performance replacement for the Multi-user addon.

## Locked decisions

| Decision | Choice |
|---|---|
| Codebase | Clean-room new addon (no GPL code reused) |
| License | MIT |
| Blender support | 5.x only (5.0+, CI on the two latest minors) |
| Networking | LAN-first; cross-network via room codes; **no mandatory third-party service** â€” the host's own machine runs the session and, when reachable, the lobby |
| Performance envelope | 1M+ vertices, heavy sculpt |
| Multires | Later (research spike), not v1 |
| Packaging | Blender 5.x Extensions (`blender_manifest.toml`), platform wheels bundled |

---

## Implementation status

**Shipped in the first vertical slice**

- TLS 1.3 host/client, certificate pinning, host-authoritative validation, leases and roles.
- LAN host/join, room codes, presence, chat, 3D pings, chat commands, pause sync, diagnostics.
- Chunked mesh deltas, full geometry snapshots (topology, UVs, smooth flags, materials), multi-mesh tracking.
- Self-hosted multiplexed relay for cross-network sessions; TLS stays end to end.
- Tauri desktop shell with native Blender window embedding, control-file bridge, packaging scripts, verified Blender extension install.

**Still ahead**

- NAT hole punching and direct-route selection before relay fallback.
- Replication for materials, geometry nodes, images and animation; object creation/deletion.
- Multires; non-Windows embedding.
- The remaining quality-of-life list in section 9.

---

## 1. Why a rebuild (measured against upstream)

Upstream analysed: `slumber/multi-user` 0.5-dev (GPLv3). Ideas only are reused; no code.

| Bottleneck | Location | Effect |
|---|---|---|
| Remote changes applied on a timer defaulting to 1.0 s | `preferences.py` (`depsgraph_update_rate`), `timers.py` (`ApplyTimer`) | Hard ~1 s latency floor |
| Every depsgraph update dumps the entire datablock | `bl_types/bl_mesh.py` â€” all verts, edges, loops, polys, UVs, colours | O(mesh size) work per edit |
| Serialization = DeepDiff delta + pickle | `replication/objects.py` | Seconds of CPU on large meshes, both ends |
| Receiver does `clear_geometry()` + re-add + `validate()` + `update()`, then re-applies parents/children | `porcelain.apply`, `bl_reload_parent` | Full mesh rebuild per apply |
| Network listener is a 1 ms busy timer on the main thread | `timers.py` (`SessionListenTimer(0.001)`) | Constant main-thread burn, frame jitter |
| Whole-datablock ownership, 10 Hz auto-lock timers, edit-mode sync off by default | `DynamicRightSelectTimer`, sync flags | Cannot model collaboratively |

None of this is tunable â€” the diff/rebuild pipeline has to be replaced.

---

## 2. Targets

| Path | Component | Target |
|---|---|---|
| Capture + diff (1M verts) | `foreach_get` + chunk hashes | 4â€“6 ms |
| Encode | binary, quantised deltas | 0.5â€“2 ms |
| LAN direct | TLS 1.3 over TCP | 1-3 ms |
| WAN direct (hole-punched) | TLS over the punched route | 15-40 ms RTT |
| Relay fallback | TLS to relay | 30-80 ms |
| Apply (1M verts, positions) | `foreach_set` + `mesh.update()` | 4â€“8 ms |
| **Transform end-to-end** | | **â‰¤ 30 ms LAN, â‰¤ 80 ms WAN direct** |
| **Mesh stroke visible** | | **â‰¤ 100 ms LAN, 15â€“20 Hz sustained** |
| Join 500 MB scene | snapshot + delta bootstrap | < 20 s |
| Idle main-thread cost | | < 2 ms/frame |

Known ceiling: Blender's own normal recomputation after a position change costs tens of ms on 1M smooth-shaded verts. Remote sculpt therefore runs at 10â€“20 Hz with receiver-side interpolation; transforms stay at 60 Hz; local editing is unaffected.

---

## 3. Architecture

Three parts, clean separation:

1. **Addon client**
   - Blender main thread: `foreach_get`/`foreach_set` and draining a queue only.
   - Daemon thread: sockets, compression, parsing, crypto.
   - 1â€“2 ms main-thread timer drains the apply queue.
2. **Session host** (authoritative)
   - Runs embedded in the host's Blender, or headless from the same install.
   - Per-datablock version vectors, lease registry, roles, snapshots, persistence.
3. **Lobby + relay** (same binary, optional, self-hosted)
   - Lobby: room registry, candidate exchange, TTL. No scene data.
   - Relay: forwards encrypted datagrams when hole punching fails.
   - Can co-host with the session on a user's machine when it is reachable.

### Transport

- Custom binary framing: length-prefixed frames, `struct` + varint + raw numpy buffers. No JSON, no pickle, no DeepDiff.
- Channels multiplexed over one connection:
  - `control` â€” reliable, ordered
  - `bulk` â€” reliable stream (images, snapshots)
  - `mesh` â€” sequenced, chunked, supersedable
  - `transform` â€” unreliable, latest-wins
  - `presence` â€” unreliable, latest-wins
- v1 transport: **TLS 1.3 over TCP** using Python's stdlib `ssl`, with length-prefixed binary frames.
- **No custom cryptography.** The host presents a self-signed certificate that joiners pin by SHA-256 fingerprint (TOFU plus a verification code); an optional PIN is checked server-side.
- A later fast path is QUIC (TLS 1.3 datagrams) via an audited library. Never hand-rolled AEAD or nonces.
- The relay only ever sees TLS ciphertext.

---

## 4. Networking: host-run, no mandatory service

### Discovery ladder (in order)

1. **LAN** â€” UDP broadcast / mDNS beacon. Same subnet â†’ direct, zero configuration, lowest latency.
2. **IPv6** â€” global address, no NAT. Host advertises it in the invite payload.
3. **IPv4 + automatic port mapping** â€” UPnP-IGD, NAT-PMP and PCP implemented in the addon (~300 lines, pure Python). Works on most home routers; shows a clear status if the router refuses.
4. **IPv4 + manual forward** â€” UI displays the port and a copyable instruction.
5. **CGNAT / symmetric NAT** â€” direct inbound is impossible. Fall back to a **relay that the host itself runs somewhere reachable** (a VPS, a spare machine, or a community relay), or a VPN overlay (Tailscale/ZeroTier) whose address is auto-detected and offered as a candidate.

This is honest about physics: without *any* reachable endpoint, two devices behind CGNAT cannot connect. The plan is to make the common cases zero-effort and the hard case one documented step.

### Room codes

- Short code (default): `ABCD-EFGH` Crockford Base32 â€” version, flags, IPv4, port, CRC.
- Full invite (QR / copy link): base64url JSON with all candidates (LAN IPs, IPv6, public endpoint, relay), host public key, expiry.
- 4-digit optional PIN, verified server-side with constant-time comparison and rate limiting.
- The room code is only an endpoint locator; it is never used as key material and is safe to broadcast on a LAN.
- Codes are per-session, so changing IPs do not break anything.
- UI shows the QR, so a phone or second screen can hand it over.

### Handshake and authorization

1. Standard TLS 1.3 handshake; the host uses a persisted self-signed certificate.
2. Joiners pin the certificate SHA-256 on first use and compare a short verification code. A changed fingerprint is refused.
3. An optional room PIN is checked server-side, in constant time, with backoff.
4. The host issues a per-connection identity, validates every frame and sets the `from` field itself. Clients never talk directly to each other and never see other clients' addresses.
5. The relay sees ciphertext only; host scene data is never trusted from a client without validation.

See `SECURITY.md` in the repository for the full trust model and known limitations.

Dependencies bundled as platform wheels: `cryptography`, `zstandard`. Everything else is stdlib + numpy.

### Join bootstrap

- Host keeps periodic snapshots (native `.blend` or compact graph snapshot, configurable).
- Joiner receives snapshot at revision *R* over `bulk`, then deltas since *R*.
- Much faster than replaying history, and works as crash recovery.

---

## 5. Consistency, ownership, undo

- **Leases**, not permanent locks: TTL, auto-release on idle/disconnect, soft lock (warn) vs hard lock, admin steal.
- **Roles**: owner / admin / editor / commenter / viewer; per-collection overrides; enforced server-side, never trusted from clients.
- **Hybrid logical clocks** + per-property LWW for genuinely shared data (world, render settings, scene props). Exclusive ownership for meshes, armatures, actions.
- **Undo**: local undo produces a corrective diff against the shadow. No `bpy` pointers retained; UUID-keyed. Server checkpoints drive the session timeline and per-object rollback.
- **Self-healing**: per-datablock revision + checksum; any divergence requests a snapshot from the host.
- Reconnect/resume from last acknowledged revision.

---

## 6. Mesh pipeline for 1M+ vertices

### Send

1. Read positions into a preallocated `float32` buffer: `mesh.vertices.foreach_get("co", buf)` vs the `position` attribute in 5.x â€” benchmark both and pick.
2. Split into 8,192-vertex chunks. One vectorised `np.bitwise_xor.reduce` over a `uint64` view yields every chunk hash in ~1â€“2 ms per 1M verts.
3. Compare to stored hashes â†’ only changed chunks get a precise diff (changed indices + coordinates).
4. Encode: varint delta-encoded indices; positions as int16 relative to a per-chunk base (6 B/vert) or float32 in lossless mode; optional zstd.
5. Skip idle meshes. Full sweep at stroke end, after undo, and every 250 ms as a correctness net.
6. **Sculpt optimisation**: derive the dirty chunk set from the brush's screen-space radius, scan those plus a margin; always fall back to a full sweep.

### Receive

- Positions only â†’ write shadow, `foreach_set` the full buffer, `mesh.update()`. No `clear_geometry`, no element re-add, no `validate()`.
- **Interpolation**: lerp changed vertices between two snapshots over ~50 ms; turns 15 Hz traffic into smooth motion.
- Topology change (separate structural revision) â†’ full array snapshot, chunked and compressed, applied through the same fast path plus custom-data arrays (UVs, colours, creases, generic attributes).
- Normals are never transmitted; the receiver recomputes.

### Backpressure

- Mesh frames supersedable (max 2 queued), transforms drop-if-stale.
- Per-session bandwidth budget throttles update rate instead of queuing.
- Head-of-line blocking is avoided by channel-level reliability, not TCP ordering of everything.

### Edit mode and sculpt

- Owner streams; other clients render a **ghost preview mesh** that never mutates their real data until commit.
- Optional live-merge mode for trusted pairs.
- Structural edits stream topology; transform/brush strokes stream positions.

### Multires

Tier 3 research spike. Blender exposes no clean per-level displacement API; treat as "experimental, may not ship" rather than a promise.

---

## 7. Other datablocks

- **Objects**: transform, parenting, visibility, collections, modifiers, instancing.
- **Materials / node trees**: sockets, links, node groups, 5.x node-tree interfaces.
- **Geometry nodes**: group data, named attributes, modifier inputs.
- **Images / movies**: chunked, resumable, deduplicated by content hash; packed-vs-path policy.
- **Animation**: actions, NLA, drivers as opaque data; frame modes (independent / follow host / jam).
- **Text, worlds, cameras, lights, curves, lattices, grease pencil**: typed handlers.
- **Custom types**: plugin API so third parties can register serializers.

Each handler is a small typed module: dump to a schema struct, apply to `bpy` data, declare dependencies, declare revision triggers. No generic deep-diff anywhere.

---

## 8. Features by tier

### Tier 1 â€” usable session
- Host/join by code or QR; LAN autodiscovery.
- Roles and permissions; leases.
- Presence: coloured cursors, view frustums, selection boxes, tool/mode badges, name tags, follow-user, snap-to-view.
- Transform/object sync.
- Mesh fast path (dense meshes).
- Materials, modifiers, geometry-node basics.
- Text chat.
- Diagnostics HUD: ping, RTT graph, per-channel bandwidth, packet loss, tick rates, main-thread cost.
- Autosave, manual snapshots.
- Encrypted rooms; optional PIN.

### Tier 2 â€” differentiators
- Edit-mode and sculpt streaming with ghost preview and interpolation.
- Chunked image/movie sync.
- Animation and frame modes.
- Comments and pins with threads.
- Session timeline, per-object rollback, restore to any checkpoint.
- Reconnect/resume; crash recovery.
- NAT traversal: UPnP/NAT-PMP/PCP, STUN, hole punching, relay fallback, IPv6.
- Headless server + Docker image, plus a one-command relay.
- Bandwidth caps and rate-limit policies; "low-bandwidth mode".

### Tier 3 â€” later
- Voice chat.
- Read-only web viewer.
- Multires sync (conditional on the spike).
- Asset-library sync.
- Host migration using relay checkpoints.
- Plugin API for custom datablock types.
- Localisation.
- CI perf dashboard.

Section 9 adds 20 quality-of-life features on top of these tiers.

---

## 9. Quality-of-life features (20)

These are conveniences layered on top of the core sync engine. None of them requires a protocol redesign; interest management and per-object subscription only add optional message fields. Each is tagged with its tier and the phase where it lands.

### Session safety and flow

1. **Pause sync** (T1 Â· phase 2) â€” a header toggle stops outbound publishing while you do heavy local work, then sends one coalesced diff on resume instead of a backlog. Inbound updates can continue or pause with it.
2. **Offline edit buffer with crash recovery** (T2 Â· phase 8) â€” if the link drops or Blender crashes, edits are journalled against the last acknowledged revision. On reconnect they replay as a diff with a per-object prompt ("keep mine / take theirs / keep both"); the same journal powers "restore unsynced edits" after a crash.
3. **Scoped undo** (T2 Â· phase 2) â€” your undo stack only reverts your own changes; another user's edit is skipped rather than steamrolled. A server-side "undo my last N minutes" produces the same scoped correction.
4. **Draft/sandbox mode** (T2 Â· phase 4) â€” mark an object as draft: you edit locally, nobody else sees it, then publish or discard. For risky remeshing or variants you might throw away.
5. **Lock request queue** (T1 Â· phase 2) â€” "Request edit" in the outliner right-click and the viewport footer. The owner gets a toast with grant/deny; pending requests stay visible so nothing is lost while they are heads-down.
6. **Lease auto-release with a grace prompt** (T1 Â· phase 2) â€” idle or unfocused Blender releases leases after a configurable window, but asks "still working?" first and accepts a one-key answer. Ends the classic "nobody can touch this for 40 minutes" deadlock.
7. **Handoff with context** (T1 Â· phase 2) â€” when you receive a lock or a deliberate handoff, the viewport snaps to the object and inherits the previous author's mode, selection and active tool; a banner names who had it and what changed.

### Big-scene ergonomics

8. **Per-object sync subscription** (T2 Â· phase 3) â€” mute objects or collections you do not need to watch update (background foliage, a distant set). The host stays authoritative; re-subscribing fetches a snapshot on demand.
9. **Interest management** (T2 Â· phase 3) â€” automatic priority: objects in your view frustum or near your cursor stream at full rate, mid-distance at reduced rate, off-screen at a trickle. Chunk-level, so it composes with the mesh delta pipeline rather than fighting it.
10. **Adaptive quality** (T2 Â· phase 3) â€” the client measures its own apply cost and available bandwidth and tunes tick rate, quantisation and interpolation to hold a target framerate. Users get a good session without understanding the settings.
11. **Named checkpoints** (T1 Â· phase 2) â€” one button before a risky operation saves a named snapshot with a thumbnail; restore is one click. Sits on the session timeline rather than replacing it.

### Pointing and talking

12. **3D ping marker** (T1 Â· phase 1) â€” hotkey plus click drops a coloured marker in world space with your name, a soft sound and a two-second fade. Works in any viewport, including while someone else is sculpting.
13. **Laser pointer** (T1 Â· phase 1) â€” hold a key and draw a fading stroke in 3D to trace an edge or silhouette while explaining. Drawn in the GPU draw handler; nothing is added to the scene.
14. **Chat commands** (T1 Â· phase 7) â€” `/ping`, `/tp @user`, `/lock`, `/unlock`, `/who`, `/snap`, `/dnd`, `/invite`. Fast path for actions that are otherwise three clicks deep.
15. **Send selection to teammate** (T2 Â· phase 7) â€” right-click â†’ Send to â†’ user. Pushes objects, materials or node groups into their scene or asset library, deduplicated by content hash, with a preview before they accept.
16. **Shared view bookmarks** (T1 Â· phase 7) â€” save "front detail", "hero shot", "check the hands" as named camera views visible to the session. Anyone can jump to one; a presenter can push it to everyone.

### UI, discovery and support

17. **Detachable session dashboard** (T1 Â· phase 7) â€” a real window rather than an N-panel tab: users, leases, sync queue, history, notification centre, DND toggles. Lives on a second monitor and remembers its layout.
18. **Deep links and smart join** (T1 Â· phase 5) â€” `blender://join/ABCD-EFGH` opens Blender and joins; pasting a code anywhere in the UI auto-joins; recent and favourite rooms are one click away, and Blender can offer to resume the last session at startup.
19. **Copy diagnostics bundle** (T1 Â· phase 8) â€” one button collects versions, addon settings, network stats, scene stats and the last N log lines into a zip or the clipboard for a bug report. No more "send me your log".
20. **Accessibility and theming** (T1 Â· phase 7) â€” colourblind-safe user palettes, high-contrast overlay mode, adjustable overlay opacity and scale, and per-event notification sound toggles. Presence is how people read a session, so it has to be legible.

---

## 10. Repo layout and packaging

```
blender_multiplayer/
  __init__.py
  blender_manifest.toml        # 5.x extensions; network permission; bundled wheels
  wheels/                      # cryptography, zstandard (per platform)
  core/
    protocol.py                # frames, message types, varints
    transport.py               # TLS/TCP framing; UDP/QUIC later
    stun.py                    # STUN binding, reflexive discovery
    nat.py                     # UPnP-IGD, NAT-PMP, PCP
    tls.py                     # TLS 1.3 contexts, certificate pinning
    clocks.py                  # hybrid logical clocks
    lobby.py                   # room registry, candidates, TTL
    relay.py                   # datagram forwarding
  sync/
    shadow.py                  # per-datablock shadow state
    chunker.py                 # 8k-vertex chunk hashing/diffing
    mesh_delta.py              # position/topology encoding
    apply_queue.py             # main-thread queue, coalescing
    leases.py                  # TTL locks, roles
    snapshot.py                # bootstrap + checkpoints
  data/                        # one handler per datablock type
  server/                      # session host, auth, persistence, headless entry
  ui/                          # panels, operators, presence, chat, diagnostics
  tests/                       # unit + two-instance headless integration
  bench/                       # 1M-vert scenes, latency harness, perf gates
```

- Installable as a 5.x extension `.zip` and as a legacy addon folder.
- Wheels listed in `blender_manifest.toml`; CI builds per-platform artifacts.
- `python -m blender_multiplayer.server` runs headless (no Blender needed for relay/lobby).

---

## 11. Milestones and exit criteria

| Phase | Work | Exit criterion |
|---|---|---|
| 0 | Benchmark harness; baseline; protocol spec | Every metric below recorded |
| 1 | Core protocol, headless server, LAN host/join, presence | Transform < 30 ms LAN |
| 2 | Object/transform sync, leases, roles, undo-safe apply | 2 users drag at 60 Hz, no drift |
| 3 | 1M-vert mesh pipeline | Tick < 6 ms, apply < 8 ms, 15â€“20 Hz stroke |
| 4 | Edit mode, ghost preview, interpolation | 1M-vert sculpt session usable |
| 5 | Room codes, QR, UPnP/NAT-PMP, STUN, hole punch, relay, crypto | Two users on different networks connect by code |
| 6 | Datablock coverage: materials, GN, images, animation | Corpus round-trip tests pass |
| 7 | Feature and QoL layer: chat, comments, timeline, dashboard, pings, bookmarks, accessibility | Feature + QoL checklist complete |
| 8 | Hardening: offline buffer, diagnostics bundle, packaging, CI, docs | Installable `.zip`, perf gates green |

Roughly 16â€“20 weeks solo; phases 5â€“7 partly parallelisable.

---

## 12. Testing and benchmarking

- **Unit**: framing, varint, chunk hashing, CRDT/LWW registers, lease expiry, code encode/decode round-trip.
- **Integration**: two headless Blender 5.x instances over loopback; assert final state equality and no drift after 10k random edits.
- **Network simulation**: `tc netem` / Clumsy â€” 80 ms RTT, 5 % loss, 30 ms jitter, 5 Mbps cap; assert behaviour and recovery.
- **NAT matrix**: full-cone, restricted, port-restricted, symmetric, CGNAT, IPv6-only, double NAT; document the pass/fail matrix and the expected fallback.
- **Load**: 2/4/8 clients Ã— 10k/100k/1M verts Ã— {sculpt stroke, transform drag, topology edit, join}.
- **Metrics**: end-to-end timestamps, p50/p95/p99, CPU %, bandwidth, memory, main-thread stall.
- **Perf gates in CI**: fail the build on regression beyond an agreed margin.

---

## 13. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Normal recompute caps 1M-vert update rate | 15â€“20 Hz + interpolation; "smooth vs fast" toggle; document the ceiling |
| Symmetric NAT / CGNAT cannot be traversed directly | Honest fallback: host-run relay on any reachable machine, VPN overlay detection, manual port forward |
| UPnP support varies by router | Multiple protocols (UPnP/NAT-PMP/PCP), clear diagnostics, manual override |
| Edit-mode API fragility | Ghost preview isolates damage; CI across minor versions |
| Undo invalidates `bpy` pointers | UUID + shadow design; explicit undo/redo test suite |
| 1M-vert memory use | Shadows only for meshes in the active sync set; configurable chunk count; release buffers on deselect |
| Multires API limits | Research spike; not promised for v1 |
| Blender 5.x API churn | CI on the two latest minors; version-gated shims only where unavoidable |
| Bundled platform wheels | CI matrix builds and tests each platform artifact |
| Relay bandwidth cost | Only used when direct fails; quantised deltas, rate caps, host pays for its own relay |

---

## 14. Non-goals for v1

- Closed-source/paid licensing concerns (clean-room, MIT).
- WebRTC/aiortc stack.
- Arbitrary Python execution or script syncing.
- Deterministic replay of sculpt strokes.
- Public hosted lobby service.
- Blender < 5.0.

---

## 15. Immediate next steps

1. Scaffold the extension (`blender_manifest.toml`, `__init__.py`, package layout, CI).
2. Write the protocol spec document (frame layout, message catalogue, channel semantics).
3. Build the benchmark harness and record the target metrics with a stub implementation.
4. Implement Phase 1: LAN discovery, handshake, presence, transform stream.
5. Benchmark against the targets before adding datablock coverage.
