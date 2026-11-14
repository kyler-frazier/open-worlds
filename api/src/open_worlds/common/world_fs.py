"""File operations inside a world's data directory that the world cannot redirect.

A world container runs as the same host uid as the platform and owns every
file under its data directory, so it can swap any directory there for a
symlink into another world's. A path check followed by a plain open() races
that swap. These helpers walk the path one component at a time with
O_NOFOLLOW and act relative to the directory they opened, so a symlink
anywhere on the path is refused, and a swap after the walk still leaves the
operation inside this world.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class UnsafeWorldPath(OSError):
    """A path inside a world's data directory runs through a symlink or non-directory."""


def _parts(rel: str) -> list[str]:
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts:
        raise UnsafeWorldPath(f"{rel!r} leaves the world's data directory")
    return parts


def _split(rel: str) -> tuple[str, str]:
    parts = _parts(rel)
    if not parts:
        raise UnsafeWorldPath(f"{rel!r} names no file")
    return "/".join(parts[:-1]), parts[-1]


def _open_child(name: str, dir_fd: int, rel: str) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafeWorldPath(f"{rel!r} is not a plain directory path") from e
        raise


@contextmanager
def open_dir(root: str, rel: str = "", *, create: bool = False) -> Iterator[int]:
    """A descriptor for ``root/rel``, refusing a symlink at any step.

    ``create`` makes missing directories on the way, like os.makedirs.
    ``root`` itself and its parents are the platform's, out of a world's reach.
    """
    if create:
        os.makedirs(root, exist_ok=True)
    fd = os.open(root, _DIR_FLAGS)
    try:
        for name in _parts(rel):
            try:
                child = _open_child(name, fd, rel)
            except FileNotFoundError:
                if not create:
                    raise
                with suppress(FileExistsError):
                    os.mkdir(name, dir_fd=fd)
                child = _open_child(name, fd, rel)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _write_into(dir_fd: int, name: str, fill: bytes | str, mode: int) -> None:
    # Written beside the target and renamed over it, so a running server or a
    # concurrent backup never reads half a file. The rename replaces a symlink
    # by that name rather than following it.
    tmp = f".{name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            if isinstance(fill, bytes):
                f.write(fill)
            else:
                with open(fill, "rb") as src:
                    shutil.copyfileobj(src, f)
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise


def write_bytes(root: str, rel: str, data: bytes, *, mode: int = 0o644) -> None:
    """Atomically replace ``root/rel`` with ``data``, creating its directories."""
    parent, name = _split(rel)
    with open_dir(root, parent, create=True) as dfd:
        _write_into(dfd, name, data, mode)


def write_text(root: str, rel: str, text: str, *, mode: int = 0o644) -> None:
    write_bytes(root, rel, text.encode("utf-8"), mode=mode)


def copy_in(src: str, root: str, rel: str, *, mode: int = 0o644) -> None:
    """Atomically copy a platform-owned file to ``root/rel``."""
    parent, name = _split(rel)
    with open_dir(root, parent, create=True) as dfd:
        _write_into(dfd, name, src, mode)


def read_bytes(root: str, rel: str, max_bytes: int | None = None) -> bytes | None:
    """``root/rel``'s contents, or None when it does not exist.

    A symlink or non-regular file there raises UnsafeWorldPath.
    """
    parent, name = _split(rel)
    try:
        with open_dir(root, parent) as dfd:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
            except OSError as e:
                if e.errno == errno.ELOOP:
                    raise UnsafeWorldPath(f"{rel!r} is a symlink") from e
                raise
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise UnsafeWorldPath(f"{rel!r} is not a regular file")
        return f.read() if max_bytes is None else f.read(max_bytes)


def read_text(root: str, rel: str) -> str | None:
    data = read_bytes(root, rel)
    return None if data is None else data.decode("utf-8", errors="replace")


def is_file(root: str, rel: str) -> bool:
    """Whether ``root/rel`` is a regular file reached without a symlink."""
    parent, name = _split(rel)
    try:
        with open_dir(root, parent) as dfd:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except (FileNotFoundError, UnsafeWorldPath):
        return False
    return stat.S_ISREG(st.st_mode)


def remove(root: str, rel: str) -> bool:
    """Delete the file (or symlink) ``root/rel``. Returns whether it existed."""
    parent, name = _split(rel)
    try:
        with open_dir(root, parent) as dfd:
            os.unlink(name, dir_fd=dfd)
    except FileNotFoundError:
        return False
    return True
