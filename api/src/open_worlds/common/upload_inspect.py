"""Inspect and classify uploaded world archives.

Shared by the API's preflight (from the archive listing) and the worker's
prune (from the extracted files), so both agree on what is kept. The rules
come from the game's ImportPolicy: a top-level entry at the data root must be
known or look like a world, and nothing nested may be loadable code or in a
partially kept subtree's discarded half.
"""

from __future__ import annotations

import hashlib
import os
import tarfile
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .games import ImportPolicy
from .world_icon import (
    ICON_MAX_BYTES,
    IconChange,
    level_icon_relpath,
    parse_level_name,
    plan_icon_changes,
)

# OS cruft (macOS __MACOSX/ and ._* files, Windows Thumbs.db). Never blocks
# wrapper descent, never reported, never written into a world.
_JUNK_BASENAMES: frozenset[str] = frozenset({".ds_store", "thumbs.db", "desktop.ini"})
_JUNK_PATH_COMPONENTS: frozenset[str] = frozenset({"__macosx"})


def is_junk_path(name: str) -> bool:
    """True if an archive member or filesystem name is OS cruft, not world data."""
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    if not parts:
        return False
    if any(p.lower() in _JUNK_PATH_COMPONENTS for p in parts):
        return True
    base = parts[-1]
    return base.lower() in _JUNK_BASENAMES or base.startswith("._")


@dataclass(frozen=True)
class RootEntry:
    name: str
    is_dir: bool
    world_like: bool


@dataclass(frozen=True)
class UploadInspection:
    total_bytes: int
    file_count: int
    entries: list[RootEntry]
    # Nested files a prune would strip from a kept directory, relative to the
    # data root.
    nested_payloads: list[str]
    # Paths an allow-list will drop, as they will land in the world.
    disallowed_paths: list[str] = field(default_factory=list)


def _read_members(path: str) -> list[tuple[str, int, bool]]:
    """Return (name, size, is_dir) for each archive member. .zip or .tar[.*]."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            return [(i.filename, i.file_size, i.is_dir()) for i in zf.infolist()]
    try:
        with tarfile.open(path, "r:*") as tf:
            return [(m.name, m.size, m.isdir()) for m in tf.getmembers()]
    except tarfile.TarError as exc:
        raise ValueError("Unsupported archive format; use .zip or .tar.gz") from exc


@dataclass(frozen=True)
class ArchiveWorld:
    """A world directory found in an upload, described well enough to choose between."""

    #: Posix path of the directory, relative to the archive root.
    path: str
    #: Size of the marker file, which tracks the world's size.
    size_bytes: int
    #: When the marker file was last written, as YYYY-MM-DD, or "" if unknown.
    modified: str

    def describe(self) -> str:
        """One line a human can pick from."""
        name = self.path.rsplit("/", 1)[-1] or self.path
        # KB below a megabyte: "0.0 MB" reads as a bug.
        if self.size_bytes < 1024 * 1024:
            size = f"{self.size_bytes / 1024:.0f} KB"
        else:
            size = f"{self.size_bytes / 1024 / 1024:.1f} MB"
        when = f", modified {self.modified}" if self.modified else ""
        return f"{name} ({size}{when})"


def worlds_in_archive(path: str, policy: ImportPolicy) -> list[ArchiveWorld]:
    """Directories in an archive that hold the policy's world marker.

    Metadata only: no save is decompressed or parsed. The marker's size and
    date are enough to tell a real world from an old test one.
    """
    marker = policy.bare_world_marker.lower()
    found: dict[str, ArchiveWorld] = {}

    def record(member_name: str, size: int, modified: str) -> None:
        parts = _norm(member_name)
        if len(parts) < 2 or parts[-1].lower() != marker:
            return
        directory = "/".join(parts[:-1])
        # A game's own snapshot of a save is not another world.
        excluded = {*policy.backup_exclude_dirs, "backup"}
        if any(p.lower() in excluded for p in parts[:-1]):
            return
        found.setdefault(directory, ArchiveWorld(directory, size, modified))

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                y, m, d = info.date_time[:3]
                record(info.filename, info.file_size, f"{y:04d}-{m:02d}-{d:02d}")
    else:
        try:
            with tarfile.open(path, "r:*") as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    stamp = datetime.fromtimestamp(member.mtime, tz=UTC).strftime("%Y-%m-%d")
                    record(member.name, member.size, stamp)
        except tarfile.TarError as exc:
            raise ValueError("Unsupported archive format; use .zip or .tar.gz") from exc

    return sorted(found.values(), key=lambda w: w.path)


def _basename(name: str) -> str:
    parts = _norm(name)
    return parts[-1].lower() if parts else ""


def read_member_text(path: str, basename: str, max_bytes: int = 1 << 20) -> str | None:
    """Text of the first member with this basename, or None. Size-capped:
    the archive is untrusted. For showing an upload's settings before commit."""
    wanted = basename.lower()
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir() or _basename(info.filename) != wanted:
                    continue
                if info.file_size > max_bytes:
                    return None
                return zf.read(info).decode("utf-8", errors="replace")
        return None
    try:
        with tarfile.open(path, "r:*") as tf:
            for member in tf.getmembers():
                if not member.isfile() or _basename(member.name) != wanted:
                    continue
                if member.size > max_bytes:
                    return None
                fh = tf.extractfile(member)
                if fh is None:
                    return None
                return fh.read().decode("utf-8", errors="replace")
    except tarfile.TarError as exc:
        raise ValueError("Unsupported archive format; use .zip or .tar.gz") from exc
    return None


def _norm(name: str) -> tuple[str, ...]:
    return tuple(p for p in name.replace("\\", "/").split("/") if p not in ("", ".", ".."))


def _analyze(
    members: list[tuple[str, int, bool]],
) -> tuple[list[tuple[str, ...]], set[tuple[str, ...]], int, int]:
    file_parts: list[tuple[str, ...]] = []
    dir_set: set[tuple[str, ...]] = set()
    total = 0
    file_count = 0
    for name, size, is_dir in members:
        if is_junk_path(name):
            continue
        parts = _norm(name)
        if not parts:
            continue
        if is_dir:
            dir_set.add(parts)
            continue
        file_parts.append(parts)
        total += size
        file_count += 1
        for i in range(1, len(parts)):  # every ancestor path is a directory
            dir_set.add(parts[:i])
    return file_parts, dir_set, total, file_count


def _children(
    prefix: tuple[str, ...],
    all_parts: set[tuple[str, ...]],
    dir_set: set[tuple[str, ...]],
) -> dict[str, bool]:
    """Immediate children under ``prefix`` mapped to whether each is a directory."""
    kids: dict[str, bool] = {}
    plen = len(prefix)
    for parts in all_parts:
        if len(parts) <= plen or parts[:plen] != prefix:
            continue
        child = parts[plen]
        is_dir = (prefix + (child,)) in dir_set or len(parts) > plen + 1
        kids[child] = kids.get(child, False) or is_dir
    return kids


def _world_like_archive(
    prefix: tuple[str, ...],
    child: str,
    all_parts: set[tuple[str, ...]],
    policy: ImportPolicy,
) -> bool:
    base = prefix + (child,)
    blen = len(base)
    for parts in all_parts:
        if (
            len(parts) > blen
            and parts[:blen] == base
            and parts[blen].lower() in policy.world_child_markers
        ):
            return True
    return False


def _data_root_prefix(
    all_parts: set[tuple[str, ...]], dir_set: set[tuple[str, ...]], policy: ImportPolicy
) -> tuple[str, ...]:
    """Descend single-directory wrappers to the archive's data root."""
    prefix: tuple[str, ...] = ()
    for _ in range(4):
        kids = _children(prefix, all_parts, dir_set)
        if not kids or any(k.lower() in policy.data_root_markers for k in kids):
            break
        subdirs = [k for k, is_dir in kids.items() if is_dir]
        if len(kids) == 1 and len(subdirs) == 1:
            prefix = prefix + (subdirs[0],)
            continue
        break
    return prefix


def _read_member_bytes(path: str, name: str, max_bytes: int) -> bytes | None:
    """The bytes of one archive member by its exact name, capped; None if absent."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            try:
                with zf.open(name) as fh:
                    return fh.read(max_bytes)
            except KeyError:
                return None
    try:
        with tarfile.open(path, "r:*") as tf:
            try:
                member = tf.getmember(name)
            except KeyError:
                return None
            extracted = tf.extractfile(member) if member.isfile() else None
            return extracted.read(max_bytes) if extracted else None
    except tarfile.TarError as exc:
        raise ValueError("Unsupported archive format; use .zip or .tar.gz") from exc


def archive_icon_changes(path: str, policy: ImportPolicy) -> list[IconChange]:
    """What importing this archive will do to its icons, for preflight: each
    file resolved where it will land, then the worker's own planner."""
    if not policy.icon_relpath or not policy.level_icon_name:
        return []
    members = _read_members(path)
    file_parts, dir_set, _, _ = _analyze(members)
    all_parts: set[tuple[str, ...]] = set(file_parts) | dir_set
    prefix = _data_root_prefix(all_parts, dir_set, policy)
    placement = policy.placement_for(list(_children(prefix, all_parts, dir_set)))
    plen = len(prefix)
    # Where each file lands (lowercased) -> its archive name.
    landed: dict[str, str] = {}
    for name, _size, is_dir in members:
        parts = _norm(name)
        if is_dir or is_junk_path(name) or parts[:plen] != prefix or len(parts) == plen:
            continue
        rel = "/".join(parts[plen:])
        landed.setdefault((f"{placement}/{rel}" if placement else rel).lower(), name)

    def read(rel: str, cap: int = ICON_MAX_BYTES + 1) -> bytes | None:
        name = landed.get(rel.lower())
        return _read_member_bytes(path, name, cap) if name else None

    props = read("server.properties", 1 << 20)
    level = parse_level_name(props.decode("utf-8", errors="replace") if props else None)
    save_rel = level_icon_relpath(level, policy)
    if save_rel is None:
        return []
    return plan_icon_changes(policy, read(policy.icon_relpath), read(save_rel), save_rel)


def inspect_archive(path: str, policy: ImportPolicy) -> UploadInspection:
    """Inspect an archive without extracting it: totals + data-root entries."""
    members = _read_members(path)
    file_parts, dir_set, total, file_count = _analyze(members)
    all_parts: set[tuple[str, ...]] = set(file_parts) | dir_set

    prefix = _data_root_prefix(all_parts, dir_set, policy)
    kids = _children(prefix, all_parts, dir_set)
    entries = [
        RootEntry(
            name=name,
            is_dir=is_dir,
            world_like=is_dir and _world_like_archive(prefix, name, all_parts, policy),
        )
        for name, is_dir in sorted(kids.items())
    ]

    # Below the top level (classify covers that), relative to the data root.
    plen = len(prefix)
    nested_payloads = sorted(
        rel
        for parts in file_parts
        if parts[:plen] == prefix
        and len(parts) - plen >= 2
        and policy.is_prunable_nested(rel := "/".join(parts[plen:]))
    )
    # Judged where each file will land; the patterns describe the world dir.
    disallowed: list[str] = []
    if policy.allowed_paths:
        placement = policy.placement_for([e.name for e in entries])
        for parts in file_parts:
            if parts[:plen] != prefix or len(parts) == plen:
                continue
            rel = "/".join(parts[plen:])
            placed = f"{placement}/{rel}" if placement else rel
            if not policy.path_allowed(placed):
                disallowed.append(placed)
    # An icon too large to be one: its path is allowed, and the exact check
    # needs its bytes, but the size is known now.
    if policy.icon_relpath:
        placement = policy.placement_for([e.name for e in entries])
        icon = policy.icon_relpath.lower()
        for name, size, is_dir in members:
            parts = _norm(name)
            if is_dir or parts[:plen] != prefix or len(parts) == plen:
                continue
            rel = "/".join(parts[plen:])
            placed = f"{placement}/{rel}" if placement else rel
            if placed.lower() == icon and size > ICON_MAX_BYTES and placed not in disallowed:
                disallowed.append(placed)

    return UploadInspection(
        total_bytes=total,
        file_count=file_count,
        entries=entries,
        nested_payloads=nested_payloads,
        disallowed_paths=sorted(disallowed),
    )


def inspect_dir(data_root: str, policy: ImportPolicy) -> list[RootEntry]:
    """Classify the top-level entries of an already-extracted data root."""
    out: list[RootEntry] = []
    for name in sorted(os.listdir(data_root)):
        if is_junk_path(name):
            continue
        full = os.path.join(data_root, name)
        is_dir = os.path.isdir(full) and not os.path.islink(full)
        world_like = False
        if is_dir:
            children = {c.lower() for c in os.listdir(full)}
            world_like = bool(children & policy.world_child_markers)
        out.append(RootEntry(name=name, is_dir=is_dir, world_like=world_like))
    return out


def classify(
    entries: list[RootEntry], policy: ImportPolicy, extra_allowed: list[str] | None = None
) -> tuple[list[str], list[str]]:
    """Split entries into (allowed, unknown): known for the game, allowed by
    the admin, or a directory that looks like a world. Unknown is prunable."""
    # The data root is itself one world, so nothing here is an extra.
    if any(e.name.lower() == policy.bare_world_marker.lower() for e in entries):
        return [e.name for e in entries], []

    extra = {e.strip().lower() for e in (extra_allowed or []) if e.strip()}
    allowed: list[str] = []
    unknown: list[str] = []
    for e in entries:
        name_lower = e.name.lower()
        if (
            name_lower in policy.known_root_entries
            or name_lower in extra
            or (e.is_dir and e.world_like)
        ):
            allowed.append(e.name)
        else:
            unknown.append(e.name)
    return allowed, unknown


def _summarize_payloads(paths: list[str]) -> list[str]:
    """Group nested prunable paths by top-level dir into lines like
    ``Pal/ (18422 files, will be removed)``."""
    counts: dict[str, int] = {}
    for p in paths:
        top = p.split("/", 1)[0]
        counts[top] = counts.get(top, 0) + 1
    lines: list[str] = []
    for top in sorted(counts):
        n = counts[top]
        noun = "file" if n == 1 else "files"
        lines.append(f"{top}/ ({n} {noun}, will be removed)")
    return lines


def prunable_report(
    inspection: UploadInspection,
    policy: ImportPolicy,
    extra_allowed: list[str] | None = None,
) -> list[str]:
    """Everything a prune would strip from this upload, for the user. Empty
    means a bare world. Gates the ``prune=true`` confirmation."""
    allowed, unknown = classify(inspection.entries, policy, extra_allowed)
    allowed_lower = {a.lower() for a in allowed}
    # Under kept dirs only; an unknown dir is already listed whole.
    under_kept = [
        j for j in inspection.nested_payloads if j.split("/", 1)[0].lower() in allowed_lower
    ]
    # For an allow-list game, most of what goes.
    return (
        list(unknown)
        + _summarize_payloads(under_kept)
        + _summarize_payloads(inspection.disallowed_paths)
    )


def plan_dir_prune(
    root: str,
    policy: ImportPolicy,
    extra_allowed: list[str] | None = None,
    managed_hashes: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Plan a prune of an extracted data root: ``(remove_paths, report)``,
    paths relative to ``root``. ``managed_hashes`` (sha512) exempts files the
    platform installed, so a curated mod survives a re-import."""
    entries = inspect_dir(root, policy)
    allowed, unknown = classify(entries, policy, extra_allowed)
    remove: list[str] = list(unknown)
    report: list[str] = list(unknown)

    managed = managed_hashes or set()
    payloads: list[str] = []
    for name in allowed:
        full = os.path.join(root, name)
        if not os.path.isdir(full) or os.path.islink(full):
            continue
        for dirpath, _dirs, files in os.walk(full, followlinks=False):
            for fname in files:
                fpath = os.path.join(dirpath, fname)
                if os.path.islink(fpath):
                    continue
                rel = os.path.relpath(fpath, root).replace(os.sep, "/")
                if not policy.is_prunable_nested(rel):
                    continue
                if managed and _sha512_file(fpath) in managed:
                    continue
                payloads.append(rel)

    remove.extend(payloads)
    report.extend(_summarize_payloads(payloads))
    return remove, report


def _sha512_file(path: str) -> str:
    h = hashlib.sha512()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
