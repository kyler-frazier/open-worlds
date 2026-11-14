"""A container create cannot ask Docker for more than a world or guard needs."""

from __future__ import annotations

import pytest
from docker.types import HostConfig

from open_worlds.common.docker_policy import UnsafeContainerSpec, check_create

ROOT = "/srv/games/worlds"
IMAGE = "itzg/minecraft-server@sha256:" + "0" * 64


def _check(image: str = IMAGE, **host: object) -> None:
    host.setdefault("network_mode", "games")
    check_create(
        image,
        HostConfig(version="1.45", **host),
        allowed_images=[IMAGE, "open-worlds-egress-guard:latest"],
        worlds_root=ROOT,
        games_network="games",
    )


def test_what_a_world_and_a_guard_ask_for_passes():
    _check(
        binds={f"{ROOT}/demo/data": {"bind": "/data", "mode": "rw"}},
        cap_drop=["ALL"],
        cap_add=["CHOWN", "SETUID", "SETGID"],
        security_opt=["no-new-privileges:true"],
    )
    _check(network_mode="container:game-world-demo--egress")
    _check("open-worlds-egress-guard:latest", cap_add=["NET_ADMIN", "NET_BIND_SERVICE"])


@pytest.mark.parametrize(
    ("image", "host"),
    [
        ("alpine:latest", {}),
        (IMAGE, {"privileged": True}),
        (IMAGE, {"pid_mode": "host"}),
        (IMAGE, {"userns_mode": "host"}),
        (IMAGE, {"network_mode": "host"}),
        (IMAGE, {"network_mode": "container:edge"}),
        (IMAGE, {"cap_add": ["SYS_ADMIN"]}),
        (IMAGE, {"cap_add": ["cap_sys_ptrace"]}),
        (IMAGE, {"security_opt": ["seccomp=unconfined"]}),
        (IMAGE, {"devices": ["/dev/sda:/dev/sda"]}),
        (IMAGE, {"binds": {"/": {"bind": "/host", "mode": "rw"}}}),
        (IMAGE, {"binds": {f"{ROOT}/../db": {"bind": "/x", "mode": "rw"}}}),
        (IMAGE, {"binds": {f"{ROOT}-other/x": {"bind": "/x", "mode": "rw"}}}),
        (IMAGE, {"binds": {"/var/run/docker.sock": {"bind": "/s", "mode": "ro"}}}),
    ],
)
def test_anything_more_is_refused(image, host):
    with pytest.raises(UnsafeContainerSpec):
        _check(image, **host)
