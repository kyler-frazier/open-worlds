"""The only process that holds the Docker credential.

Anything able to create a container can be root on the host (bind-mount `/`,
`Privileged`), and the socket proxy cannot prevent it: it never reads request
bodies. So the internet-facing api does not hold it. This service exposes a few
typed operations taking a subdomain or a world spec, so a compromised api can
ask only for a well-formed world. A flaw here is still a host compromise, but
the surface is small and on a private network.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Path, Response, status
from prometheus_fastapi_instrumentator import Instrumentator

from ..common.config import get_settings
from ..common.games import adapter_for
from ..common.games.base import InstallIncomplete
from ..common.logging import configure_logging, get_logger
from ..common.orchestrator import (
    ContainerNameConflict,
    ContainerStatus,
    DockerOrchestrator,
    WorldImageUnavailable,
    WorldSpec,
)
from ..common.subdomain import LABEL_PATTERN, is_dns_label

log = get_logger(__name__)

# What the process refuses to start without.
WORLD_MANAGER_SECRETS = ("world_manager_secret",)

# The api validated it at creation; checked again because it becomes a path
# under the worlds dir, and this process is the one that holds Docker.
Subdomain = Path(pattern=LABEL_PATTERN)


def require_secret(x_world_manager_secret: str | None = Header(default=None)) -> None:
    """Shared-secret gate, constant-time, so a world container that reached
    this service still cannot drive it."""
    expected = get_settings().world_manager_secret
    if (
        not expected
        or x_world_manager_secret is None
        or not secrets.compare_digest(x_world_manager_secret, expected)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


def _out(cs: ContainerStatus) -> dict:
    return {
        "subdomain": cs.subdomain,
        "world_id": cs.world_id,
        "run_state": cs.run_state.value,
        "container_id": cs.container_id,
        "labels": cs.labels,
        "exit_code": cs.exit_code,
        "finished_at": cs.finished_at,
        "oom_killed": cs.oom_killed,
        "stage": cs.stage,
        "install_finished": cs.install_finished,
    }


def create_app() -> FastAPI:
    s = get_settings()
    s.require(*WORLD_MANAGER_SECRETS)
    configure_logging(level=s.log_level, json_output=s.log_json)

    app = FastAPI(
        title="Open Worlds World Manager",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    orch = DockerOrchestrator()

    # Liveness needs no secret; it reveals nothing.
    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    worlds = APIRouter(dependencies=[Depends(require_secret)])

    @worlds.post("/worlds/start")
    def start(payload: dict) -> dict:
        # Field by field rather than **payload: WorldSpec is the whole
        # vocabulary a caller is allowed.
        try:
            spec = WorldSpec(
                game=adapter_for(str(payload["game"])).game,
                world_id=str(payload["world_id"]),
                owner_id=str(payload["owner_id"]),
                subdomain=str(payload["subdomain"]),
                display_name=str(payload["display_name"]),
                memory_mb=int(payload["memory_mb"]),
                max_players=int(payload["max_players"]),
                admin_password=str(payload["admin_password"]),
                server_password=str(payload.get("server_password") or ""),
                game_port=payload.get("game_port"),
                # Revalidated: these reach the container's environment.
                settings=adapter_for(str(payload["game"])).validate_settings(
                    dict(payload.get("settings") or {})
                ),
                extras=_str_dict(payload.get("extras")),
                egress_log_only=bool(payload.get("egress_log_only", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f"Bad world spec: {exc}"
            ) from exc
        if not is_dns_label(spec.subdomain):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bad subdomain")

        try:
            return _out(orch.start(spec))
        except ContainerNameConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except WorldImageUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        except InstallIncomplete as exc:
            # The game's install, not the platform: the world's card says why.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc

    @worlds.post("/worlds/{subdomain}/stop", status_code=status.HTTP_204_NO_CONTENT)
    def stop(subdomain: str = Subdomain, graceful: bool = True) -> Response:
        orch.stop(subdomain, graceful=graceful)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @worlds.post("/worlds/{subdomain}/sleep", status_code=status.HTTP_204_NO_CONTENT)
    def sleep(subdomain: str = Subdomain, graceful: bool = True) -> Response:
        orch.sleep(subdomain, graceful=graceful)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @worlds.get("/worlds/{subdomain}/logs")
    def logs(subdomain: str = Subdomain, tail: int = 400) -> dict:
        # Capped: this is for reading why a server failed, not for streaming.
        return {"logs": orch.logs(subdomain, tail=max(1, min(tail, 2000)))}

    @worlds.delete("/worlds/{subdomain}", status_code=status.HTTP_204_NO_CONTENT)
    def delete(subdomain: str = Subdomain) -> Response:
        orch.delete(subdomain)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @worlds.get("/worlds/{subdomain}")
    def inspect(subdomain: str = Subdomain) -> dict:
        return _out(orch.inspect(subdomain))

    @worlds.get("/worlds")
    def list_managed() -> list[dict]:
        return [_out(cs) for cs in orch.list_managed()]

    app.include_router(worlds)
    # Scraped for the "world manager down" alert. Unauthenticated, as healthz.
    Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
    log.info("world manager started")
    return app


def __getattr__(name: str) -> FastAPI:
    # Built on access, as in the API, so each process validates only its own
    # secrets.
    if name != "app":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    built = create_app()
    globals()[name] = built
    return built


def _str_dict(raw: object) -> dict[str, str | bool]:
    """Spec extras: flat strings and flags only, never nested structures."""
    if not isinstance(raw, dict):
        return {}
    return {str(k): v if isinstance(v, bool) else str(v) for k, v in raw.items()}
