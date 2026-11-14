from dataclasses import replace
from unittest.mock import MagicMock

import docker.errors
import pytest

from open_worlds.common.games import adapter_for, game_ids
from open_worlds.common.games.minecraft.settings import ServerType
from open_worlds.common.orchestrator import (
    INSTALL,
    PLAY,
    ContainerNameConflict,
    DockerOrchestrator,
    EgressGuardUnavailable,
    RunState,
    WorldImageUnavailable,
    WorldSpec,
)


def _spec(game="PALWORLD", **kw):
    """A world spec carrying the union of both games' fields."""
    base = dict(
        game=game,
        world_id="w1",
        owner_id="o1",
        subdomain="demo",
        display_name="Demo World",
        memory_mb=8192,
        max_players=16,
        admin_password="secret",
        game_port=8213 if game == "PALWORLD" else None,
        settings=(
            {"version": "1.21.4", "server_type": ServerType.FABRIC.value}
            if game == "MINECRAFT"
            else {}
        ),
    )
    base.update(kw)
    return WorldSpec(**base)


SPEC = _spec()


@pytest.fixture(autouse=True)
def _installed(request, monkeypatch):
    """Worlds here are installed already, so a start runs the server, as it did
    before install stages; the ``staged`` tests keep the real checks."""
    if "staged" in request.fixturenames:
        return
    for game in game_ids():
        cls = type(adapter_for(game))
        monkeypatch.setattr(cls, "install_needed", lambda self, spec, data_dir: False)
        monkeypatch.setattr(
            cls, "run_environment", lambda self, spec, data_dir: self.environment(spec)
        )


# Label that marks a container as one we created and may manage.
MANAGED = {"open-worlds": "true"}


class FakeContainer:
    def __init__(
        self, status="created", labels=None, cid="cid-1", name="game-world-demo", health=None
    ):
        self.status = status
        self.labels = labels or {}
        self.id = cid
        self.name = name
        self.attrs = {"State": {}}
        if health is not None:
            self.attrs["State"]["Health"] = {"Status": health}

    def reload(self):
        pass

    def reload_status(self, status):
        self.status = status


class FakeDockerApi:
    """The low-level client the orchestrator mutates containers through.

    The socket proxy allows a mutation only when the path names a world
    container, so these record what was addressed.
    """

    def __init__(self, containers):
        self._containers = containers
        self.started: list[str] = []
        self.stopped: list[tuple[str, int | None]] = []
        self.removed: list[tuple[str, bool]] = []
        self.created: list[dict] = []
        self._host_config: dict = {}
        self._networks: list[str] = []

    def _find(self, ref):
        return self._containers.get(ref)

    def start(self, ref):
        self.started.append(ref)
        c = self._find(ref)
        if c is not None:
            c.status = "running"

    def stop(self, ref, timeout=None):
        self.stopped.append((ref, timeout))
        c = self._find(ref)
        if c is not None:
            c.status = "exited"

    def remove_container(self, ref, force=False):
        self.removed.append((ref, force))
        # A removed container stops resolving, which is what lets a caller
        # re-create one under the same name.
        self._containers.pop(ref, None)

    # --- creation ---------------------------------------------------------
    # Containers are built low-level so they can carry StopTimeout, which the
    # high-level create refuses. That means the definition arrives split across
    # three calls, so the fake reassembles it: tests care about the container
    # that results, not about which half Docker wants each field in.

    def create_host_config(self, **kwargs):
        self._host_config = kwargs
        # Docker's own shape, which the create policy reads, plus the kwargs
        # as passed, for tests that assert on them.
        from docker.types import HostConfig

        host = HostConfig(version="1.45", **kwargs)
        host["_host_config"] = kwargs
        return host

    def create_endpoint_config(self, **kwargs):
        return {}

    def create_networking_config(self, endpoints):
        self._networks = list(endpoints)
        return {"_networking": endpoints}

    def create_container(self, **kwargs):
        self.created.append(kwargs)
        container = FakeContainer(
            status="created",
            labels=kwargs.get("labels", {}),
            cid=f"cid-{len(self.created)}",
            name=kwargs.get("name", "game-world-demo"),
        )
        self._containers[container.name] = container
        self._containers[container.id] = container
        return {"Id": container.id}

    @property
    def created_spec(self) -> dict:
        """The last create, reassembled into one dict.

        Merges the host config and the network back in so a test can assert on
        ``mem_limit`` or ``network`` without caring that Docker keeps them in a
        different object from ``image`` and ``labels``.
        """
        merged = dict(self.created[-1])
        merged.pop("host_config", None)
        merged.pop("networking_config", None)
        merged.update(self._host_config)
        merged["network"] = self._networks[0]
        # Present the bind map under the name the template uses. Docker wants
        # the mount points on the container config and the binds on the host
        # config; the template says it once, as ``volumes``.
        merged["volumes"] = merged.pop("binds")
        return merged


@pytest.fixture
def fake_client():
    client = MagicMock()
    # Containers the fake api can resolve, keyed by the name the orchestrator
    # addresses them with.
    client._by_name = {}
    client.api = FakeDockerApi(client._by_name)
    return client


def _register(client, container):
    """Make a container resolvable by name through the fake low-level api."""
    client._by_name[container.name] = container
    return container


def _resolve_by_name(client):
    """A containers.get that tracks removals, as the real one does."""

    def _get(ref):
        found = client._by_name.get(ref)
        if found is None:
            raise docker.errors.NotFound(f"no such container: {ref}")
        return found

    return _get


def test_start_creates_and_starts_when_absent(fake_client):
    fake_client.containers.get.side_effect = _resolve_by_name(fake_client)

    orch = DockerOrchestrator(client=fake_client)
    result = orch.start(SPEC)

    kwargs = fake_client.api.created_spec
    # Identity + port labels are applied.
    assert kwargs["labels"]["open-worlds.game_port"] == "8213"
    assert kwargs["labels"]["open-worlds.subdomain"] == "demo"
    assert kwargs["name"] == "game-world-demo"
    # No host port is published: pal-waker relays to the container instead, so a
    # world container can never ask Docker to bind a port of its choosing.
    assert "ports" not in kwargs
    # The server always listens on the stock port inside its container.
    assert kwargs["environment"]["PORT"] == "8211"
    # World rules live in the world's own settings file, which the platform
    # renders before start, so the image must not regenerate that file from the
    # environment and overwrite them.
    assert kwargs["environment"]["DISABLE_GENERATE_SETTINGS"] == "true"
    # The world's figure is the container's memory limit, with swap disabled so
    # an overgrown world is OOM-killed inside its cgroup rather than swapping
    # the whole host. CPU is uncapped unless the operator configures it.
    assert kwargs["mem_limit"] == "8192m"
    assert kwargs["memswap_limit"] == "8192m"
    assert "nano_cpus" not in kwargs
    assert result.run_state is RunState.RUNNING


def test_start_caps_cpu_when_configured(fake_client, monkeypatch):
    from open_worlds.common.config import get_settings

    settings = get_settings().model_copy(update={"container_cpus": 2.5})
    fake_client.containers.get.side_effect = _resolve_by_name(fake_client)

    DockerOrchestrator(settings=settings, client=fake_client).start(SPEC)

    kwargs = fake_client.api.created_spec
    assert kwargs["nano_cpus"] == 2_500_000_000


def test_start_pulls_image_when_missing(fake_client):
    fake_client.containers.get.side_effect = _resolve_by_name(fake_client)
    fake_client.images.get.side_effect = docker.errors.ImageNotFound("no image")

    orch = DockerOrchestrator(client=fake_client)
    orch.start(SPEC)

    # A missing world image is pulled before create, so start self-heals.
    assert fake_client.images.pull.called
    assert fake_client.api.created


def test_start_does_not_pull_when_image_present(fake_client):
    fake_client.containers.get.side_effect = _resolve_by_name(fake_client)
    fake_client.images.get.return_value = object()  # present -> no pull

    orch = DockerOrchestrator(client=fake_client)
    orch.start(SPEC)

    assert not fake_client.images.pull.called


def test_inspect_absent_returns_absent(fake_client):
    fake_client.containers.get.side_effect = _resolve_by_name(fake_client)
    orch = DockerOrchestrator(client=fake_client)
    assert orch.inspect("demo").run_state is RunState.ABSENT


def test_inspect_exited_maps_to_exited(fake_client):
    fake_client.containers.get.return_value = FakeContainer(status="exited", labels=MANAGED)
    orch = DockerOrchestrator(client=fake_client)
    assert orch.inspect("demo").run_state is RunState.EXITED


def test_stop_removes_container(fake_client):
    container = _register(fake_client, FakeContainer(status="running", labels=MANAGED))
    fake_client.containers.get.return_value = container
    orch = DockerOrchestrator(client=fake_client)
    orch.stop("demo")
    # Addressed by name, not id: that is what the socket proxy's rule keys on.
    assert fake_client.api.removed == [("game-world-demo", True)]


def test_stop_starting_container_killed_promptly(fake_client):
    # A world stopped mid-boot has nothing to save and can't handle SIGTERM;
    # it must be killed immediately (timeout 0) rather than blocking the caller.
    container = _register(
        fake_client, FakeContainer(status="running", labels=MANAGED, health="starting")
    )
    fake_client.containers.get.return_value = container
    orch = DockerOrchestrator(client=fake_client)
    orch.stop("demo")
    assert fake_client.api.stopped == [("game-world-demo", 0)]
    assert fake_client.api.removed == [("game-world-demo", True)]


def test_stop_healthy_container_uses_grace_window(fake_client):
    container = _register(
        fake_client, FakeContainer(status="running", labels=MANAGED, health="healthy")
    )
    fake_client.containers.get.return_value = container
    orch = DockerOrchestrator(client=fake_client)
    orch.stop("demo")
    assert fake_client.api.stopped == [("game-world-demo", 60)]


def test_a_non_graceful_stop_skips_the_shutdown_window(fake_client):
    """For a server that never became ready: nothing to save, and a failed
    startup may never answer SIGTERM, so waiting just stalls the stop."""
    container = _register(
        fake_client, FakeContainer(status="running", labels=MANAGED, health="unhealthy")
    )
    fake_client.containers.get.return_value = container
    orch = DockerOrchestrator(client=fake_client)
    orch.stop("demo", graceful=False)
    orch.sleep("demo", graceful=False)
    assert fake_client.api.stopped == [("game-world-demo", 0), ("game-world-demo", 0)]


def test_logs_read_only_the_current_run_of_the_world_container(fake_client):
    """A restarted container keeps its old log. Reading it whole stopped a
    fixed world again for the previous boot's failure."""
    container = _register(fake_client, FakeContainer(status="exited", labels=MANAGED))
    container.attrs["State"]["StartedAt"] = "2026-10-05T10:41:16.741863658Z"
    fake_client.containers.get.return_value = container
    fake_client.api.logs = MagicMock(return_value=b"line one\nFailed to load datapacks")
    assert DockerOrchestrator(client=fake_client).logs("demo").endswith("Failed to load datapacks")
    call = fake_client.api.logs.call_args
    assert call.args[0] == "game-world-demo"
    assert call.kwargs["since"] == 1791196876  # 2026-10-05T10:41:16Z


def test_stop_ignores_unmanaged_container(fake_client):
    # A DNS-valid subdomain can collide with a platform/compose container name.
    # Such a container lacks our label and must never be stopped or removed.
    foreign = _register(
        fake_client,
        FakeContainer(status="running", labels={}, name="open-worlds-web-1"),
    )
    fake_client.containers.get.return_value = foreign
    orch = DockerOrchestrator(client=fake_client)
    orch.stop("platform-deploy-web-1")
    assert fake_client.api.removed == []
    assert fake_client.api.stopped == []


def test_inspect_unmanaged_returns_absent(fake_client):
    foreign = FakeContainer(status="running", labels={}, name="open-worlds-web-1")
    fake_client.containers.get.return_value = foreign
    orch = DockerOrchestrator(client=fake_client)
    assert orch.inspect("platform-deploy-web-1").run_state is RunState.ABSENT


def test_start_refuses_to_clobber_unmanaged_name(fake_client):
    foreign = FakeContainer(status="running", labels={}, name="open-worlds-web-1")
    fake_client.containers.get.return_value = foreign
    orch = DockerOrchestrator(client=fake_client)
    with pytest.raises(ContainerNameConflict):
        orch.start(SPEC)
    assert not fake_client.api.created


def test_list_managed_filters_by_label(fake_client):
    c = FakeContainer(
        status="running",
        labels={
            "open-worlds": "true",
            "open-worlds.subdomain": "demo",
            "open-worlds.world_id": "w1",
        },
    )
    fake_client.containers.list.return_value = [c]
    orch = DockerOrchestrator(client=fake_client)
    managed = orch.list_managed()
    assert len(managed) == 1
    assert managed[0].subdomain == "demo"
    _, kwargs = fake_client.containers.list.call_args
    assert kwargs["filters"] == {"label": "open-worlds=true"}


# --- the game seam ---------------------------------------------------------
# The orchestrator must know nothing about either game. These pin down that the
# container it builds is entirely the adapter's answer.


@pytest.mark.parametrize(
    "game,expect_image,expect_mount",
    [
        ("MINECRAFT", "itzg/minecraft-server", "/data"),
        ("PALWORLD", "thijsvanloef/palworld-server-docker", "/palworld"),
    ],
)
def test_container_is_built_from_the_games_adapter(fake_client, game, expect_image, expect_mount):
    fake_client.containers.get.side_effect = _resolve_by_name(fake_client)

    DockerOrchestrator(client=fake_client).start(_spec(game))

    kwargs = fake_client.api.created_spec
    assert expect_image in kwargs["image"]
    assert expect_mount in kwargs["volumes"][next(iter(kwargs["volumes"]))]["bind"]
    # The game is recorded on the container so a reconcile can identify it
    # without consulting the database.
    assert kwargs["labels"]["open-worlds.game"] == game


def test_no_game_publishes_a_host_port(fake_client):
    """Neither router needs it: both reach containers over the private network.

    This is the property that keeps a compromised control plane from asking
    Docker to bind a port of its choosing on the host.
    """
    for game in ("MINECRAFT", "PALWORLD"):
        fake_client.containers.get.side_effect = _resolve_by_name(fake_client)
        DockerOrchestrator(client=fake_client).start(_spec(game))
        kwargs = fake_client.api.created_spec
        assert "ports" not in kwargs


def test_minecraft_advertises_itself_to_mc_router(fake_client):
    fake_client.containers.get.side_effect = _resolve_by_name(fake_client)

    DockerOrchestrator(client=fake_client).start(_spec("MINECRAFT"))

    kwargs = fake_client.api.created_spec
    # mc-router routes by the hostname in the handshake, learned from this label.
    assert kwargs["labels"]["mc-router.host"].startswith("demo.")
    assert kwargs["labels"]["mc-router.port"] == "25565"
    assert kwargs["environment"]["TYPE"] == "FABRIC"
    assert kwargs["environment"]["VERSION"] == "1.21.4"
    # Minecraft worlds share one port, so none is allocated to them.
    assert "open-worlds.game_port" not in kwargs["labels"]


def test_palworld_records_the_port_the_waker_relays(fake_client):
    fake_client.containers.get.side_effect = _resolve_by_name(fake_client)

    DockerOrchestrator(client=fake_client).start(_spec("PALWORLD"))

    kwargs = fake_client.api.created_spec
    assert kwargs["labels"]["open-worlds.game_port"] == "8213"
    # The server always listens on the stock port inside its container; the
    # per-world port exists only at the host/waker boundary.
    assert kwargs["environment"]["PORT"] == "8211"
    # No hostname to route on, so nothing is advertised to mc-router.
    assert "mc-router.host" not in kwargs["labels"]


def test_delete_force_removes_without_a_graceful_wait(fake_client):
    """A delete removes the data next, so waiting for the server to save it is
    pure latency -- and for Palworld, five minutes of it at the proxy."""
    container = _register(fake_client, FakeContainer(status="running", labels=MANAGED))
    fake_client.containers.get.return_value = container
    DockerOrchestrator(client=fake_client).delete("demo")
    assert fake_client.api.stopped == []
    assert fake_client.api.removed == [("game-world-demo", True)]


def test_a_container_built_from_the_current_template_is_adopted(fake_client):
    """The common case must not churn.

    If the fingerprint were unstable -- anything non-deterministic in the
    template -- every start would delete and rebuild a perfectly good
    container, so this pins that an unchanged spec is left alone.
    """
    orch = DockerOrchestrator(client=fake_client)
    adapter = adapter_for(SPEC.game)
    current = orch._spec_fingerprint(adapter, SPEC, PLAY)
    container = _register(
        fake_client,
        FakeContainer(
            status="running",
            labels={**MANAGED, "open-worlds.spec": current},
        ),
    )
    fake_client.containers.get.return_value = container

    orch.start(SPEC)

    assert fake_client.api.removed == []
    assert not fake_client.api.created


def test_a_container_from_an_older_template_is_replaced(fake_client):
    """The week-old TTY container, in miniature."""
    orch = DockerOrchestrator(client=fake_client)
    _register(
        fake_client,
        FakeContainer(
            status="running",
            labels={**MANAGED, "open-worlds.spec": "0000000000000000"},
        ),
    )
    fake_client.containers.get.side_effect = _resolve_by_name(fake_client)
    # Registered when create is called, not before: otherwise the replacement is
    # already resolvable and start() reads it as a name collision.

    orch.start(SPEC)

    assert fake_client.api.removed == [("game-world-demo", True)]
    assert fake_client.api.created


def test_a_container_predating_the_stamp_is_replaced(fake_client):
    """No stamp means it was built before we started checking, which is drift."""
    orch = DockerOrchestrator(client=fake_client)
    _register(fake_client, FakeContainer(status="running", labels=dict(MANAGED)))
    fake_client.containers.get.side_effect = _resolve_by_name(fake_client)
    # Registered when create is called, not before: otherwise the replacement is
    # already resolvable and start() reads it as a name collision.

    orch.start(SPEC)

    assert fake_client.api.removed == [("game-world-demo", True)]


def test_the_fingerprint_moves_when_the_template_does(fake_client):
    """Changing a resource limit must change the digest, or drift goes unseen."""
    from open_worlds.common.config import get_settings

    orch = DockerOrchestrator(client=fake_client)
    adapter = adapter_for(SPEC.game)
    base = orch._spec_fingerprint(adapter, SPEC, PLAY)

    capped = DockerOrchestrator(
        settings=get_settings().model_copy(update={"container_cpus": 2.5}),
        client=fake_client,
    )
    assert capped._spec_fingerprint(adapter, SPEC, PLAY) != base

    bigger = orch._spec_fingerprint(adapter, replace(SPEC, memory_mb=SPEC.memory_mb + 1024), PLAY)
    assert bigger != base


def test_world_containers_get_a_tty_with_stdin_closed(fake_client):
    """The TTY is safe again only because the Palworld image's SIGTERM handler
    no longer follows stdin (fixed in v2.8.0). Stdin stays closed: an open,
    unattached stdin was the other half of the five-minute sleep, and nothing
    here attaches to it.
    """
    fake_client.containers.get.side_effect = _resolve_by_name(fake_client)

    DockerOrchestrator(client=fake_client).start(SPEC)

    kwargs = fake_client.api.created_spec
    assert kwargs["tty"] is True
    assert kwargs["stdin_open"] is False


class TestWorldResolver:
    """Pointing world containers at the dns-observer.

    Off by default: this puts a component in the path of every lookup a world
    makes, so it is opt-in and always carries a fallback. A resolver that stops
    answering stops the game server.
    """

    def _orch(self, monkeypatch, **env):
        from open_worlds.common import config
        from open_worlds.common.orchestrator import DockerOrchestrator

        for k, v in env.items():
            monkeypatch.setenv(f"PLATFORM_{k.upper()}", v)
        config.get_settings.cache_clear()
        return DockerOrchestrator(client=object())

    def test_no_dns_kwarg_when_unset(self, monkeypatch):
        """The default must not touch a world's resolver at all."""
        assert self._orch(monkeypatch)._dns() == {}

    def test_fallback_is_appended(self, monkeypatch):
        orch = self._orch(monkeypatch, world_dns="172.19.255.53", world_dns_fallback="1.1.1.1")
        # Order matters: the observer is tried first, the fallback only after it
        # fails to answer, so a dead observer is slow rather than fatal.
        assert orch._dns() == {"dns": ["172.19.255.53", "1.1.1.1"]}

    def test_fallback_not_duplicated(self, monkeypatch):
        orch = self._orch(monkeypatch, world_dns="1.1.1.1", world_dns_fallback="1.1.1.1")
        assert orch._dns() == {"dns": ["1.1.1.1"]}

    def test_dns_is_routed_to_the_host_config(self):
        """The create path raises on any kwarg it cannot place; dns must be placed.

        Without this the setting would look configured and blow up only when a
        world was next started.
        """
        from open_worlds.common.orchestrator import _CONTAINER_CONFIG_KEYS, _HOST_CONFIG_KEYS

        assert "dns" in _HOST_CONFIG_KEYS
        assert "dns" not in _CONTAINER_CONFIG_KEYS


# --- egress guard ------------------------------------------------------------


@pytest.fixture
def guarded(monkeypatch, fake_client):
    """Switch the guard on for SPEC's world, with a fake Docker that behaves like
    the real one where the guard depends on it: guards report healthy once
    started, and a world records the guard id it joined, as Docker does."""
    from open_worlds.common.config import get_settings

    monkeypatch.setenv("PLATFORM_EGRESS_GUARD_ENABLED", "true")
    get_settings.cache_clear()
    api = fake_client.api
    real_create, real_start = api.create_container, api.start

    def create(**kwargs):
        result = real_create(**kwargs)
        made = api._containers[result["Id"]]
        mode = api._host_config.get("network_mode") or ""
        if mode.startswith("container:"):
            # Docker resolves the name at create time and stores the id.
            mode = f"container:{api._containers[mode.split(':', 1)[1]].id}"
        made.attrs["HostConfig"] = {"NetworkMode": mode}
        made._endpoints = kwargs.get("networking_config")
        return result

    def start(ref):
        real_start(ref)
        c = api._containers.get(ref)
        if c is not None and ref.endswith("--egress"):
            c.attrs["State"]["Health"] = {"Status": getattr(api, "guard_health", "healthy")}

    api.create_container, api.start = create, start
    fake_client.containers.get.side_effect = _resolve_by_name(fake_client)
    fake_client.images.get.return_value.id = "sha256:guard-v1"
    yield fake_client
    get_settings.cache_clear()


def _guard_name():
    return "game-world-demo--egress"


def test_guarded_world_joins_its_guard_and_carries_no_network_of_its_own(guarded):
    DockerOrchestrator(client=guarded).start(SPEC)

    api = guarded.api
    guard = guarded._by_name[_guard_name()]
    # The guard is created and started first; a world cannot join a namespace
    # that does not exist yet.
    assert api.started[:2] == [_guard_name(), "game-world-demo"]
    world = guarded._by_name["game-world-demo"]
    assert world.attrs["HostConfig"]["NetworkMode"] == f"container:{guard.id}"
    assert api.created[-1]["host_config"]["_host_config"]["network_mode"] == (
        f"container:{_guard_name()}"
    )
    # Docker refuses an endpoint or resolver settings on a container: network,
    # and the world must not have a way around the guard's resolver anyway.
    assert "networking_config" not in api.created[-1]
    assert "dns" not in api._host_config
    assert guard.labels["open-worlds.guard"] == "true"
    # Never the world label: reconcile would otherwise treat it as an orphan world.
    assert "open-worlds" not in guard.labels


def test_guard_holds_the_world_name_and_only_the_capabilities_it_needs(guarded):
    DockerOrchestrator(client=guarded).start(SPEC)

    guard_create = next(c for c in guarded.api.created if c["name"] == _guard_name())
    host = guard_create["host_config"]["_host_config"]
    assert host["cap_drop"] == ["ALL"]
    assert sorted(host["cap_add"]) == ["NET_ADMIN", "NET_BIND_SERVICE"]
    # Without this the world could bind the guard's source ports and reach the
    # upstream resolvers directly.
    assert host["sysctls"] == {"net.ipv4.ip_unprivileged_port_start": "1024"}
    assert host["dns"] == ["127.0.0.1"]
    assert host["security_opt"] == ["no-new-privileges:true"]
    endpoints = guard_create["networking_config"]["_networking"]
    assert list(endpoints) == ["games"]


def test_world_is_not_started_behind_a_guard_that_never_became_ready(guarded):
    from open_worlds.common.config import get_settings

    guarded.api.guard_health = "unhealthy"
    settings = get_settings().model_copy(update={"egress_guard_ready_seconds": 0.1})

    with pytest.raises(EgressGuardUnavailable):
        DockerOrchestrator(settings=settings, client=guarded).start(SPEC)
    assert "game-world-demo" not in guarded._by_name


def test_missing_guard_image_refuses_the_start_with_a_build_hint(guarded):
    guarded.images.get.side_effect = docker.errors.ImageNotFound("nope")

    with pytest.raises(EgressGuardUnavailable, match="profile guard build"):
        DockerOrchestrator(client=guarded).start(SPEC)
    # Treated like a bad image pin everywhere: a platform problem, world untouched.
    assert issubclass(EgressGuardUnavailable, WorldImageUnavailable)


def test_rebuilt_guard_image_replaces_the_guard_and_the_world_that_joined_it(guarded):
    orch = DockerOrchestrator(client=guarded)
    orch.start(SPEC)
    old_guard = guarded._by_name[_guard_name()].id
    orch.sleep(SPEC.subdomain)

    # The approved-name list ships inside the image, so a new image id is a
    # policy change even under the same tag.
    guarded.images.get.return_value.id = "sha256:guard-v2"
    orch.start(SPEC)

    new_guard = guarded._by_name[_guard_name()]
    assert new_guard.id != old_guard
    # Docker pins container: mode to the guard's id, so the old world container
    # could never start again; it has to be rebuilt against the new guard.
    removed = [ref for ref, _ in guarded.api.removed]
    assert _guard_name() in removed and "game-world-demo" in removed
    assert guarded._by_name["game-world-demo"].status == "running"


def test_sleep_stops_the_world_before_its_guard(guarded):
    orch = DockerOrchestrator(client=guarded)
    orch.start(SPEC)
    orch.sleep(SPEC.subdomain)

    assert [ref for ref, _ in guarded.api.stopped] == ["game-world-demo", _guard_name()]
    # Asleep, not off: both containers stay so the world can be woken.
    assert _guard_name() in guarded._by_name


def test_stop_and_delete_take_the_guard_with_the_world(guarded):
    orch = DockerOrchestrator(client=guarded)
    orch.start(SPEC)
    orch.stop(SPEC.subdomain)
    assert _guard_name() not in guarded._by_name

    orch.start(SPEC)
    orch.delete(SPEC.subdomain)
    assert _guard_name() not in guarded._by_name


def test_the_emergency_switch_rebuilds_the_world_without_its_guard(guarded, monkeypatch):
    from open_worlds.common.config import get_settings

    orch = DockerOrchestrator(client=guarded)
    orch.start(SPEC)
    orch.sleep(SPEC.subdomain)

    monkeypatch.setenv("PLATFORM_EGRESS_GUARD_ENABLED", "false")
    get_settings.cache_clear()
    DockerOrchestrator(client=guarded).start(SPEC)

    assert _guard_name() not in guarded._by_name
    assert guarded.api.created_spec["network"] == "games"


def test_guard_suffix_cannot_be_claimed_as_a_subdomain():
    from open_worlds.common.subdomain import InvalidSubdomain, validate_subdomain

    with pytest.raises(InvalidSubdomain):
        validate_subdomain("demo--egress")


def test_waking_a_sleeping_guarded_world_reuses_both_containers(guarded):
    orch = DockerOrchestrator(client=guarded)
    orch.start(SPEC)
    orch.sleep(SPEC.subdomain)
    created_before = len(guarded.api.created)

    orch.start(SPEC)

    assert len(guarded.api.created) == created_before
    assert guarded.api.removed == []
    assert guarded.api.started[-2:] == [_guard_name(), "game-world-demo"]


def test_log_only_mode_reaches_the_guard_and_switching_it_replaces_both(guarded):
    orch = DockerOrchestrator(client=guarded)
    orch.start(SPEC)
    guard = next(c for c in guarded.api.created if c["name"] == _guard_name())
    assert guard["environment"]["GUARD_MODE"] == "enforce"
    orch.sleep(SPEC.subdomain)

    orch.start(replace(SPEC, egress_log_only=True))

    guard = [c for c in guarded.api.created if c["name"] == _guard_name()][-1]
    assert guard["environment"]["GUARD_MODE"] == "log"
    # A new guard means a new namespace; the world is rebuilt to join it.
    removed = [ref for ref, _ in guarded.api.removed]
    assert _guard_name() in removed and "game-world-demo" in removed


def test_the_guard_is_told_its_world_game_and_variant(guarded):
    orch = DockerOrchestrator(client=guarded)
    orch.start(SPEC)
    env = next(c for c in guarded.api.created if c["name"] == _guard_name())["environment"]
    assert (env["GUARD_GAME"], env["GUARD_VARIANT"], env["GUARD_WORLD"]) == ("PALWORLD", "", "demo")
    orch.sleep(SPEC.subdomain)
    orch.start(_spec("MINECRAFT"))
    env = [c for c in guarded.api.created if c["name"] == _guard_name()][-1]["environment"]
    assert (env["GUARD_GAME"], env["GUARD_VARIANT"]) == ("MINECRAFT", "FABRIC")


def test_a_palworld_guard_never_gets_a_variant(guarded):
    # A stray server_type on a Palworld world must not reach the guard, which
    # would refuse to start on a variant Palworld does not have.
    orch = DockerOrchestrator(client=guarded)
    orch.start(_spec("PALWORLD", settings={"server_type": "PAPER"}))
    env = next(c for c in guarded.api.created if c["name"] == _guard_name())["environment"]
    assert (env["GUARD_GAME"], env["GUARD_VARIANT"]) == ("PALWORLD", "")


def test_converting_a_world_to_another_loader_replaces_its_guard(guarded):
    fabric = _spec("MINECRAFT")
    orch = DockerOrchestrator(client=guarded)
    orch.start(fabric)
    orch.sleep(fabric.subdomain)
    orch.start(replace(fabric, settings={**fabric.settings, "server_type": ServerType.FORGE.value}))
    removed = [ref for ref, _ in guarded.api.removed]
    assert _guard_name() in removed
    guard = [c for c in guarded.api.created if c["name"] == _guard_name()][-1]
    assert guard["environment"]["GUARD_VARIANT"] == "FORGE"


def test_the_guard_policy_knows_every_game_and_server_type():
    """deploy/games/<game>/egress.py names each game and its variants; they
    must track the adapters, or a new game's guard refuses to start."""
    import pathlib
    import sys

    root = pathlib.Path(__file__).resolve().parents[2]
    trust_dir = root / "deploy" / "egress-guard"
    if not (trust_dir / "trust.py").exists():
        pytest.skip("deploy/ not present beside api/")
    sys.path.insert(0, str(trust_dir))
    try:
        import trust
    finally:
        sys.path.remove(str(trust_dir))
    assert set(trust.GAMES) == game_ids()
    assert trust.GAMES["MINECRAFT"] == {t.value for t in ServerType}


# --- install and play stages ------------------------------------------------


@pytest.fixture
def staged(monkeypatch, tmp_path):
    """A fresh worlds dir, with the adapters' real install checks."""
    from open_worlds.common.config import get_settings

    monkeypatch.setenv("PLATFORM_WORLDS_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _data(root, subdomain="demo"):
    path = root / subdomain / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _exit(container, code):
    container.status = "exited"
    container.attrs["State"]["ExitCode"] = code


def _guard_env(client):
    return client._by_name[_guard_name()].labels, next(
        c["environment"] for c in reversed(client.api.created) if c["name"] == _guard_name()
    )


def _world_env(client):
    return next(
        c["environment"] for c in reversed(client.api.created) if c["name"] == "game-world-demo"
    )


def _install_palworld(data):
    (data / "PalServer.sh").write_text("#!/bin/sh\n")
    (data / "steamapps").mkdir()
    (data / "steamapps" / "appmanifest_2394010.acf").write_text("{}")


def test_an_uninstalled_world_installs_first_behind_the_install_list(staged, guarded):
    status = DockerOrchestrator(client=guarded).start(SPEC)

    assert status.stage == INSTALL
    assert status.run_state is RunState.RUNNING
    world = guarded._by_name["game-world-demo"]
    assert world.labels["open-worlds.stage"] == INSTALL
    assert _world_env(guarded)["SETUP_ONLY"] == "true"
    assert _guard_env(guarded)[1]["GUARD_STAGE"] == INSTALL


def test_a_running_install_is_left_alone(staged, guarded):
    orch = DockerOrchestrator(client=guarded)
    orch.start(SPEC)
    created = len(guarded.api.created)

    status = orch.start(SPEC)

    assert len(guarded.api.created) == created
    assert status.stage == INSTALL


def test_a_finished_install_reads_as_still_starting(staged, guarded):
    orch = DockerOrchestrator(client=guarded)
    orch.start(SPEC)
    _exit(guarded._by_name["game-world-demo"], 0)

    status = orch.inspect(SPEC.subdomain)

    # Not EXITED, which would read as a sleeping world (or, non-zero, a crash).
    assert status.run_state is RunState.RUNNING
    assert status.install_finished
    assert status.exit_code is None
    guarded.containers.list.return_value = [guarded._by_name["game-world-demo"]]
    assert [s.install_finished for s in orch.list_managed()] == [True]


def test_a_failed_install_reads_as_a_crash(staged, guarded):
    orch = DockerOrchestrator(client=guarded)
    orch.start(SPEC)
    _exit(guarded._by_name["game-world-demo"], 1)

    status = orch.inspect(SPEC.subdomain)

    assert status.run_state is RunState.EXITED
    assert status.exit_code == 1
    assert not status.install_finished


def test_after_the_install_the_server_runs_behind_the_play_list(staged, guarded):
    orch = DockerOrchestrator(client=guarded)
    orch.start(SPEC)
    install_guard = guarded._by_name[_guard_name()].id
    _install_palworld(_data(staged))
    _exit(guarded._by_name["game-world-demo"], 0)

    status = orch.start(SPEC)

    assert status.stage == PLAY
    assert status.run_state is RunState.RUNNING
    assert ("game-world-demo", True) in guarded.api.removed
    env = _world_env(guarded)
    assert "SETUP_ONLY" not in env
    assert env["UPDATE_ON_BOOT"] == "false"
    # A new guard: the install's list is gone with it.
    labels, guard_env = _guard_env(guarded)
    assert guard_env["GUARD_STAGE"] == PLAY
    assert guarded._by_name[_guard_name()].id != install_guard
    world = guarded._by_name["game-world-demo"]
    assert (
        world.attrs["HostConfig"]["NetworkMode"]
        == f"container:{guarded._by_name[_guard_name()].id}"
    )


def test_an_install_that_left_nothing_fails_and_is_cleared(staged, guarded):
    from open_worlds.common.games.base import InstallIncomplete

    orch = DockerOrchestrator(client=guarded)
    orch.start(SPEC)
    _exit(guarded._by_name["game-world-demo"], 0)

    with pytest.raises(InstallIncomplete):
        orch.start(SPEC)

    # So the next start installs again rather than failing the same way.
    assert "game-world-demo" not in guarded._by_name
    orch.start(SPEC)
    assert guarded._by_name["game-world-demo"].labels["open-worlds.stage"] == INSTALL


def test_an_interrupted_install_runs_again(staged, guarded):
    orch = DockerOrchestrator(client=guarded)
    orch.start(SPEC)
    first = guarded._by_name["game-world-demo"].id
    _exit(guarded._by_name["game-world-demo"], 143)

    status = orch.start(SPEC)

    assert status.stage == INSTALL
    assert guarded._by_name["game-world-demo"].id != first


def test_an_installed_world_goes_straight_to_the_server(staged, guarded):
    _install_palworld(_data(staged))

    status = DockerOrchestrator(client=guarded).start(SPEC)

    assert status.stage == PLAY
    assert _guard_env(guarded)[1]["GUARD_STAGE"] == PLAY


def test_a_running_server_is_never_reinstalled_under(staged, guarded, monkeypatch):
    _install_palworld(_data(staged))
    orch = DockerOrchestrator(client=guarded)
    orch.start(SPEC)
    created = len(guarded.api.created)
    # As if its install had aged out while it ran.
    monkeypatch.setattr(type(adapter_for("PALWORLD")), "install_needed", lambda *_: True)

    status = orch.start(SPEC)

    assert status.stage == PLAY
    assert len(guarded.api.created) == created
    assert not guarded.api.removed


MC = _spec("MINECRAFT", settings={"version": "1.21.4", "server_type": ServerType.PAPER.value})


def _finish_paper_install(data, server="paper-1.21.4-232.jar"):
    (data / ".paper.env").write_text(f'SERVER="/data/{server}"\nPAPER_VERSION=1.21.4\n')
    (data / server).write_text("jar")
    for d in ("libraries", "versions", "cache"):
        (data / d).mkdir(exist_ok=True)


def test_minecraft_installs_without_the_worlds_mods_or_plugins(staged, guarded):
    data = _data(staged)
    (data / "plugins").mkdir()

    DockerOrchestrator(client=guarded).start(MC)

    host = guarded.api.created[-1]["host_config"]["_host_config"]
    # Only what exists: Docker would create a missing one on the world's volume.
    assert host["tmpfs"] == {"/data/plugins": "size=1m"}
    env = _world_env(guarded)
    assert env["TYPE"] == "PAPER"
    assert env["JVM_DD_OPTS"] == "paperclip.patchonly:true"
    assert "SETUP_ONLY" not in env


def test_minecraft_runs_the_server_its_install_left(staged, guarded):
    data = _data(staged)
    orch = DockerOrchestrator(client=guarded)
    orch.start(MC)
    _finish_paper_install(data)
    _exit(guarded._by_name["game-world-demo"], 0)

    orch.start(MC)

    env = _world_env(guarded)
    assert env["TYPE"] == "CUSTOM"
    assert env["CUSTOM_SERVER"] == "/data/paper-1.21.4-232.jar"
    assert env["FAMILY"] == "SPIGOT"
    assert "tmpfs" not in guarded.api.created[-1]["host_config"]["_host_config"]
    assert (data / ".open-worlds-install.json").exists()


def test_a_changed_minecraft_version_installs_again(staged, guarded):
    data = _data(staged)
    orch = DockerOrchestrator(client=guarded)
    orch.start(MC)
    _finish_paper_install(data)
    _exit(guarded._by_name["game-world-demo"], 0)
    orch.start(MC)
    orch.sleep(MC.subdomain)

    status = orch.start(replace(MC, settings={**MC.settings, "version": "1.21.5"}))

    assert status.stage == INSTALL
