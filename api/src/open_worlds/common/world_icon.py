"""A world's icon: one small PNG at a path the game names.

Minecraft's is the game's own ``server-icon.png``. Palworld has none, so its
icon is ours alone, kept in the save tree so backups carry it. Users can also
fill that directory by import, so the same exact check guards the upload route
and every import and restore.
"""

from __future__ import annotations

import os
import stat
import struct
from dataclasses import dataclass
from typing import Literal

from . import world_fs
from .games.base import ImportPolicy

ICON_SIZE_PX = 64
# A 64x64 RGBA image is 16 KB raw, so even a PNG that compresses nothing lands
# a little over that. 24 KB fits any honest 64x64 PNG.
ICON_MAX_BYTES = 24 * 1024

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class InvalidIcon(ValueError):
    """The bytes are not an icon we keep. The message is safe to show a user."""


def validate_icon(data: bytes) -> None:
    """Raise :class:`InvalidIcon` unless ``data`` is a 64x64 PNG within the cap.

    Reads only the signature and the IHDR chunk (which PNG requires first), so
    no image library is needed.
    """
    if len(data) > ICON_MAX_BYTES:
        raise InvalidIcon(f"The icon must be {ICON_MAX_BYTES // 1024} KB or smaller.")
    # 8 signature + 4 length + 4 type + 8 width/height.
    if len(data) < 24 or not data.startswith(_PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise InvalidIcon("The icon must be a PNG image.")
    width, height = struct.unpack(">II", data[16:24])
    if (width, height) != (ICON_SIZE_PX, ICON_SIZE_PX):
        raise InvalidIcon(
            f"The icon must be exactly {ICON_SIZE_PX}x{ICON_SIZE_PX} pixels "
            f"(this one is {width}x{height})."
        )


def icon_path(data_dir: str, policy: ImportPolicy) -> str | None:
    """Where this game keeps the icon under a world's data dir, if it has one."""
    if not policy.icon_relpath:
        return None
    return os.path.join(data_dir, *policy.icon_relpath.split("/"))


def adopt_legacy_icons(data_dir: str, policy: ImportPolicy) -> list[str]:
    """Move an icon left at a legacy path to the current one. Returns what moved.

    A world, or an archive of one, from before the platform was renamed keeps
    its icon under the old name. The current path wins when both exist; the
    legacy copy is then removed rather than left as a second icon.
    """
    own = icon_path(data_dir, policy)
    if own is None:
        return []
    moved: list[str] = []
    for rel in policy.legacy_icon_relpaths:
        legacy = os.path.join(data_dir, *rel.split("/"))
        if os.path.islink(legacy) or not os.path.isfile(legacy):
            continue
        if os.path.lexists(own):
            os.remove(legacy)
        else:
            os.replace(legacy, own)
            moved.append(rel)
    return moved


def parse_level_name(properties: str | None) -> str:
    """The save folder's name from server.properties text (Minecraft's default: world)."""
    for line in (properties or "").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "level-name":
            return value.strip() or "world"
    return "world"


def _level_name(data_dir: str) -> str:
    try:
        with open(os.path.join(data_dir, "server.properties"), encoding="utf-8") as f:
            return parse_level_name(f.read())
    except (OSError, UnicodeDecodeError):
        return "world"


def level_icon_relpath(level: str, policy: ImportPolicy) -> str | None:
    """Where a save named ``level`` keeps its icon, relative to the data dir.

    level-name is user-editable, so a value that would step outside the data
    dir is treated as having no save icon at all.
    """
    if not policy.level_icon_name:
        return None
    if "/" in level or "\\" in level or level in (".", ".."):
        return None
    return f"{level}/{policy.level_icon_name}"


def _level_icon_relpath(data_dir: str, policy: ImportPolicy) -> str | None:
    return level_icon_relpath(_level_name(data_dir), policy)


def _is_valid_icon_file(path: str) -> bool:
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return False
        with open(path, "rb") as f:
            validate_icon(f.read(ICON_MAX_BYTES + 1))
    except (InvalidIcon, OSError):
        return False
    return True


def _level_icon(data_dir: str, policy: ImportPolicy) -> str | None:
    """The save folder's own icon, if it has a valid one.

    Validated like an upload because the platform serves it: one the game
    would refuse is not shown here either.
    """
    rel = _level_icon_relpath(data_dir, policy)
    if rel is None:
        return None
    path = os.path.join(data_dir, *rel.split("/"))
    return path if _is_valid_icon_file(path) else None


def resolve_icon(data_dir: str, policy: ImportPolicy) -> tuple[str, bool] | None:
    """The icon to show and whether it is the world's own (True) or the save's.

    Mirrors the game: the world's own icon wins, failing that the save
    folder's.
    """
    own = icon_path(data_dir, policy)
    if own is not None and os.path.isfile(own) and not os.path.islink(own):
        return own, True
    level = _level_icon(data_dir, policy)
    return (level, False) if level else None


def write_icon(data_dir: str, policy: ImportPolicy, data: bytes) -> None:
    """Validate and atomically install ``data`` as the world's icon."""
    if not policy.icon_relpath:
        raise InvalidIcon("This game does not support a world icon.")
    validate_icon(data)
    # Atomic, and refuses a symlinked directory on the way (see world_fs).
    world_fs.write_bytes(data_dir, policy.icon_relpath, data)


@dataclass(frozen=True)
class IconFile:
    """One icon file on disk, for showing the user what a clear will delete."""

    #: "server" for the world's own icon, "save" for the save folder's.
    kind: Literal["server", "save"]
    #: Relative to the data dir, as the user would find it in a backup.
    relpath: str
    path: str
    #: Whether it passes the icon check -- i.e. whether the game would show it.
    valid: bool
    #: mtime in ms, for a cache-busting preview URL.
    version: int


def icon_files(data_dir: str, policy: ImportPolicy) -> list[IconFile]:
    """Every icon file the world has, in the order the game would pick them.

    Includes ones that fail the check: they are still files a clear deletes.
    """
    candidates: list[tuple[Literal["server", "save"], str]] = []
    if policy.icon_relpath:
        candidates.append(("server", policy.icon_relpath))
    level = _level_icon_relpath(data_dir, policy)
    if level is not None:
        candidates.append(("save", level))
    out: list[IconFile] = []
    for kind, rel in candidates:
        path = os.path.join(data_dir, *rel.split("/"))
        try:
            st = os.lstat(path)
        except OSError:
            continue
        # A directory by that name is not an icon, and not ours to delete.
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
            continue
        out.append(
            IconFile(
                kind=kind,
                relpath=rel,
                path=path,
                valid=_is_valid_icon_file(path),
                version=int(st.st_mtime * 1000),
            )
        )
    return out


def clear_icons(data_dir: str, policy: ImportPolicy) -> list[str]:
    """Delete every icon file the world has. Returns the relpaths removed.

    Both, not just ours: the game falls back to the save's icon, so removing
    only server-icon.png would leave the world with an icon the owner asked to
    be rid of.
    """
    removed: list[str] = []
    for f in icon_files(data_dir, policy):
        try:
            if not world_fs.remove(data_dir, f.relpath):
                continue
        except world_fs.UnsafeWorldPath:
            continue
        removed.append(f.relpath)
    return removed


def enforce_icon(data_dir: str, policy: ImportPolicy) -> bool:
    """Drop an imported icon that fails validation. Returns whether it was removed.

    The import allow-list only checks the path; this checks the file, so an
    archive cannot bring in an "icon" the upload route would refuse.
    """
    path = icon_path(data_dir, policy)
    if path is None or not os.path.lexists(path):
        return False
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            raise InvalidIcon("not a regular file")
        if os.path.getsize(path) > ICON_MAX_BYTES:
            raise InvalidIcon("too large")
        with open(path, "rb") as f:
            validate_icon(f.read(ICON_MAX_BYTES + 1))
    except (InvalidIcon, OSError):
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                return False  # a directory by that name is not ours to delete
            world_fs.remove(data_dir, policy.icon_relpath or "")
        except OSError:
            return False
        return True
    return False


# An imported Minecraft world can carry its icon in the save folder, in
# server-icon.png, or both. The game reads server-icon.png first, so the icon is
# kept there alone. Planned from bytes so the import preflight (reading the
# archive) and the worker (reading the disk) decide identically.


@dataclass(frozen=True)
class IconChange:
    #: "delete": a save icon the server icon makes redundant.
    #: "move": a save icon that becomes the server icon.
    action: Literal["delete", "move"]
    #: The save icon, relative to the data dir.
    path: str
    #: Where a move puts it; None for a delete.
    to: str | None = None


def _valid(data: bytes | None) -> bool:
    if data is None:
        return False
    try:
        validate_icon(data)
    except InvalidIcon:
        return False
    return True


def plan_icon_changes(
    policy: ImportPolicy, server: bytes | None, save: bytes | None, save_relpath: str | None
) -> list[IconChange]:
    """What normalising a world's icons will do, given each file's bytes (or None).

    An invalid server icon counts as absent: the import drops it before this
    runs. An invalid save icon on its own is left alone -- the game ignores it
    too, and it is not the platform's to move.
    """
    if save is None or save_relpath is None or not policy.icon_relpath:
        return []
    if _valid(server):
        return [IconChange("delete", save_relpath)]
    if _valid(save):
        return [IconChange("move", save_relpath, policy.icon_relpath)]
    return []


def _read_capped(path: str) -> bytes | None:
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            # One byte over the cap is enough to fail validation.
            return f.read(ICON_MAX_BYTES + 1)
    except OSError:
        return None


def normalize_icons(data_dir: str, policy: ImportPolicy) -> list[IconChange]:
    """Leave the world's icon in the game's own path only. Returns what it did."""
    save_rel = _level_icon_relpath(data_dir, policy)
    own = icon_path(data_dir, policy)
    if save_rel is None or own is None:
        return []
    save_path = os.path.join(data_dir, *save_rel.split("/"))
    save = _read_capped(save_path)
    changes = plan_icon_changes(policy, _read_capped(own), save, save_rel)
    for change in changes:
        if change.action == "move" and save is not None:
            write_icon(data_dir, policy, save)
        world_fs.remove(data_dir, save_rel)
    return changes
