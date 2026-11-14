#!/usr/bin/env python3
"""Shift the numeric owner of every file in a tree by the userns-remap offset.

With userns-remap on, uid N inside a container is uid base+N on the host, so
data written before remap was enabled (or restored from a host without it)
has to move by the same offset before containers can use it. Run as root:

    sudo scripts/shift-owner.py --to-remapped /path/to/tree
    sudo scripts/shift-owner.py --to-host /path/to/tree      # rollback

The base comes from the dockremap line in /etc/subuid unless --base is given.
Refuses to run if any owner is already on the far side of the shift, so it
cannot double-shift a tree, and leaves mode bits exactly as they were (chown
clears setuid/setgid).
"""

from __future__ import annotations

import argparse
import os
import stat
import sys


def dockremap_base(path: str = "/etc/subuid") -> int:
    with open(path) as f:
        for line in f:
            name, start, _count = line.strip().split(":")
            if name == "dockremap":
                return int(start)
    sys.exit("no dockremap entry in /etc/subuid; is userns-remap enabled?")


def walk(root: str):
    yield root
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            yield os.path.join(dirpath, name)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    direction = p.add_mutually_exclusive_group(required=True)
    direction.add_argument("--to-remapped", action="store_true")
    direction.add_argument("--to-host", action="store_true")
    p.add_argument("--base", type=int, help="override the dockremap base")
    p.add_argument("trees", nargs="+")
    args = p.parse_args()

    base = args.base if args.base is not None else dockremap_base()
    size = 65536
    offset = base if args.to_remapped else -base

    def ok(i: int) -> bool:
        return i < size if args.to_remapped else base <= i < base + size

    paths = [path for tree in args.trees for path in walk(tree)]
    stats = {path: os.lstat(path) for path in paths}
    bad = [p for p, st in stats.items() if not (ok(st.st_uid) and ok(st.st_gid))]
    if bad:
        sys.exit(f"{len(bad)} paths already outside the expected range, e.g. {bad[0]}; not shifting")

    for path, st in stats.items():
        os.lchown(path, st.st_uid + offset, st.st_gid + offset)
        if not stat.S_ISLNK(st.st_mode):
            os.chmod(path, stat.S_IMODE(st.st_mode))
    print(f"shifted {len(paths)} paths by {offset:+d}")


if __name__ == "__main__":
    main()
