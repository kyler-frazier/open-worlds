"""Tests for auto-backup skip-when-unchanged: the data-dir fingerprint and the
run_backup skip branch. Follows the suite convention of mocking the DB/Drive."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from open_worlds.common.models import BackupKind, BackupStatus
from open_worlds.worker import backups
from open_worlds.worker.backups import _fingerprint_dir

# --- fingerprint helper ---------------------------------------------------


def test_fingerprint_missing_dir_is_none(tmp_path):
    assert _fingerprint_dir(str(tmp_path / "nope")) is None


def test_fingerprint_stable_for_identical_content(tmp_path):
    (tmp_path / "world").mkdir()
    (tmp_path / "world" / "level.dat").write_bytes(b"hello")
    (tmp_path / "server.properties").write_text("level-name=world\n")
    first = _fingerprint_dir(str(tmp_path))
    second = _fingerprint_dir(str(tmp_path))
    assert first is not None
    assert first == second


def test_fingerprint_changes_when_a_file_changes(tmp_path):
    (tmp_path / "world").mkdir()
    f = tmp_path / "world" / "level.dat"
    f.write_bytes(b"hello")
    before = _fingerprint_dir(str(tmp_path))
    # Rewrite with different size + a bumped mtime.
    f.write_bytes(b"hello world!!")
    import os

    os.utime(f, (1_000_000, 1_000_000))
    assert _fingerprint_dir(str(tmp_path)) != before


def test_fingerprint_changes_when_a_file_is_added(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    before = _fingerprint_dir(str(tmp_path))
    (tmp_path / "b.txt").write_text("b")
    assert _fingerprint_dir(str(tmp_path)) != before


# --- run_backup skip branch ----------------------------------------------


class _FakeDB:
    def __init__(self, objs):
        self._objs = objs
        self.deleted: list = []
        self.commits = 0

    def get(self, _model, key):
        return self._objs.get(key)

    def delete(self, obj):
        self.deleted.append(obj)

    def commit(self):
        self.commits += 1


def _linked_world(wid, fingerprint):
    owner = SimpleNamespace(drive_linked=True, drive_refresh_token_enc="enc")
    return SimpleNamespace(
        id=wid,
        subdomain="w1",
        # The disk guard sizes itself from the game's import policy.
        game="MINECRAFT",
        owner=owner,
        last_auto_backup_at=None,
        last_auto_backup_fingerprint=fingerprint,
        auto_backup_failures=0,
        auto_backup_last_attempt_at=None,
    )


def test_auto_backup_skipped_when_fingerprint_unchanged(monkeypatch):
    bid, wid = uuid.uuid4(), uuid.uuid4()
    backup = SimpleNamespace(
        id=bid, world_id=wid, kind=BackupKind.AUTO, status=BackupStatus.PENDING, filename="f"
    )
    world = _linked_world(wid, fingerprint="abc123")
    db = _FakeDB({bid: backup, wid: world})
    monkeypatch.setattr(backups, "fingerprint_data_dir", lambda _sub: "abc123")

    # Would raise if we reached the Drive path; skip must return before that.
    monkeypatch.setattr(
        backups, "DriveClient", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no drive"))
    )

    backups.run_backup(db, bid)

    assert backup in db.deleted
    assert world.last_auto_backup_at is not None


def test_auto_backup_not_skipped_when_fingerprint_differs(monkeypatch):
    bid, wid = uuid.uuid4(), uuid.uuid4()
    backup = SimpleNamespace(
        id=bid, world_id=wid, kind=BackupKind.AUTO, status=BackupStatus.PENDING, filename="f"
    )
    world = _linked_world(wid, fingerprint="old")
    db = _FakeDB({bid: backup, wid: world})
    monkeypatch.setattr(backups, "fingerprint_data_dir", lambda _sub: "new")
    monkeypatch.setattr(backups.os, "makedirs", lambda *_a, **_k: None)
    # Fail fast the moment we move past the skip check into real work.
    monkeypatch.setattr(
        backups,
        "_check_free_space",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("proceeded")),
    )
    monkeypatch.setattr(backups, "_prune_failed_backups", lambda *_a, **_k: 0)

    # The sentinel propagates now: a failed backup fails its job rather than
    # reporting success.
    with pytest.raises(RuntimeError, match="proceeded"):
        backups.run_backup(db, bid)

    # Did not skip: backup was not deleted, and it was marked FAILED by _fail
    # after our sentinel raised (proving execution continued past the skip).
    assert backup not in db.deleted
    assert backup.status is BackupStatus.FAILED
