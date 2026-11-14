"""Backups and imports, run by the worker.

A backup flushes the save if the world is running, archives the data dir to
local staging, uploads it to the owner's Drive and deletes the local copy.
Only AUTO backups are pruned by retention.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tarfile
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..common.config import get_settings
from ..common.crypto import decrypt
from ..common.drive import (
    DRIVE_REVOKED_MESSAGE,
    DriveClient,
    DriveFileTooLarge,
    unlink_if_revoked,
)
from ..common.games import AdminUnavailable, ImportPolicy, adapter_for
from ..common.logging import get_logger
from ..common.metrics import backup_bytes, backup_duration_seconds, backups_total
from ..common.models import Backup, BackupKind, BackupStatus, World
from ..common.orchestrator import RunState, make_orchestrator
from ..common.retention import BackupRef, select_backups_to_prune
from ..common.runtime_config import get_runtime_config
from ..common.upload_inspect import is_junk_path, plan_dir_prune
from ..common.world_icon import adopt_legacy_icons, enforce_icon, normalize_icons

log = get_logger(__name__)


class BackupError(Exception):
    pass


@dataclass(frozen=True)
class ExtractLimits:
    """Caps on what an archive may expand to."""

    max_total_bytes: int
    max_files: int


def upload_limits() -> ExtractLimits:
    cfg = get_runtime_config()
    return ExtractLimits(cfg.max_upload_bytes, cfg.max_upload_file_count)


def restore_limits() -> ExtractLimits:
    s = get_settings()
    return ExtractLimits(s.max_restore_bytes, s.max_restore_file_count)


def _enforce_limits(total_bytes: int, file_count: int, limits: ExtractLimits) -> None:
    if file_count > limits.max_files:
        raise BackupError(
            f"Upload contains too many files ({file_count}); the limit is {limits.max_files}."
        )
    if total_bytes > limits.max_total_bytes:
        raise BackupError(
            f"Upload is too large uncompressed ({total_bytes} bytes); "
            f"the limit is {limits.max_total_bytes} bytes."
        )


def _data_dir(subdomain: str) -> str:
    s = get_settings()
    return os.path.join(s.worlds_dir, subdomain, "data")


def _admin_host(subdomain: str) -> str:
    """A world container's address on the private network."""
    return f"{get_settings().container_name_prefix}{subdomain}"


def _archivable_bytes(data_dir: str, policy: ImportPolicy) -> int:
    """Roughly what an archive of this world will cost on disk: only what the
    backup filter keeps (the install is most of the bytes). Uncompressed, so an
    over-estimate."""
    total = 0
    for root, dirs, files in os.walk(data_dir):
        rel_root = os.path.relpath(root, data_dir)
        # Skip whole subtrees the filter drops, such as the install.
        dirs[:] = [
            d
            for d in dirs
            if not _is_filtered_out(
                os.path.join(rel_root, d) if rel_root != "." else d, policy, is_dir=True
            )
        ]
        for name in files:
            rel = os.path.join(rel_root, name) if rel_root != "." else name
            if _is_filtered_out(rel, policy):
                continue
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def _is_filtered_out(rel_posix: str, policy: ImportPolicy, *, is_dir: bool = False) -> bool:
    """Whether _backup_filter would drop this path. Kept in step with it."""
    rel_posix = rel_posix.replace(os.sep, "/")
    if not _restorable(rel_posix, policy, is_dir=is_dir):
        return True
    parts = rel_posix.split("/")
    top = parts[0].lower()
    if top in policy.backup_exclude_dirs:
        return True
    if len(parts) == 1 and (
        top in policy.backup_exclude_files or top.endswith(policy.executable_suffixes)
    ):
        return True
    if len(parts) >= 2 and policy.is_prunable_nested(rel_posix):
        return True
    return False


def _restorable(rel_posix: str, policy: ImportPolicy, *, is_dir: bool) -> bool:
    """Whether a restore would keep this path, for games with an allow-list.
    Anything else would be archived only to be deleted on restore (Palworld's
    own per-autosave snapshots were 15x the save)."""
    if is_dir:
        return policy.dir_may_hold_allowed(rel_posix)
    return policy.path_allowed(rel_posix)


def _check_free_space(staging_dir: str, needed_bytes: int = 0) -> None:
    """Refuse work that would not fit: this world's bytes plus a fixed headroom
    for the host, since worlds write to the same disk."""
    s = get_settings()
    usage = shutil.disk_usage(staging_dir)
    required = s.min_free_bytes_for_backup + needed_bytes
    if usage.free < required:
        raise BackupError(
            f"Insufficient free disk space: {usage.free} bytes free, "
            f"require {required} ({needed_bytes} for this world plus "
            f"{s.min_free_bytes_for_backup} headroom)"
        )


def _fingerprint_dir(path: str) -> str | None:
    """sha256 over every file's (path, size, mtime): equal iff a backup would
    be identical. Stat-only. None for a missing dir, so no data never counts
    as unchanged."""
    if not os.path.isdir(path):
        return None
    h = hashlib.sha256()
    for root, dirs, files in os.walk(path):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, path)
            try:
                st = os.stat(full)
            except OSError:
                continue
            h.update(rel.encode("utf-8", "surrogatepass"))
            h.update(f"\0{st.st_size}\0{st.st_mtime_ns}\0".encode())
    return h.hexdigest()


def fingerprint_data_dir(subdomain: str) -> str | None:
    return _fingerprint_dir(_data_dir(subdomain))


def _backup_filter(policy: ImportPolicy, ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """Drop what the game can regenerate (names are ``data/...``), so a backup
    is the size of the world, not of the install."""
    parts = ti.name.split("/")
    if len(parts) >= 2:
        top = parts[1].lower()
        if top in policy.backup_exclude_dirs:
            return None
        # Loose executables at the data root are regenerable, and an import
        # would strip them anyway.
        if len(parts) == 2 and (
            top in policy.backup_exclude_files or top.endswith(policy.executable_suffixes)
        ):
            return None
        # Anything the import would prune inside a kept directory (Pal/ keeps
        # only Saved/). Depth 2 and below: the kept top-level directory itself
        # must stay. Excluding a directory also stops tarfile walking it.
        if len(parts) >= 3 and policy.is_prunable_nested("/".join(parts[1:])):
            return None
        if not _restorable("/".join(parts[1:]), policy, is_dir=ti.isdir()):
            return None
    return ti


def _archive(subdomain: str, staging_path: str, policy: ImportPolicy) -> int:
    data_dir = _data_dir(subdomain)
    if not os.path.isdir(data_dir):
        raise BackupError(f"World data directory missing: {data_dir}")
    with tarfile.open(staging_path, "w:gz") as tar:
        tar.add(data_dir, arcname="data", filter=partial(_backup_filter, policy))
    return os.path.getsize(staging_path)


def run_backup(db: Session, backup_id: uuid.UUID) -> None:
    s = get_settings()
    backup = db.get(Backup, backup_id)
    if backup is None:
        log.warning("backup record missing", extra={"backup_id": str(backup_id)})
        return
    world = db.get(World, backup.world_id)
    if world is None:
        _fail(db, backup, "World no longer exists")
        return
    owner = world.owner
    if not owner or not owner.drive_linked or not owner.drive_refresh_token_enc:
        _fail(db, backup, "Owner has not linked Google Drive")
        return

    # An AUTO backup of unchanged data (a world off since the last one) is
    # skipped. MANUAL backups always run.
    fingerprint = None
    if backup.kind is BackupKind.AUTO:
        fingerprint = fingerprint_data_dir(world.subdomain)
        if fingerprint is not None and fingerprint == world.last_auto_backup_fingerprint:
            world.last_auto_backup_at = datetime.now(UTC)
            db.delete(backup)
            db.commit()
            backups_total.labels(kind=BackupKind.AUTO.value, result="skipped").inc()
            log.info(
                "auto-backup skipped; data unchanged since last backup",
                extra={"world_id": str(world.id)},
            )
            return

    backup.status = BackupStatus.RUNNING
    if backup.kind is BackupKind.AUTO:
        # Every attempt: the failure backoff runs against it.
        world.auto_backup_last_attempt_at = datetime.now(UTC)
    db.commit()

    os.makedirs(s.backup_staging_dir, exist_ok=True)
    staging_path = os.path.join(s.backup_staging_dir, f"{backup.id}.tar.gz")
    started = time.monotonic()
    kind_label = backup.kind.value
    try:
        policy = adapter_for(world.game).import_policy
        _check_free_space(
            s.backup_staging_dir, _archivable_bytes(_data_dir(world.subdomain), policy)
        )
        _freeze_and_archive(world, staging_path)

        refresh_token = decrypt(owner.drive_refresh_token_enc)
        drive = DriveClient(refresh_token)
        world_folder = drive.world_folder(world.game, world.subdomain)
        file_id = drive.upload(staging_path, backup.filename, world_folder)

        size = os.path.getsize(staging_path)
        backup.size_bytes = size
        backup.drive_file_id = file_id
        backup.status = BackupStatus.SUCCESS
        backup.completed_at = datetime.now(UTC)
        if backup.kind is BackupKind.AUTO:
            world.last_auto_backup_at = backup.completed_at
            world.auto_backup_failures = 0
            world.last_auto_backup_fingerprint = fingerprint
        db.commit()

        backup_bytes.observe(size)
        backups_total.labels(kind=kind_label, result="success").inc()
        log.info(
            "backup complete",
            extra={"world_id": str(world.id), "kind": kind_label, "size_bytes": size},
        )

        if backup.kind is BackupKind.AUTO:
            _prune_auto_backups(db, world, drive)
    except Exception as exc:
        backups_total.labels(kind=kind_label, result="error").inc()
        message = str(exc)
        if unlink_if_revoked(exc, owner):
            # A dead token fails every attempt: unlinking tells the user and
            # stops the schedule.
            message = DRIVE_REVOKED_MESSAGE
            log.warning("drive token revoked; unlinked", extra={"user_id": str(owner.id)})
        if backup.kind is BackupKind.AUTO:
            world.auto_backup_failures += 1
        _fail(db, backup, message)
        _prune_failed_backups(db, world)
        log.exception("backup failed", extra={"world_id": str(world.id)})
        # So the job fails too and the failed-job alert fires.
        raise
    finally:
        backup_duration_seconds.labels(kind=kind_label).observe(time.monotonic() - started)
        if os.path.exists(staging_path):
            os.remove(staging_path)


def _freeze_and_archive(world: World, staging_path: str) -> None:
    """Archive with a save flush if the container is running, else cold.

    Falls back to a cold archive if the server cannot be reached: missing the
    last few seconds beats no backup.
    """
    adapter = adapter_for(world.game)
    # The factory: the worker has no Docker access of its own.
    orch = make_orchestrator()
    running = orch.inspect(world.subdomain).run_state is RunState.RUNNING

    if not running or not world.admin_password_enc:
        _archive(world.subdomain, staging_path, adapter.import_policy)
        return

    admin = adapter.admin_client(_admin_host(world.subdomain), decrypt(world.admin_password_enc))
    try:
        admin.save_flush()
    except AdminUnavailable as exc:
        log.warning("save flush failed, cold archiving", extra={"error": str(exc)})
        _archive(world.subdomain, staging_path, adapter.import_policy)
        return
    try:
        _archive(world.subdomain, staging_path, adapter.import_policy)
    finally:
        # Always paired with save_flush (a no-op for Palworld).
        admin.save_resume()
        admin.close()


def _prune_auto_backups(db: Session, world: World, drive: DriveClient) -> None:
    rows = list(
        db.execute(
            select(Backup).where(Backup.world_id == world.id, Backup.status == BackupStatus.SUCCESS)
        ).scalars()
    )
    refs = [BackupRef(id=str(b.id), kind=b.kind.value, created_at=b.created_at) for b in rows]
    to_prune_ids = {r.id for r in select_backups_to_prune(refs, world.auto_backup_max)}
    for b in rows:
        if str(b.id) in to_prune_ids:
            if b.drive_file_id:
                try:
                    drive.delete(b.drive_file_id)
                except Exception as exc:
                    log.warning("failed to delete drive file", extra={"error": str(exc)})
            db.delete(b)
    db.commit()


# FAILED rows kept per world: enough to see a pattern, bounded.
FAILED_BACKUPS_KEPT = 5


def _prune_failed_backups(db: Session, world: World, keep: int = FAILED_BACKUPS_KEPT) -> int:
    """Drop all but the newest ``keep`` FAILED backup rows for a world."""
    rows = list(
        db.execute(
            select(Backup)
            .where(Backup.world_id == world.id, Backup.status == BackupStatus.FAILED)
            .order_by(Backup.created_at.desc())
        ).scalars()
    )
    for stale in rows[keep:]:
        db.delete(stale)
    if len(rows) > keep:
        db.commit()
    return max(0, len(rows) - keep)


def _fail(db: Session, backup: Backup, message: str) -> None:
    backup.status = BackupStatus.FAILED
    backup.error = message[:2000]
    backup.completed_at = datetime.now(UTC)
    db.commit()


def run_import(db: Session, world_id: uuid.UUID, payload: str) -> None:
    """Populate a world's data directory from an uploaded file or a Drive backup.

    ``payload`` is JSON: ``{"source": "file", "path": ...}`` for a staged upload,
    or ``{"source": "drive", "backup_id": ...}``. The world must be OFF.
    """
    world = db.get(World, world_id)
    if world is None:
        log.warning("import target world missing", extra={"world_id": str(world_id)})
        return

    spec = _parse_import_payload(payload)
    prune = bool(spec.get("prune"))
    cleanup_path: str | None = None
    try:
        if spec["source"] == "file":
            archive_path = spec["path"]
            cleanup_path = archive_path
            # Preflight already capped a staged upload.
            limits: ExtractLimits | None = None if spec.get("skip_limits") else upload_limits()
        else:
            archive_path, cleanup_path = _download_drive_backup(db, world, spec["backup_id"])
            # Our own backups may exceed the upload cap, but the file in Drive
            # is the owner's to replace, so it is still capped.
            limits = restore_limits()
        # Platform-managed files (by sha512) survive the prune and are put
        # back after, so a re-import keeps a world's curated content.
        adapter = adapter_for(world.game)
        managed = adapter.managed_content_hashes(db, world)
        _restore_into_world(world, archive_path, limits=limits, prune=prune, managed=managed)
        adapter.after_restore(db, world)
        log.info("import complete", extra={"world_id": str(world_id)})
    finally:
        if cleanup_path and os.path.exists(cleanup_path):
            os.remove(cleanup_path)


def _parse_import_payload(payload: str) -> dict[str, str]:
    # A bare string payload is an older file path.
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return {"source": "file", "path": payload}
    if not isinstance(data, dict) or "source" not in data:
        return {"source": "file", "path": payload}
    return data


def _download_drive_backup(db: Session, world: World, backup_id: str) -> tuple[str, str]:
    """Fetch a backup file from the owner's Drive into local staging."""
    s = get_settings()
    backup = db.get(Backup, uuid.UUID(str(backup_id)))
    if backup is None or backup.drive_file_id is None:
        raise BackupError("Backup not found or not stored on Drive")
    owner = world.owner
    if not owner or not owner.drive_refresh_token_enc:
        raise BackupError("Owner has not linked Google Drive")
    os.makedirs(s.backup_staging_dir, exist_ok=True)
    dest = os.path.join(s.backup_staging_dir, f"restore-{world.id}-{uuid.uuid4().hex}.archive")
    drive = DriveClient(decrypt(owner.drive_refresh_token_enc))
    _check_free_space(s.backup_staging_dir)
    try:
        drive.download(backup.drive_file_id, dest, max_bytes=s.max_restore_bytes)
    except Exception as exc:
        if os.path.exists(dest):
            os.remove(dest)
        if isinstance(exc, DriveFileTooLarge):
            raise BackupError("The Drive backup is larger than a restore allows.") from exc
        if unlink_if_revoked(exc, owner):
            db.commit()
            raise BackupError(DRIVE_REVOKED_MESSAGE) from exc
        raise
    return dest, dest


def _prune_unknown(root: str, policy: ImportPolicy, managed_hashes: set[str] | None = None) -> None:
    """Strip an extracted data root to the world, per the game's ImportPolicy.
    ``managed_hashes`` exempts files the platform installed itself."""
    extra = get_runtime_config().extra_allowed_root_names
    remove, report = plan_dir_prune(root, policy, extra, managed_hashes=managed_hashes)
    for rel in remove:
        full = os.path.join(root, rel)
        if os.path.isdir(full) and not os.path.islink(full):
            shutil.rmtree(full, ignore_errors=True)
        else:
            try:
                os.remove(full)
            except OSError:
                pass
    if report:
        log.info("pruned non-essential upload content", extra={"pruned": report})


def _restore_into_world(
    world: World,
    archive_path: str,
    limits: ExtractLimits | None = None,
    prune: bool = False,
    managed: set[str] | None = None,
) -> None:
    """Replace the world's data dir with an archive's world data.

    Always a replace, never a merge (a merge left two Palworld saves side by
    side). The old data is wiped only after extraction succeeds. With
    ``prune=True`` unknown top-level entries are dropped.
    """
    data_dir = _data_dir(world.subdomain)
    # Compressed world data expands, so the archive's size is only a floor.
    try:
        archive_bytes = os.path.getsize(archive_path)
    except OSError:
        archive_bytes = 0
    _check_free_space(
        get_settings().backup_staging_dir,
        archive_bytes * get_settings().import_expansion_factor,
    )
    with tempfile.TemporaryDirectory(dir=get_settings().backup_staging_dir) as tmp:
        _extract_archive(archive_path, tmp, limits=limits)
        policy = adapter_for(world.game).import_policy
        root = _find_data_root(tmp, policy)
        if prune:
            _prune_unknown(root, policy, managed_hashes=managed)
        _clear_dir(data_dir)
        # People archive their world at any depth; the game says where it goes.
        dest = os.path.join(data_dir, policy.placement_for(os.listdir(root)))
        os.makedirs(dest, exist_ok=True)
        for name in os.listdir(root):
            src = os.path.join(root, name)
            target = os.path.join(dest, name)
            if os.path.exists(target):
                if os.path.isdir(target) and not os.path.islink(target):
                    shutil.rmtree(target)
                else:
                    os.remove(target)
            shutil.move(src, target)

        # Before the allow-list, so the icon is judged at the path it now uses.
        for rel in adopt_legacy_icons(data_dir, policy):
            log.info("moved an imported world icon from its legacy path", extra={"path": rel})
        _enforce_allowed_paths(data_dir, policy)
        # The allow-list vouches for the icon's path, not its contents.
        if enforce_icon(data_dir, policy):
            log.info("removed an imported world icon that is not a 64x64 PNG")
        # Then one icon, in the game's own path, as the preflight said.
        for change in normalize_icons(data_dir, policy):
            log.info(
                "normalised imported world icon",
                extra={"action": change.action, "path": change.path, "to": change.to},
            )


def _enforce_allowed_paths(data_dir: str, policy: ImportPolicy) -> list[str]:
    """Delete anything the policy does not name. Returns what was removed.

    After placement, when paths are the canonical ones the patterns describe,
    and the last step before the world is live. A no-op without allowed_paths.
    """
    if not policy.allowed_paths:
        return []
    removed: list[str] = []
    emptied: set[str] = set()
    for dirpath, _dirs, files in os.walk(data_dir, followlinks=False):
        for fname in files:
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, data_dir).replace(os.sep, "/")
            if policy.path_allowed(rel):
                continue
            try:
                os.remove(full)
                removed.append(rel)
                emptied.add(dirpath)
            except OSError:
                log.warning("could not remove disallowed path", extra={"path": rel})
    _remove_emptied_dirs(data_dir, emptied)
    if removed:
        log.info(
            "removed paths the import policy does not allow",
            extra={"count": len(removed), "sample": sorted(removed)[:10]},
        )
    return removed


def _remove_emptied_dirs(root: str, emptied: set[str]) -> None:
    """Drop directories this prune emptied, and ancestors left bare. Only those:
    an upload may hold a legitimately empty directory."""
    for dirpath in sorted(emptied, key=len, reverse=True):
        current = dirpath
        while os.path.commonpath([root, current]) == root and current != root:
            try:
                if os.listdir(current):
                    break
                os.rmdir(current)
            except OSError:
                break
            current = os.path.dirname(current)


def _clear_dir(path: str) -> None:
    """Remove everything inside ``path`` (keeping the directory itself)."""
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
        return
    for name in os.listdir(path):
        full = os.path.join(path, name)
        if os.path.isdir(full) and not os.path.islink(full):
            shutil.rmtree(full, ignore_errors=True)
        else:
            os.remove(full)


def _zip_member_is_symlink(info: zipfile.ZipInfo) -> bool:
    """True if a zip entry is a Unix symlink (encoded in the high external attrs)."""
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def _check_expansion(
    dest: str, total_bytes: int, file_count: int, limits: ExtractLimits | None
) -> None:
    if limits is not None:
        _enforce_limits(total_bytes, file_count, limits)
    # The archive-size estimate before extraction misses a high-ratio bomb;
    # the declared total does not.
    os.makedirs(dest, exist_ok=True)
    _check_free_space(dest, total_bytes)


def _extract_archive(archive_path: str, dest: str, limits: ExtractLimits | None = None) -> None:
    """Extract a .zip or .tar[.gz/.bz2/.xz], refusing path traversal.

    ``limits`` and free disk are checked against the declared sizes before
    anything is written (zip bombs). zipfile refuses a member that inflates
    past its declared size, and a tar's sizes are the real ones. Symlinks are
    refused (zip) or neutralised (tar's ``data`` filter).
    """
    dest_abs = os.path.realpath(dest)

    def _is_safe(name: str) -> bool:
        target = os.path.realpath(os.path.join(dest, name))
        return target == dest_abs or target.startswith(dest_abs + os.sep)

    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as zf:
            # OS cruft (__MACOSX, .DS_Store), as preflight reported.
            infos = [i for i in zf.infolist() if not is_junk_path(i.filename)]
            files = [i for i in infos if not i.is_dir()]
            _check_expansion(dest, sum(i.file_size for i in files), len(files), limits)
            for i in infos:
                # zip has no safe symlink handling; refuse outright.
                if _zip_member_is_symlink(i):
                    raise BackupError(f"Archive contains a disallowed symlink: {i.filename}")
                if not _is_safe(i.filename):
                    raise BackupError(f"Unsafe path in archive: {i.filename}")
            zf.extractall(dest, members=[i.filename for i in infos])
        return

    try:
        with tarfile.open(archive_path, "r:*") as tar:
            members = [m for m in tar.getmembers() if not is_junk_path(m.name)]
            regular = [m for m in members if m.isreg()]
            _check_expansion(dest, sum(m.size for m in regular), len(regular), limits)
            for member in members:
                if not _is_safe(member.name):
                    raise BackupError(f"Unsafe path in archive: {member.name}")
            tar.extractall(dest, members=members, filter="data")
        return
    except tarfile.TarError as exc:
        raise BackupError("Unsupported archive format; use .zip or .tar.gz") from exc


def _find_data_root(root: str, policy: ImportPolicy) -> str:
    """Descend through single-directory wrappers (our backups' ``data/``, a
    zipped folder) to the server data root."""
    current = root
    for _ in range(4):
        # Ignore OS cruft, so a lone wrapper is still seen as one.
        entries = [e for e in os.listdir(current) if not is_junk_path(e)]
        if any(e.lower() in policy.data_root_markers for e in entries):
            return current
        subdirs = [e for e in entries if os.path.isdir(os.path.join(current, e))]
        if len(entries) == 1 and len(subdirs) == 1:
            current = os.path.join(current, subdirs[0])
            continue
        break
    return current
