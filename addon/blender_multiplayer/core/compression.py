"""Session-wide payload compression policy.

Compression here is always **lossless** (zlib). It never quantises positions,
drops vertices or otherwise changes the data; a decoded payload is byte-for-byte
identical to the source.

Policy:

* Direct LAN sessions set this to ``False``: payloads go out raw, so the CPU
  cost is zero and a local gigabit link is not the bottleneck.
* Relay/cross-network sessions set it to ``True``: geometry snapshots, mesh
  deltas, asset bundles and images use zlib to save bandwidth.

The policy is process-wide because one Blender instance hosts or joins one
session at a time. Decoders always accept both raw and compressed payloads, so
mixed-version or reconnecting peers keep working.
"""

from __future__ import annotations

_enabled = True


def set_enabled(value: bool) -> None:
    global _enabled
    _enabled = bool(value)


def enabled() -> bool:
    return _enabled
