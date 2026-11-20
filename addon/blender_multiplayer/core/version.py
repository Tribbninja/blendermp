"""Single source of truth for the addon version and compatibility rules."""

from __future__ import annotations

import re

ADDON_VERSION = "0.1.16"

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def parts(version: str) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch(str(version).strip())
    if not match:
        return (0, 0, 0)
    return tuple(int(value) for value in match.groups())  # type: ignore[return-value]


def compatible(local: str, remote: str) -> bool:
    """Return whether two builds may share a session.

    Pre-1.0 releases change protocol-visible behavior in patch versions, so the
    versions must match exactly. From 1.0 on, patch releases interoperate and
    only the major/minor pair must match. An empty remote version is treated as
    a legacy peer and allowed; the capability gate then limits its messages.
    """
    if not str(remote).strip():
        return True
    local_parts = parts(local)
    remote_parts = parts(remote)
    if local_parts[0] == 0:
        return local_parts == remote_parts
    return local_parts[:2] == remote_parts[:2]


def label(version: str) -> str:
    return str(version).strip() or "unknown"
