"""World containers over the Docker Engine API.

Game-agnostic: image, environment, mounts and labels come from the world's
GameAdapter. Containers are disposable and rebuilt from a WorldSpec; the
database is the source of truth.

Run state maps to world state: running -> RUNNING, exited but present ->
SLEEPING (a router can wake it), absent -> OFF. Turning a world off removes its
container, which is how the routers tell "asleep" from "off".

A game with an install stage (``GameAdapter.install_needed``) runs twice: an
install container downloads the server behind the install egress list and
exits; the next start runs the server behind the play list. Both use the
world's container name, so the rest of the platform sees one world.

Managed containers are found by label, and nothing without our label is ever
touched. World containers publish no host ports; the routers reach them on the
private games network.
"""

from __future__ import annotations

import abc
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

import httpx2

from .config import Settings, get_settings
from .docker_policy import check_create
from .games import adapter_for, all_adapters
from .games.base import InstallIncomplete
from .logging import get_logger

log = get_logger(__name__)


class RunState(str, Enum):
    RUNNING = "RUNNING"
    EXITED = "EXITED"
    ABSENT = "ABSENT"


class WorldImageUnavailable(RuntimeError):
    """The game's container image could not be pulled: almost always a bad pin
    or a registry outage, so nothing about the world is wrong."""


class EgressGuardUnavailable(WorldImageUnavailable):
    """The world's egress guard could not be brought up, so the world was not
    started. Usually the guard image is not built on this host.

    Fatal on purpose: a world started anyway would run unfenced or with no
    network. Callers treat it like WorldImageUnavailable: a platform problem,
    and the world's state is left alone.
    """


class ContainerNameConflict(RuntimeError):
    """The target container name is held by a container we do not manage."""


@dataclass(frozen=True)
class WorldSpec:
    """A container's full description, derived from the world row. Each adapter
    reads only the fields its game uses."""

    game: str
    world_id: str
    owner_id: str
    subdomain: str
    display_name: str
    memory_mb: int
    max_players: int
    admin_password: str

    # For games with ``supports_server_password``. Empty means open.
    server_password: str = ""
    # Allocated host port, for games that allocate one. A label too, so the
    # waker's routing table can be rebuilt from Docker alone.
    game_port: int | None = None
    # Already validated by the adapter, which is the only reader.
    settings: dict = field(default_factory=dict)
    # Runtime facts from the adapter's spec_extras.
    extras: dict = field(default_factory=dict)
    # Already resolved against PLATFORM_EGRESS_OVERRIDE_ACTORS by spec_for.
    egress_log_only: bool = False


@dataclass
class ContainerStatus:
    subdomain: str
    world_id: str | None
    run_state: RunState
    container_id: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    # Exit code of a stopped container: a graceful stop (0/143) or a crash.
    exit_code: int | None = None
    # Docker's ISO timestamp, to de-duplicate crash events across polls.
    finished_at: str | None = None
    # The only way to tell an OOM kill from a stop timeout's SIGKILL: both
    # exit 137.
    oom_killed: bool = False
    # INSTALL or PLAY: which container holds the world's name.
    stage: str = "play"
    # The install exited cleanly: reported as RUNNING (the world is still
    # starting) until a start() replaces it with the server.
    install_finished: bool = False


#: The two containers a world can run as, and the egress list each runs behind.
INSTALL = "install"
PLAY = "play"


def container_name(subdomain: str) -> str:
    """The container a world runs in. The one definition, so route publishing
    and the placeholder's relay address what the orchestrator created."""
    return f"{get_settings().container_name_prefix}{subdomain}"


#: Appended to a world's container name to name its egress guard. Subdomain
#: validation reserves it, so no world can be named into a guard's slot.
GUARD_SUFFIX = "--egress"


def guard_enabled() -> bool:
    """Whether worlds run behind egress guards: always, unless the operator has
    pulled the emergency switch (PLATFORM_EGRESS_GUARD_ENABLED=false)."""
    return get_settings().egress_guard_enabled


def backend_address(subdomain: str, game: str) -> str:
    """Where to reach a running world on the private games network."""
    return f"{container_name(subdomain)}:{adapter_for(game).container_port}"


class Orchestrator(abc.ABC):
    """Lifecycle operations the control plane depends on."""

    @abc.abstractmethod
    def start(self, spec: WorldSpec) -> ContainerStatus: ...

    @abc.abstractmethod
    def stop(self, subdomain: str, graceful: bool = True) -> None:
        """Explicit OFF: remove the container so the waker will not wake it.

        ``graceful=False`` skips the shutdown window, for a server that never
        became ready: it has nothing to save and often ignores SIGTERM.
        """

    @abc.abstractmethod
    def sleep(self, subdomain: str, graceful: bool = True) -> None:
        """Idle sleep: stop the container but keep it, so it can be woken."""

    @abc.abstractmethod
    def logs(self, subdomain: str, tail: int = 400) -> str:
        """The last ``tail`` lines of the container's current run; "" if none.

        Docker keeps the log across restarts, so an unbounded tail would show a
        fixed world the failure of its previous boot.
        """

    @abc.abstractmethod
    def delete(self, subdomain: str) -> None: ...

    @abc.abstractmethod
    def inspect(self, subdomain: str) -> ContainerStatus: ...

    @abc.abstractmethod
    def list_managed(self) -> list[ContainerStatus]: ...


# The low-level create, split into container config and HostConfig, because
# ``containers.create()`` refuses ``stop_timeout``. Every template key must be in
# exactly one of these sets; an unknown one raises rather than being dropped.
_HOST_CONFIG_KEYS = frozenset(
    {
        "cap_add",
        "cap_drop",
        "dns",
        "mem_limit",
        "memswap_limit",
        "nano_cpus",
        "pids_limit",
        "restart_policy",
        "security_opt",
        "tmpfs",
    }
)
_CONTAINER_CONFIG_KEYS = frozenset(
    {"detach", "environment", "image", "labels", "name", "stdin_open", "stop_timeout", "tty"}
)
# Handled by hand because Docker wants them in both halves, in different shapes.
_SPLIT_KEYS = frozenset({"network", "volumes"})


def _started_epoch(container) -> int | None:  # type: ignore[no-untyped-def]
    """When the container's current (or most recent) run began, as Unix
    seconds, for ``docker logs --since``. None if Docker does not say."""
    from datetime import datetime

    raw = (container.attrs.get("State") or {}).get("StartedAt") or ""
    if not raw or raw.startswith("0001-"):
        return None
    # RFC 3339 with nanoseconds; fromisoformat takes at most microseconds.
    head, _, frac = raw.rstrip("Z").partition(".")
    try:
        return int(datetime.fromisoformat(head + "+00:00").timestamp())
    except ValueError:
        return None


class DockerOrchestrator(Orchestrator):
    def __init__(self, settings: Settings | None = None, client: object | None = None) -> None:
        self._s = settings or get_settings()
        self._client = client

    # Lazy, so importing this module never needs a socket.
    @property
    def client(self):  # type: ignore[no-untyped-def]
        if self._client is None:
            import docker

            self._client = docker.DockerClient(base_url=self._s.docker_host)
        return self._client

    def _container_name(self, subdomain: str) -> str:
        return container_name(subdomain)

    def _labels(self, spec: WorldSpec) -> dict[str, str]:
        ns = self._s.label_namespace
        labels = {
            ns: "true",
            f"{ns}.world_id": spec.world_id,
            f"{ns}.owner_id": spec.owner_id,
            f"{ns}.subdomain": spec.subdomain,
            # So a reconcile knows the game without the database.
            f"{ns}.game": spec.game,
        }
        labels.update(adapter_for(spec.game).labels(spec))
        return labels

    def _data_dir(self, subdomain: str) -> str:
        return f"{self._s.worlds_dir.rstrip('/')}/{subdomain}/data"

    def _cpu_limit(self) -> dict[str, int]:
        """The per-world CPU cap, only when PLATFORM_CONTAINER_CPUS is set: a
        sensible figure depends on the host's cores and how many worlds run."""
        cpus = self._s.container_cpus
        if cpus <= 0:
            return {}
        return {"nano_cpus": int(cpus * 1_000_000_000)}

    def _dns(self) -> dict[str, Any]:
        """Point worlds at the dns-observer, which records the names they look up.

        Names are the only reliable way to identify a CDN address a world
        reached. The fallback is always appended, so a dead observer costs a
        slow lookup rather than a broken world. Docker's embedded resolver still
        answers container names.
        """
        primary = (self._s.world_dns or "").strip()
        if not primary:
            return {}
        servers = [primary]
        fallback = (self._s.world_dns_fallback or "").strip()
        if fallback and fallback != primary:
            servers.append(fallback)
        return {"dns": servers}

    def _is_managed(self, container) -> bool:  # type: ignore[no-untyped-def]
        return (container.labels or {}).get(self._s.label_namespace) == "true"

    def _get_container(self, subdomain: str):  # type: ignore[no-untyped-def]
        import docker.errors

        try:
            container = self.client.containers.get(self._container_name(subdomain))
        except docker.errors.NotFound:
            return None
        # A valid subdomain can derive a platform container's name. Never touch a
        # container without our label.
        if not self._is_managed(container):
            log.warning(
                "refusing to operate on unmanaged container with colliding name",
                extra={"subdomain": subdomain, "container_name": container.name},
            )
            return None
        return container

    @staticmethod
    def _run_state(container) -> RunState:  # type: ignore[no-untyped-def]
        status = (container.status or "").lower()
        if status == "running":
            return RunState.RUNNING
        return RunState.EXITED

    @staticmethod
    def _exit_code(container) -> int | None:  # type: ignore[no-untyped-def]
        try:
            return int(container.attrs.get("State", {}).get("ExitCode"))
        except (AttributeError, TypeError, ValueError):
            return None

    @staticmethod
    def _oom_killed(container) -> bool:  # type: ignore[no-untyped-def]
        try:
            return bool(container.attrs.get("State", {}).get("OOMKilled"))
        except AttributeError:
            return False

    @staticmethod
    def _finished_at(container) -> str | None:  # type: ignore[no-untyped-def]
        try:
            return container.attrs.get("State", {}).get("FinishedAt")
        except AttributeError:
            return None

    def _ensure_name_available(self, subdomain: str) -> None:
        """Raise if the derived name is held by a container we do not manage."""
        import docker.errors

        try:
            existing = self.client.containers.get(self._container_name(subdomain))
        except docker.errors.NotFound:
            return
        raise ContainerNameConflict(
            f"Container name '{existing.name}' is already in use by a container "
            f"not managed by this platform; refusing to touch it. Remove or rename "
            f"that container, or choose a different subdomain."
        )

    def _ensure_image(self, image: str) -> None:
        """Pull the world image if it is not present locally.

        ``containers.create`` never pulls, and the image can vanish (fresh host,
        prune, a new storage root after enabling userns-remap). A failed pull
        names the image instead of surfacing as an anonymous start failure.
        """
        import docker.errors

        try:
            self.client.images.get(image)
            return
        except docker.errors.ImageNotFound:
            log.info("world image missing; pulling", extra={"image": image})
        try:
            self.client.images.pull(image)
        except docker.errors.APIError as exc:
            log.error("world image could not be pulled", extra={"image": image, "error": str(exc)})
            raise WorldImageUnavailable(
                f"Could not pull the game image '{image}': {exc}. The image pin is "
                f"probably wrong or the registry is unreachable."
            ) from exc

    def _create_container(self, adapter, spec: WorldSpec, stage: str):  # type: ignore[no-untyped-def]
        """Create the world's container, low-level, so it can carry StopTimeout:
        stops we did not issue (``docker stop``, host reboot) then still wait
        for the server to save."""
        kwargs = self._create_kwargs(adapter, spec, stage)
        kwargs["labels"] = {
            **kwargs["labels"],
            f"{self._s.label_namespace}.spec": self._spec_fingerprint(adapter, spec, stage),
        }

        host: dict = {}
        create: dict = {}
        for key, value in kwargs.items():
            if key in _HOST_CONFIG_KEYS:
                host[key] = value
            elif key in _CONTAINER_CONFIG_KEYS:
                create[key] = value
            elif key in _SPLIT_KEYS:
                continue
            else:
                # Dropping it would leave a hardening flag set in the template
                # and absent on the container.
                raise RuntimeError(
                    f"container setting {key!r} is not routed to the container "
                    f"config or the host config; add it to one of the sets in "
                    f"common/orchestrator.py"
                )

        # Docker wants these in both halves, in different shapes.
        binds = kwargs["volumes"]
        host["binds"] = binds
        create["volumes"] = [v["bind"] for v in binds.values()]
        network = kwargs["network"]
        host["network_mode"] = network

        api = self.client.api
        networking = {}
        if not network.startswith("container:"):
            # Docker refuses an endpoint alongside container: mode.
            networking["networking_config"] = api.create_networking_config(
                {network: api.create_endpoint_config()}
            )
        host_config = api.create_host_config(**host)
        self._check_create(kwargs["image"], host_config)
        created = api.create_container(
            host_config=host_config,
            **networking,
            **create,
        )
        return self.client.containers.get(created["Id"])

    def _check_create(self, image: str, host_config: dict) -> None:
        """Refuse here what the socket proxy would refuse, with a reason."""
        check_create(
            image,
            host_config,
            allowed_images=[a.image for a in all_adapters()] + [self._s.egress_guard_image],
            worlds_root=self._s.worlds_dir,
            games_network=self._s.games_network,
        )

    def _create_kwargs(self, adapter, spec: WorldSpec, stage: str) -> dict:  # type: ignore[no-untyped-def]
        """Everything that defines a world's container. The create call and the
        drift fingerprint both read this, so they cannot disagree."""
        data_dir = self._data_dir(spec.subdomain)
        extra: dict[str, Any] = {}
        if stage == INSTALL:
            environment = adapter.install_environment(spec)
            # The world's own code stays out of the install, which can reach
            # more than the server ever may. Only what exists: Docker would
            # create a missing mount point as root on the world's volume.
            hidden = [
                p for p in adapter.install_hidden_paths if os.path.isdir(os.path.join(data_dir, p))
            ]
            if hidden:
                extra["tmpfs"] = {f"{adapter.data_mount}/{p}": "size=1m" for p in hidden}
        else:
            environment = adapter.run_environment(spec, data_dir)
        if guard_enabled():
            # The world joins its guard's namespace, so the guard's firewall is
            # the world's. Docker refuses resolver settings with container: mode.
            network = {"network": f"container:{self._guard_name(spec.subdomain)}"}
        else:
            network = {"network": self._s.games_network, **self._dns()}
        return dict(
            image=adapter.image,
            name=self._container_name(spec.subdomain),
            environment=environment,
            labels={**self._labels(spec), f"{self._s.label_namespace}.stage": stage},
            **network,
            mem_limit=f"{spec.memory_mb}m",
            # No swap: a world that outgrows its memory is OOM-killed and
            # reported, rather than slowing the whole host.
            memswap_limit=f"{spec.memory_mb}m",
            **self._cpu_limit(),
            volumes={data_dir: {"bind": adapter.data_mount, "mode": "rw"}},
            restart_policy={"Name": "no"},
            # An untrusted server runs here: no privilege escalation, only
            # the capabilities its entrypoint needs to chown and drop root,
            # and a pid cap.
            security_opt=["no-new-privileges:true"],
            cap_drop=["ALL"],
            cap_add=list(adapter.container_caps),
            pids_limit=self._s.container_pids_limit,
            detach=True,
            # A TTY once hung every Palworld stop until SIGKILL; fixed upstream
            # in palworld-server-docker v2.8.0 (#955). Stdin stays closed: the
            # platform never attaches to the console.
            tty=True,
            stdin_open=False,
            # Here rather than at the create call, so the fingerprint covers it.
            stop_timeout=adapter.stop_timeout_seconds,
            **extra,
        )

    def _spec_fingerprint(self, adapter, spec: WorldSpec, stage: str) -> str:  # type: ignore[no-untyped-def]
        """A short digest of the container template, stamped at create and
        compared on every start. Docker's own view normalises values (bytes,
        reordered caps, defaults), so a field diff would report false drift."""
        payload = self._create_kwargs(adapter, spec, stage)
        # Name is identity, and labels carry this digest.
        payload.pop("name", None)
        payload.pop("labels", None)
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def _config_drifted(self, adapter, spec: WorldSpec, container, stage: str) -> bool:  # type: ignore[no-untyped-def]
        """Whether this container was built from a template we no longer use.
        An unstamped container counts: it predates the check."""
        if self._stage_of(container) != stage:
            return True
        stamped = (container.labels or {}).get(f"{self._s.label_namespace}.spec")
        return stamped != self._spec_fingerprint(adapter, spec, stage)

    def _stage_of(self, container) -> str:  # type: ignore[no-untyped-def]
        # Unlabelled containers predate install stages and ran the server.
        return (container.labels or {}).get(f"{self._s.label_namespace}.stage") or PLAY

    def _install_finished(self, container) -> bool:  # type: ignore[no-untyped-def]
        return (
            self._stage_of(container) == INSTALL
            and self._run_state(container) is RunState.EXITED
            and self._exit_code(container) == 0
        )

    def _status(self, container, subdomain: str | None = None) -> ContainerStatus:  # type: ignore[no-untyped-def]
        labels = dict(container.labels or {})
        ns = self._s.label_namespace
        finished = self._install_finished(container)
        # A finished install is a world still starting, not a stopped one.
        run_state = RunState.RUNNING if finished else self._run_state(container)
        exited = run_state is RunState.EXITED
        return ContainerStatus(
            subdomain=subdomain if subdomain is not None else labels.get(f"{ns}.subdomain", ""),
            world_id=labels.get(f"{ns}.world_id"),
            run_state=run_state,
            container_id=container.id,
            labels=labels,
            exit_code=self._exit_code(container) if exited else None,
            finished_at=self._finished_at(container) if exited else None,
            oom_killed=self._oom_killed(container) if exited else False,
            stage=self._stage_of(container),
            install_finished=finished,
        )

    def _stage_for(self, adapter, spec: WorldSpec, container) -> str:  # type: ignore[no-untyped-def]
        """Which container the world should run as now."""
        if (
            container is not None
            and self._stage_of(container) == PLAY
            and self._run_state(container) is RunState.RUNNING
        ):
            # Never reinstall under a running server: one that is due (by age)
            # waits for the world's next start.
            try:
                if not self._config_drifted(adapter, spec, container, PLAY):
                    return PLAY
            except InstallIncomplete:
                pass
        return INSTALL if adapter.install_needed(spec, self._data_dir(spec.subdomain)) else PLAY

    # --- egress guard -------------------------------------------------------
    # A guarded world is two containers: the guard owns the network namespace
    # (and the world's name on the games network) and the world joins it. The
    # guard is stopped while the world sleeps and removed with it.

    def _guard_name(self, subdomain: str) -> str:
        return f"{self._container_name(subdomain)}{GUARD_SUFFIX}"

    def _guard_upstreams(self) -> str:
        """Where the guard forwards approved names: the observer, then the
        fallback. Only the guard can reach them, so the world cannot bypass the
        observer."""
        servers = [s for s in (self._s.world_dns.strip(), self._s.world_dns_fallback.strip()) if s]
        return ",".join(dict.fromkeys(servers)) or "1.1.1.1"

    def _guard_kwargs(self, spec: WorldSpec, stage: str) -> dict:
        ns = self._s.label_namespace
        return dict(
            image=self._s.egress_guard_image,
            name=self._guard_name(spec.subdomain),
            # Not the world label, so a guard is never taken for a world or an
            # orphan.
            labels={f"{ns}.guard": "true", f"{ns}.subdomain": spec.subdomain},
            environment={
                "GUARD_UPSTREAMS": self._guard_upstreams(),
                # Mode and policy are in the template, so changing either
                # replaces the guard (and the world that joined it).
                "GUARD_MODE": "log" if spec.egress_log_only else "enforce",
                "GUARD_GAME": spec.game,
                "GUARD_VARIANT": adapter_for(spec.game).egress_variant(spec.settings),
                # Which of the game's lists: the install's or the server's.
                "GUARD_STAGE": stage,
                # Named in every refusal the guard records.
                "GUARD_WORLD": spec.subdomain,
                # Refusals are reported to the dns-observer, which alerts.
                "GUARD_REPORT": (
                    f"{self._s.world_dns.strip()}:{self._s.dns_observer_report_port}"
                    if self._s.world_dns.strip()
                    else ""
                ),
            },
            network=self._s.games_network,
            # Everything reaches a world as game-world-<subdomain>, and that
            # address is now the guard's.
            aliases=[self._container_name(spec.subdomain)],
            # Docker's resolver hands external names to the guard. No search
            # domains, or each refused name is retried with a suffix.
            dns=["127.0.0.1"],
            dns_search=["."],
            # The guard marks its own upstream queries by binding below 1024;
            # keeping that range privileged stops the world forging them.
            sysctls={"net.ipv4.ip_unprivileged_port_start": "1024"},
            cap_drop=["ALL"],
            cap_add=["NET_ADMIN", "NET_BIND_SERVICE"],
            security_opt=["no-new-privileges:true"],
            read_only=True,
            tmpfs={"/run": "size=1m"},
            mem_limit="64m",
            pids_limit=64,
            restart_policy={"Name": "no"},
        )

    def _guard_image_id(self) -> str:
        import docker.errors

        try:
            return self.client.images.get(self._s.egress_guard_image).id
        except docker.errors.ImageNotFound as exc:
            raise EgressGuardUnavailable(
                f"The egress guard image '{self._s.egress_guard_image}' is not built on "
                f"this host. Build it from deploy/ with "
                f"`docker compose --profile guard build egress-guard`."
            ) from exc

    def _guard_fingerprint(self, spec: WorldSpec, image_id: str, stage: str) -> str:
        """Template digest plus the image id: the policy is baked into the
        image, so a rebuild under the same tag must replace the guard."""
        payload = self._guard_kwargs(spec, stage)
        payload.pop("labels")
        payload["image_id"] = image_id
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def _get_guard(self, subdomain: str, *, for_create: bool = False):  # type: ignore[no-untyped-def]
        """The world's guard, or None. A container holding the name without our
        guard label is ignored on the way down and refused on the way up."""
        import docker.errors

        try:
            guard = self.client.containers.get(self._guard_name(subdomain))
        except docker.errors.NotFound:
            return None
        if (guard.labels or {}).get(f"{self._s.label_namespace}.guard") != "true":
            if for_create:
                raise ContainerNameConflict(
                    f"Container name '{guard.name}' is held by a container that is not "
                    f"this platform's egress guard; refusing to touch it."
                )
            return None
        return guard

    def _create_guard(self, spec: WorldSpec, image_id: str, stage: str):  # type: ignore[no-untyped-def]
        kw = self._guard_kwargs(spec, stage)
        kw["labels"] = {
            **kw["labels"],
            f"{self._s.label_namespace}.spec": self._guard_fingerprint(spec, image_id, stage),
        }
        api = self.client.api
        host_config = api.create_host_config(
            network_mode=kw["network"],
            **{
                k: kw[k]
                for k in (
                    "dns",
                    "dns_search",
                    "sysctls",
                    "cap_drop",
                    "cap_add",
                    "security_opt",
                    "read_only",
                    "tmpfs",
                    "mem_limit",
                    "pids_limit",
                    "restart_policy",
                )
            },
        )
        self._check_create(kw["image"], host_config)
        created = api.create_container(
            image=kw["image"],
            name=kw["name"],
            labels=kw["labels"],
            environment=kw["environment"],
            detach=True,
            host_config=host_config,
            networking_config=api.create_networking_config(
                {kw["network"]: api.create_endpoint_config(aliases=kw["aliases"])}
            ),
        )
        return self.client.containers.get(created["Id"])

    def _ensure_guard(self, spec: WorldSpec, stage: str) -> tuple[Any, bool]:
        """Bring the guard up and wait until it has proven its firewall holds.

        Returns (guard, changed): changed means a running world must be
        restarted into the guard's new namespace.
        """
        image_id = self._guard_image_id()
        guard = self._get_guard(spec.subdomain, for_create=True)
        stamped = (guard.labels or {}).get(f"{self._s.label_namespace}.spec") if guard else None
        if guard is not None and stamped != self._guard_fingerprint(spec, image_id, stage):
            log.info(
                "recreating egress guard: template or policy changed",
                extra={"subdomain": spec.subdomain},
            )
            self.client.api.remove_container(guard.name, force=True)
            guard = None
        changed = False
        if guard is None:
            log.info("creating egress guard", extra={"subdomain": spec.subdomain})
            guard = self._create_guard(spec, image_id, stage)
            changed = True
        guard.reload()
        if self._run_state(guard) is not RunState.RUNNING:
            self.client.api.start(guard.name)
            changed = True
        self._wait_guard_ready(guard)
        return guard, changed

    def _wait_guard_ready(self, guard) -> None:  # type: ignore[no-untyped-def]
        import time

        deadline = time.monotonic() + self._s.egress_guard_ready_seconds
        while True:
            guard.reload()
            state = guard.attrs.get("State", {})
            health = state.get("Health", {}).get("Status")
            if health == "healthy":
                return
            gone = self._run_state(guard) is not RunState.RUNNING or health == "unhealthy"
            if gone or time.monotonic() > deadline:
                break
            time.sleep(0.5)
        try:
            tail = self.client.api.logs(guard.name, tail=5).decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001 - the logs are a courtesy, the refusal is not
            tail = ""
        raise EgressGuardUnavailable(
            f"Egress guard '{guard.name}' did not become ready "
            f"(status {guard.status}, health {health}); the world was not "
            f"started. {tail}".strip()
        )

    def _remove_guard(self, subdomain: str) -> None:
        guard = self._get_guard(subdomain)
        if guard is not None:
            log.info("removing egress guard", extra={"subdomain": subdomain})
            self.client.api.remove_container(guard.name, force=True)

    def _stop_guard(self, subdomain: str) -> None:
        guard = self._get_guard(subdomain)
        if guard is not None:
            self.client.api.stop(guard.name, timeout=5)

    def start(self, spec: WorldSpec) -> ContainerStatus:
        adapter = adapter_for(spec.game)
        data_dir = self._data_dir(spec.subdomain)
        # Every start, not just create: an existing container is restarted, and
        # a game that reads config once at boot needs it current first.
        adapter.prepare_data_dir(spec, data_dir)
        container = self._get_container(spec.subdomain)
        if container is not None and self._stage_of(container) == INSTALL:
            container.reload()
            if self._run_state(container) is RunState.RUNNING:
                # Still installing; the worker starts the server when it exits.
                return self._status(container, spec.subdomain)
            try:
                if self._exit_code(container) == 0:
                    log.info("install finished", extra={"subdomain": spec.subdomain})
                    adapter.finish_install(spec, data_dir)
                else:
                    log.info(
                        "installing again: the last install did not finish",
                        extra={"subdomain": spec.subdomain},
                    )
            finally:
                # Either way it is done: the next container is a fresh install
                # or the server.
                self.client.api.remove_container(container.name, force=True)
            container = None
        stage = self._stage_for(adapter, spec, container)
        if container is not None and self._config_drifted(adapter, spec, container, stage):
            # A container keeps the template it was built with, so without this a
            # hardening change never reaches existing worlds. The world lives on
            # a bind mount; replacing the container costs only a restart.
            log.info(
                "recreating container: template changed",
                extra={"subdomain": spec.subdomain},
            )
            self.client.api.remove_container(container.name, force=True)
            container = None
        if guard_enabled():
            # A world cannot start without a guard that has proven its firewall.
            guard, guard_changed = self._ensure_guard(spec, stage)
            if container is not None:
                mode = (container.attrs.get("HostConfig") or {}).get("NetworkMode", "")
                if mode != f"container:{guard.id}":
                    # Docker records the guard by id, so a world that joined a
                    # replaced guard can never start again.
                    log.info(
                        "recreating container: its egress guard was replaced",
                        extra={"subdomain": spec.subdomain},
                    )
                    self.client.api.remove_container(container.name, force=True)
                    container = None
                elif guard_changed and self._run_state(container) is RunState.RUNNING:
                    # Its old namespace has no network now.
                    self.client.api.stop(container.name, timeout=self._stop_timeout_for(container))
        else:
            # A leftover guard would hold the world's name on the network.
            self._remove_guard(spec.subdomain)
        if container is None:
            self._ensure_name_available(spec.subdomain)
            self._ensure_image(adapter.image)
            log.info("creating container", extra={"subdomain": spec.subdomain, "stage": stage})
            container = self._create_container(adapter, spec, stage)
        container.reload()
        if self._run_state(container) is not RunState.RUNNING:
            log.info("starting container", extra={"subdomain": spec.subdomain})
            # By name: the socket proxy only allows mutating game-world-*
            # containers by name.
            self.client.api.start(container.name)
            container.reload()
        return self._status(container, spec.subdomain)

    def stop(self, subdomain: str, graceful: bool = True) -> None:
        container = self._get_container(subdomain)
        if container is None:
            self._remove_guard(subdomain)
            return
        timeout = self._graceful_stop_timeout(container) if graceful else 0
        log.info(
            "stopping+removing container (OFF)",
            extra={"subdomain": subdomain, "timeout": timeout},
        )
        try:
            self.client.api.stop(container.name, timeout=timeout)
        finally:
            self.client.api.remove_container(container.name, force=True)
            self._remove_guard(subdomain)

    def sleep(self, subdomain: str, graceful: bool = True) -> None:
        """Stop the container but keep it: an exited container is what tells
        the routers to wake it on the next connection."""
        container = self._get_container(subdomain)
        if container is None:
            return
        timeout = self._graceful_stop_timeout(container) if graceful else 0
        log.info("stopping container (SLEEP)", extra={"subdomain": subdomain, "timeout": timeout})
        self.client.api.stop(container.name, timeout=timeout)
        # After the world, which may still use its admin API while saving.
        self._stop_guard(subdomain)

    def logs(self, subdomain: str, tail: int = 400) -> str:
        """The world container's log tail."""
        container = self._get_container(subdomain)
        if container is None:
            return ""
        container.reload()
        started = _started_epoch(container)
        kwargs = {"since": started} if started is not None else {}
        raw = self.client.api.logs(container.name, tail=tail, stdout=True, stderr=True, **kwargs)
        return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")

    def _graceful_stop_timeout(self, container) -> int:  # type: ignore[no-untyped-def]
        """Seconds to allow for a graceful shutdown before SIGKILL: the game's
        own, or none while the server is still booting and has nothing to save."""
        try:
            container.reload()
            health = container.attrs.get("State", {}).get("Health", {}).get("Status")
        except Exception:  # noqa: BLE001 - best effort; fall back to the full window
            health = None
        if health == "starting":
            return 0
        return self._stop_timeout_for(container)

    def _stop_timeout_for(self, container) -> int:  # type: ignore[no-untyped-def]
        """The game's stop timeout, from the container's label (stop paths have
        only a subdomain). The platform default if the label is unusable."""
        label = (container.labels or {}).get(f"{self._s.label_namespace}.game")
        try:
            return adapter_for(label or "").stop_timeout_seconds
        except (ValueError, KeyError, TypeError):
            log.warning(
                "container has no usable game label; using default stop timeout",
                extra={"container": getattr(container, "name", "?"), "label": label},
            )
            return self._s.stop_timeout_seconds

    def delete(self, subdomain: str) -> None:
        """Kill and remove the container; the caller removes the data.

        Not graceful: waiting for a save of data about to be deleted made deletes
        time out at the proxy.
        """
        container = self._get_container(subdomain)
        if container is not None:
            log.info("removing container (DELETE)", extra={"subdomain": subdomain})
            self.client.api.remove_container(container.name, force=True)
        # A guard can outlive a world container something else removed.
        self._remove_guard(subdomain)

    def inspect(self, subdomain: str) -> ContainerStatus:
        container = self._get_container(subdomain)
        if container is None:
            return ContainerStatus(subdomain=subdomain, world_id=None, run_state=RunState.ABSENT)
        container.reload()
        return self._status(container, subdomain)

    def list_managed(self) -> list[ContainerStatus]:
        containers = self.client.containers.list(
            all=True, filters={"label": f"{self._s.label_namespace}=true"}
        )
        return [self._status(c) for c in containers]


class OrchestratorUnavailable(RuntimeError):
    """The world manager could not be reached or refused the request."""


class RemoteOrchestrator(Orchestrator):
    """Lifecycle over HTTP to the world manager, for processes without Docker.

    Docker access is root on the host, and the api is the process most exposed
    to the internet. So it asks the world manager, which offers only these
    operations on a subdomain or a spec: a compromised api can ask for a
    well-formed world, never for a container with ``/`` mounted.
    """

    def __init__(self, base_url: str, secret: str, timeout: float = 360.0) -> None:
        self._base = base_url.rstrip("/")
        self._secret = secret
        # Longer than any game's graceful stop window.
        self._timeout = timeout

    def _call(self, method: str, path: str, payload: dict | None = None) -> Any:
        url = f"{self._base}{path}"
        try:
            resp = httpx2.request(
                method,
                url,
                json=payload,
                headers={"X-World-Manager-Secret": self._secret},
                timeout=self._timeout,
            )
        except httpx2.HTTPError as exc:
            raise OrchestratorUnavailable(f"world manager unreachable: {exc}") from exc

        if resp.status_code == 409:
            raise ContainerNameConflict(resp.json().get("detail", "name conflict"))
        if resp.status_code == 503:
            raise WorldImageUnavailable(resp.json().get("detail", "image unavailable"))
        if resp.status_code == 422 and isinstance(detail := resp.json().get("detail"), str):
            raise InstallIncomplete(detail)
        if resp.status_code >= 400:
            raise OrchestratorUnavailable(
                f"world manager returned {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json() if resp.content else None

    @staticmethod
    def _status(raw: dict) -> ContainerStatus:
        return ContainerStatus(
            subdomain=raw["subdomain"],
            world_id=raw.get("world_id"),
            run_state=RunState(raw["run_state"]),
            container_id=raw.get("container_id"),
            labels=raw.get("labels") or {},
            exit_code=raw.get("exit_code"),
            finished_at=raw.get("finished_at"),
            oom_killed=bool(raw.get("oom_killed", False)),
            stage=raw.get("stage") or PLAY,
            install_finished=bool(raw.get("install_finished", False)),
        )

    def start(self, spec: WorldSpec) -> ContainerStatus:
        payload = asdict(spec)
        payload["game"] = spec.game
        return self._status(self._call("POST", "/worlds/start", payload))

    def stop(self, subdomain: str, graceful: bool = True) -> None:
        self._call("POST", f"/worlds/{subdomain}/stop" + ("" if graceful else "?graceful=false"))

    def sleep(self, subdomain: str, graceful: bool = True) -> None:
        self._call("POST", f"/worlds/{subdomain}/sleep" + ("" if graceful else "?graceful=false"))

    def logs(self, subdomain: str, tail: int = 400) -> str:
        out = self._call("GET", f"/worlds/{subdomain}/logs?tail={int(tail)}")
        return str((out or {}).get("logs", ""))

    def delete(self, subdomain: str) -> None:
        self._call("DELETE", f"/worlds/{subdomain}")

    def inspect(self, subdomain: str) -> ContainerStatus:
        return self._status(self._call("GET", f"/worlds/{subdomain}"))

    def list_managed(self) -> list[ContainerStatus]:
        return [self._status(r) for r in self._call("GET", "/worlds")]


def make_orchestrator() -> Orchestrator:
    """Remote when a world manager is configured, direct Docker otherwise
    (in the world manager itself)."""
    s = get_settings()
    if s.world_manager_url:
        s.require("world_manager_secret")
        return RemoteOrchestrator(s.world_manager_url, s.world_manager_secret)
    return DockerOrchestrator()
