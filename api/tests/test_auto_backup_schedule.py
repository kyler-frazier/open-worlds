"""Tests for the auto-backup schedule's failure backoff and the worker's
handling of a revoked Drive token. Mocks the DB per the suite convention."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from google.auth.exceptions import RefreshError

from open_worlds.common.drive import DRIVE_REVOKED_MESSAGE, is_revoked_grant
from open_worlds.common.models import AutoBackupInterval, BackupKind, BackupStatus
from open_worlds.worker import backups
from open_worlds.worker.main import (
    AUTO_BACKUP_RETRY_BASE,
    AUTO_BACKUP_RETRY_CAP,
    auto_backup_due,
    auto_backup_retry_delay,
)

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def _world(**kw):
    base = dict(
        auto_backup_interval=AutoBackupInterval.DAILY,
        last_auto_backup_at=None,
        auto_backup_failures=0,
        auto_backup_last_attempt_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --- due check ------------------------------------------------------------


def test_never_backed_up_is_due():
    assert auto_backup_due(_world(), NOW)


def test_not_due_until_interval_elapses_after_success():
    w = _world(last_auto_backup_at=NOW - timedelta(hours=23))
    assert not auto_backup_due(w, NOW)
    w = _world(last_auto_backup_at=NOW - timedelta(hours=25))
    assert auto_backup_due(w, NOW)


def test_failure_defers_the_retry():
    """A failing world is not due again on every scheduler tick."""
    w = _world(auto_backup_failures=1, auto_backup_last_attempt_at=NOW - timedelta(minutes=5))
    assert not auto_backup_due(w, NOW)
    w.auto_backup_last_attempt_at = NOW - AUTO_BACKUP_RETRY_BASE
    assert auto_backup_due(w, NOW)


def test_backoff_doubles_and_caps():
    assert auto_backup_retry_delay(0) == timedelta(0)
    assert auto_backup_retry_delay(1) == AUTO_BACKUP_RETRY_BASE
    assert auto_backup_retry_delay(2) == AUTO_BACKUP_RETRY_BASE * 2
    assert auto_backup_retry_delay(3) == AUTO_BACKUP_RETRY_BASE * 4
    assert auto_backup_retry_delay(50) == AUTO_BACKUP_RETRY_CAP


def test_backoff_never_exceeds_a_day():
    w = _world(auto_backup_failures=20, auto_backup_last_attempt_at=NOW - timedelta(hours=25))
    assert auto_backup_due(w, NOW)


# --- revoked token -----------------------------------------------------------


def test_is_revoked_grant_only_for_invalid_grant():
    assert is_revoked_grant(RefreshError("invalid_grant: Token has been expired or revoked."))
    assert not is_revoked_grant(RefreshError("temporarily unavailable"))
    assert not is_revoked_grant(RuntimeError("invalid_grant"))


class _FakeDB:
    def __init__(self, objs):
        self._objs = objs
        self.deleted: list = []

    def get(self, _model, key):
        return self._objs.get(key)

    def delete(self, obj):
        self.deleted.append(obj)

    def commit(self):
        pass


def _run_failing_backup(monkeypatch, exc, kind=BackupKind.AUTO):
    bid, wid = uuid.uuid4(), uuid.uuid4()
    owner = SimpleNamespace(id=uuid.uuid4(), drive_linked=True, drive_refresh_token_enc="enc")
    world = SimpleNamespace(
        id=wid,
        subdomain="w1",
        # The disk guard sizes itself from this world's import policy, which is
        # the game's.
        game="MINECRAFT",
        owner=owner,
        last_auto_backup_at=None,
        last_auto_backup_fingerprint="old",
        auto_backup_failures=2,
        auto_backup_last_attempt_at=None,
    )
    backup = SimpleNamespace(
        id=bid, world_id=wid, kind=kind, status=BackupStatus.PENDING, filename="f"
    )
    db = _FakeDB({bid: backup, wid: world})
    monkeypatch.setattr(backups, "fingerprint_data_dir", lambda _sub: "new")
    monkeypatch.setattr(backups.os, "makedirs", lambda *_a, **_k: None)
    # *args: the guard takes the needed size as well as the directory now, and
    # a stub pinned to the old arity fails with a TypeError that looks like a
    # product bug rather than a stale test.
    monkeypatch.setattr(backups, "_check_free_space", lambda *_a, **_k: (_ for _ in ()).throw(exc))
    monkeypatch.setattr(backups, "_prune_failed_backups", lambda *_a, **_k: 0)
    # run_backup re-raises after recording the failure, so the job it belongs to
    # fails too; the caller here only cares about what it recorded on the way.
    with pytest.raises(Exception):  # noqa: B017 - the exception is the fixture's own
        backups.run_backup(db, bid)
    return backup, world, owner


def test_failed_auto_backup_counts_and_stamps_the_attempt(monkeypatch):
    backup, world, owner = _run_failing_backup(monkeypatch, RuntimeError("disk full"))
    assert backup.status is BackupStatus.FAILED
    assert world.auto_backup_failures == 3
    assert world.auto_backup_last_attempt_at is not None
    assert owner.drive_linked  # an ordinary failure leaves the link alone


def test_revoked_token_unlinks_drive_and_says_so(monkeypatch):
    exc = RefreshError("invalid_grant: Token has been expired or revoked.")
    backup, _world, owner = _run_failing_backup(monkeypatch, exc)
    assert backup.status is BackupStatus.FAILED
    assert backup.error == DRIVE_REVOKED_MESSAGE
    assert owner.drive_linked is False
    assert owner.drive_refresh_token_enc is None


def test_manual_failure_does_not_touch_the_auto_counter(monkeypatch):
    _backup, world, _owner = _run_failing_backup(
        monkeypatch, RuntimeError("x"), kind=BackupKind.MANUAL
    )
    assert world.auto_backup_failures == 2
    assert world.auto_backup_last_attempt_at is None
