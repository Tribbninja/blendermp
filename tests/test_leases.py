import pytest

from sync.leases import LeaseManager, Role, can_edit, can_manage


def test_roles():
    assert can_edit(Role.EDITOR) and not can_edit(Role.COMMENTER)
    assert can_manage(Role.ADMIN) and not can_manage(Role.EDITOR)


def test_acquire_and_deny():
    leases = LeaseManager(ttl=10, grace=5)
    assert leases.acquire("obj", "ada", now=0)
    assert not leases.acquire("obj", "grace", now=1)
    assert leases.owner_of("obj", now=1) == "ada"
    assert leases.acquire("obj", "ada", now=2)


def test_expiry_and_grace_prompt():
    leases = LeaseManager(ttl=10, grace=5)
    leases.acquire("obj", "ada", now=0)
    result = leases.sweep(now=10.5)
    assert result.expiring == ["obj"] and not result.released
    assert leases.owner_of("obj", now=10.5) == "ada"
    result = leases.sweep(now=16)
    assert result.released == ["obj"]
    assert leases.owner_of("obj", now=16) is None


def test_release_requires_owner():
    leases = LeaseManager()
    leases.acquire("obj", "ada", now=0)
    assert not leases.release("obj", "grace")
    assert leases.release("obj", "ada")
    assert len(leases) == 0


def test_requests_and_grant():
    leases = LeaseManager()
    leases.acquire("obj", "ada", now=0)
    assert leases.request("obj", "grace", now=0)
    assert not leases.request("obj", "grace", now=0)
    assert leases.requests_for("obj") == ["grace"]
    assert leases.resolve_request("obj", "grace", grant=True, now=1)
    assert leases.owner_of("obj", now=1) == "grace"
    assert leases.requests_for("obj") == []


def test_force_is_admin_only_by_convention():
    leases = LeaseManager()
    leases.acquire("obj", "ada", now=0)
    leases.force("obj", "admin", now=1)
    assert leases.owner_of("obj", now=1) == "admin"


def test_snapshot():
    leases = LeaseManager(ttl=10, grace=5)
    leases.acquire("a", "ada", now=0)
    body = leases.snapshot(now=2)
    assert body["a"]["owner"] == "ada"
    assert body["a"]["remaining"] == pytest.approx(8)
    assert body["a"]["expiring"] is False
