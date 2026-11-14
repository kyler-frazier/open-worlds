"""The low-level create must build exactly the container the high-level one did.

Containers are created through ``client.api.create_container`` because only it
accepts ``stop_timeout``. A setting routed to the wrong half of the config is
not an error, just a container missing a hardening flag, so this asks Docker
whether the two paths agree.

Needs a Docker daemon; skipped without one.
"""

from __future__ import annotations

import pytest

from open_worlds.common.games import adapter_for
from open_worlds.common.orchestrator import INSTALL, PLAY, DockerOrchestrator, WorldSpec

docker = pytest.importorskip("docker")

# Differences that are not drift: the id Docker assigns as the hostname, and the
# setting the whole migration exists to add.
EXPECTED_DIFFERENCES = {"Config.Hostname", "Config.StopTimeout"}

PROBE_IMAGE = "alpine:latest"


@pytest.fixture(scope="module")
def client():
    try:
        c = docker.from_env()
        c.ping()
    except Exception as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"no Docker daemon available: {exc}")
    try:
        c.images.get(PROBE_IMAGE)
    except docker.errors.ImageNotFound:  # pragma: no cover
        pytest.skip(f"{PROBE_IMAGE} not present; skipping rather than pulling")
    return c


def _spec() -> WorldSpec:
    return WorldSpec(
        game="PALWORLD",
        world_id="equiv-world",
        owner_id="equiv-owner",
        subdomain="equiv",
        display_name="Equivalence",
        admin_password="pw",
        memory_mb=2048,
        max_players=8,
        game_port=8299,
    )


def _differences(a, b) -> set[str]:
    a.reload()
    b.reload()
    out = set()
    for section in ("Config", "HostConfig"):
        left, right = a.attrs[section], b.attrs[section]
        for key in set(left) | set(right):
            if left.get(key) != right.get(key):
                out.add(f"{section}.{key}")
    return out


@pytest.mark.parametrize("stage", [INSTALL, PLAY])
def test_low_level_create_matches_the_high_level_one(client, stage):
    orch = DockerOrchestrator(client=client)
    spec = _spec()
    adapter = adapter_for(spec.game)

    kwargs = orch._create_kwargs(adapter, spec, stage)
    # A stand-in image: the real one is multi-gigabyte and nothing is started.
    kwargs["image"] = PROBE_IMAGE
    kwargs["environment"] = {"PROBE": "1"}
    if stage == INSTALL:
        # What an install hides from itself; Palworld has nothing to hide.
        kwargs["tmpfs"] = {"/palworld/Pal/Binaries/Win64/Mods": "size=1m"}

    # The one key the high-level client will not take -- it raises rather than
    # passing it through, which is the entire reason for the low-level path.
    high_kwargs = {k: v for k, v in kwargs.items() if k != "stop_timeout"}
    with pytest.raises(TypeError, match="stop_timeout"):
        client.containers.create(**{**kwargs, "name": "equivalence-rejected"})

    high = low = None
    try:
        high = client.containers.create(**{**high_kwargs, "name": "equivalence-high"})

        orch._create_kwargs = lambda *_: {  # type: ignore[method-assign]
            **kwargs,
            "name": "equivalence-low",
        }
        # The stand-in image is not one the create policy allows.
        orch._check_create = lambda *_: None  # type: ignore[method-assign]
        low = orch._create_container(adapter, spec, stage)

        drifted = _differences(high, low) - EXPECTED_DIFFERENCES
        # The spec label is stamped by the low-level path only.
        drifted.discard("Config.Labels")
        assert not drifted, f"the two create paths disagree on: {sorted(drifted)}"

        assert low.attrs["Config"]["StopTimeout"] == adapter.stop_timeout_seconds
    finally:
        for c in (high, low):
            if c is not None:
                c.remove(force=True)


def test_an_unrouted_setting_raises_instead_of_vanishing(client):
    """The guarantee the high-level client gave us, kept by hand.

    A new container setting that nobody routes to a half would otherwise look
    set in the template and be absent on the container -- which for a hardening
    flag is exactly the failure that is impossible to notice.
    """
    orch = DockerOrchestrator(client=client)
    spec = _spec()
    adapter = adapter_for(spec.game)
    base = orch._create_kwargs(adapter, spec, PLAY)
    orch._create_kwargs = lambda *_: {**base, "some_new_docker_setting": True}  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="not routed"):
        orch._create_container(adapter, spec, PLAY)
