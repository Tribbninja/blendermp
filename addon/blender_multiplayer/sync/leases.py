"""Leases, roles and lock requests.

A lease is a soft lock with a TTL and a grace window. The grace window is what
makes collaboration pleasant: instead of an object being stuck because someone
walked away, the current owner gets a prompt and the lease frees itself.

Pure Python and time-injectable so it can be tested without sleeping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class Role(IntEnum):
    VIEWER = 0
    COMMENTER = 1
    EDITOR = 2
    ADMIN = 3
    OWNER = 4


def can_edit(role: Role | int) -> bool:
    return int(role) >= int(Role.EDITOR)


def can_manage(role: Role | int) -> bool:
    return int(role) >= int(Role.ADMIN)


@dataclass(slots=True)
class Lease:
    key: str
    owner: str
    expires_at: float
    renewed_at: float
    grace: float
    requests: list[str] = field(default_factory=list)

    @property
    def grace_until(self) -> float:
        return self.expires_at + self.grace

    def is_expired(self, now: float) -> bool:
        return now >= self.grace_until

    def is_expiring(self, now: float) -> bool:
        return self.expires_at <= now < self.grace_until


@dataclass(slots=True)
class SweepResult:
    released: list[str] = field(default_factory=list)
    expiring: list[str] = field(default_factory=list)


class LeaseManager:
    def __init__(self, ttl: float = 90.0, grace: float = 20.0):
        if ttl <= 0 or grace < 0:
            raise ValueError("ttl must be positive and grace non-negative")
        self.ttl = ttl
        self.grace = grace
        self._leases: dict[str, Lease] = {}

    def acquire(self, key: str, owner: str, now: float) -> bool:
        lease = self._leases.get(key)
        if lease is None or lease.is_expired(now):
            self._leases[key] = Lease(key, owner, now + self.ttl, now, self.grace)
            return True
        if lease.owner == owner:
            self.renew(key, owner, now)
            return True
        return False

    def force(self, key: str, owner: str, now: float) -> None:
        self._leases[key] = Lease(key, owner, now + self.ttl, now, self.grace)

    def renew(self, key: str, owner: str, now: float) -> bool:
        lease = self._leases.get(key)
        if lease is None or lease.owner != owner or lease.is_expired(now):
            return False
        lease.expires_at = now + self.ttl
        lease.renewed_at = now
        return True

    def release(self, key: str, owner: str | None = None) -> bool:
        lease = self._leases.get(key)
        if lease is None:
            return False
        if owner is not None and lease.owner != owner:
            return False
        del self._leases[key]
        return True

    def owner_of(self, key: str, now: float) -> str | None:
        lease = self._leases.get(key)
        if lease is None or lease.is_expired(now):
            return None
        return lease.owner

    def held_by(self, owner: str, now: float) -> list[str]:
        return [key for key, lease in self._leases.items() if lease.owner == owner and not lease.is_expired(now)]

    def request(self, key: str, requester: str, now: float) -> bool:
        lease = self._leases.get(key)
        if lease is None or lease.is_expired(now):
            return False
        if lease.owner == requester or requester in lease.requests:
            return False
        lease.requests.append(requester)
        return True

    def requests_for(self, key: str) -> list[str]:
        lease = self._leases.get(key)
        return [] if lease is None else list(lease.requests)

    def resolve_request(self, key: str, requester: str, grant: bool, now: float) -> bool:
        lease = self._leases.get(key)
        if lease is None or requester not in lease.requests:
            return False
        lease.requests.remove(requester)
        if grant:
            owner = lease.owner
            lease.owner = requester
            lease.expires_at = now + self.ttl
            lease.renewed_at = now
            lease.requests = [r for r in lease.requests if r != owner]
        return True

    def sweep(self, now: float) -> SweepResult:
        result = SweepResult()
        for key, lease in list(self._leases.items()):
            if lease.is_expired(now):
                del self._leases[key]
                result.released.append(key)
            elif lease.is_expiring(now):
                result.expiring.append(key)
        return result

    def snapshot(self, now: float) -> dict[str, dict]:
        return {
            key: {
                "owner": lease.owner,
                "remaining": max(0.0, lease.expires_at - now),
                "grace": max(0.0, lease.grace_until - now),
                "requests": list(lease.requests),
                "expiring": lease.is_expiring(now),
            }
            for key, lease in self._leases.items()
            if not lease.is_expired(now)
        }

    def __len__(self) -> int:
        return len(self._leases)
