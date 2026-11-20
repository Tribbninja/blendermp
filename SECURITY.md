# Security model

Blendermp is an early alpha. This document describes the guarantees it aims to
provide and the ones it does not provide yet.

## Trust model

* **The host is authoritative.** All clients connect to the host over TLS 1.3 in
  a star topology. Clients never talk to each other directly.
* **Transport security is standard TLS 1.3** using Python's `ssl` module. There
  is no application-level encryption and no bespoke cryptography in this
  project.
* **The relay only forwards ciphertext.** In relay mode the host keeps an
  outbound TCP tunnel; clients reach the host's TLS listener through a local
  loopback bridge. The relay sees IP addresses, room ids (a hash of the code),
  timing and byte counts, but never plaintext. It cannot impersonate a host
  without the host's private key.
* **The host certificate is pinned.** The host generates a self-signed
  certificate in `%LOCALAPPDATA%\Blendermp\certs` (owner-only permissions).
  Joiners pin its SHA-256 fingerprint on first connection (TOFU) and compare the
  short verification code shown in both UIs. A changed certificate is refused,
  not silently trusted.
* **The room code is an endpoint locator, not a secret.** It encodes the host's
  IPv4 address and TCP port. It is not used to derive keys.
* **An optional PIN** is verified server-side in constant time, with rate
  limiting. It is held in memory for the session and never written to disk.

## Installer downloads

The Windows setup only downloads Blender when no local installation is found and
the user accepts the prompt. The URL is pinned to `download.blender.org` and the
MSI's SHA-256 is checked before it is passed to `msiexec`. A mismatch aborts the
Blender step; Blendermp is still installed and no unverified binary is executed.

## What the host validates

Untrusted input is rejected before it reaches Blender:

* frame magic, version, length, CRC;
* message type and direction (clients cannot send host-only messages);
* per-peer token-bucket rate limits and connection strikes;
* JSON payload size (control ≤ 64 KB) and shape;
* chat length (≤ 2,000 chars) and control-character stripping;
* transform payloads: exactly 80 bytes, finite floats, bounded magnitude;
* mesh payloads: ≤ 24 MB, ≤ 5,000,000 vertices, structural validation of delta
  headers, geometry snapshots and positions before relay;
* object payloads: 16-byte UUID, bounded metadata, geometry only for mesh objects;
* material payloads: bounded strings and colours, finite numbers in range;
* modifier stacks: ≤ 64 modifiers, ≤ 32 properties each, scalar/vector values only,
  no nested objects, names/lengths capped;
* object leases: a peer cannot edit an object leased to someone else;
* names and colours: assigned by the host after authentication, made unique, so
  a client cannot impersonate another participant;
* relayed messages: the host sets the `from` identity itself.

Backpressure is bounded: stale transforms are dropped, peers that cannot keep up
are disconnected instead of growing an unbounded queue.

## Local control channel

The desktop app drives the addon through two JSON files in
`%LOCALAPPDATA%\Blendermp`. That is a local trust boundary, so:

* `session.json` is size-limited and schema-validated, with a strict action
  allowlist (`host`, `join`, `leave`, `chat`) and a nonce;
* only a valid 15-character room code is accepted;
* no file paths, script fragments or arbitrary arguments are accepted;
* status writes are atomic and bounded;
* the Rust side validates the same inputs again before writing.

## Desktop app

* Strict CSP: no remote content, no inline scripts, no `eval` in our code.
* Minimal Tauri capability list; only the commands the app needs are allowed.
* The embedded window must belong to the Blender process the app launched, and
  only a window whose title matches Blender is accepted.
* Blender is launched with a fixed `--python-expr` and an allowlisted addon
  path; no shell, no arbitrary arguments from the webview.
* Release builds disable devtools.

## Known limitations

* **First-connection TOFU.** If an attacker can intercept the very first
  connection and the user skips the verification-code comparison, they can
  impersonate the host. Verify the code for untrusted networks.
* **The host sees the scene.** This is cooperative editing, not
  zero-knowledge sharing.
* **The relay sees metadata.** IP addresses, room ids, timing and traffic volume
  are visible to the relay operator. Run your own relay if that matters.
* **LAN discovery leaks the code and display name to the local subnet.** That is
  inherent to zero-config discovery; use a non-LAN room if that matters.
* **No certificate rotation UI.** Certificates rotate automatically after 30
  days; peers must re-verify the new code.
* **No independent security audit.** Do not use this for sensitive production
  work yet.

## Reporting

Open an issue with a minimal reproduction. Do not include private keys, room
PINs or captured scene data. If the report is sensitive, say so and a private
channel will be arranged.

## Public directory

The public directory stores a server's display name, host name, room endpoint,
player count, password flag and version for at most 60 seconds after the last
announcement. It never receives the server password, scene data, chat or
certificates. Announcements are unauthenticated by design, so a directory
operator can list or remove entries; clients still pin the host certificate and
the host still validates every connection. Kicks are enforced by the host.

## Cloudflare Worker hub

The optional Worker hub stores the same short-lived directory metadata and
relays WebSocket messages between the host and clients. Cloudflare sees IP
addresses, room ids, timing and byte counts, but not TLS plaintext, passwords,
scene data or host private keys. The host certificate is still pinned and the
password is still verified by the host.
