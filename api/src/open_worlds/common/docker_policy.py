"""What a container create may ask Docker for.

The socket proxy refuses the same things (deploy/socketproxy/haproxy.cfg.template),
but it can only pattern-match the request body. Checking the host config here,
before it is sent, turns a bug in the orchestrator into a clear error naming the
setting instead of an anonymous 403, and keeps the two lists side by side for
review.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

# What worlds and guards are given; nothing else may be added.
ALLOWED_CAPS = frozenset(
    {
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
        "FSETID",
        "SETGID",
        "SETUID",
        "KILL",
        "NET_ADMIN",
        "NET_BIND_SERVICE",
    }
)

# Host-config keys that must be unset: each one reaches past the container.
_MUST_BE_EMPTY = (
    "PidMode",
    "IpcMode",
    "UTSMode",
    "UsernsMode",
    "CgroupnsMode",
    "CgroupParent",
    "Runtime",
    "Mounts",
    "Devices",
    "DeviceRequests",
    "DeviceCgroupRules",
    "VolumesFrom",
    "MaskedPaths",
    "ReadonlyPaths",
)

_GUARD_NETNS = re.compile(r"container:game-world-[a-z0-9-]+")
_UNSAFE_SECURITY_OPT = re.compile(r"unconfined|seccomp[:=]|apparmor[:=]|label[:=]disable", re.I)


class UnsafeContainerSpec(RuntimeError):
    """The orchestrator built a create the socket proxy would refuse."""


def check_create(
    image: str,
    host_config: Mapping[str, Any],
    *,
    allowed_images: Iterable[str],
    worlds_root: str,
    games_network: str,
) -> None:
    """Raise UnsafeContainerSpec unless the create stays inside the policy."""
    if image not in set(allowed_images):
        raise UnsafeContainerSpec(f"image {image!r} is not a game image or the egress guard")
    if host_config.get("Privileged"):
        raise UnsafeContainerSpec("privileged containers are not allowed")
    for key in _MUST_BE_EMPTY:
        if host_config.get(key):
            raise UnsafeContainerSpec(f"{key} must not be set")
    for opt in host_config.get("SecurityOpt") or ():
        if _UNSAFE_SECURITY_OPT.search(opt):
            raise UnsafeContainerSpec(f"security option {opt!r} is not allowed")

    network = host_config.get("NetworkMode") or ""
    if network != games_network and not _GUARD_NETNS.fullmatch(network):
        raise UnsafeContainerSpec(f"network {network!r} is not the games network or a guard's")

    for cap in host_config.get("CapAdd") or ():
        name = cap.upper().removeprefix("CAP_")
        if name not in ALLOWED_CAPS:
            raise UnsafeContainerSpec(f"capability {cap!r} is not allowed")

    root = worlds_root.rstrip("/") + "/"
    for bind in host_config.get("Binds") or ():
        source = bind.split(":", 1)[0]
        if ".." in source.split("/") or not os.path.normpath(source).startswith(root):
            raise UnsafeContainerSpec(f"bind {source!r} is outside {root}")
