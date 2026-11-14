"""FastAPI application factory and middleware wiring."""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import itsdangerous
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from starlette.datastructures import MutableHeaders
from starlette.formparsers import MultiPartParser
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..common.config import Settings, get_settings, split_keys
from ..common.games import all_adapters
from ..common.logging import configure_logging, get_logger
from ..common.metrics import scrape_allowed
from . import dev_login
from .dev_login import check_dev_login_safe
from .oauth import init_oauth
from .routers import admin, auth, collaborators, games, internal, worlds

log = get_logger(__name__)


# What each process refuses to start without; the deploy's per-service secret
# handout must match.
API_SECRETS = ("session_secret", "fernet_key", "google_client_id", "google_client_secret")
INTERNAL_API_SECRETS = ("internal_secret", "fernet_key")


def _configure_upload_spooling(s: Settings) -> None:
    """Keep uploaded archives off the container's writable layer.

    Starlette spools multipart bodies to a temp file before the handler copies
    them to staging. Spooling to the staging volume keeps multi-gigabyte imports
    off the overlay filesystem, and a higher threshold keeps small uploads off
    disk entirely. The parsing itself is untouched.
    """
    tmp = os.path.join(s.backup_staging_dir, "upload-tmp")
    try:
        os.makedirs(tmp, exist_ok=True)
    except OSError:
        # The default /tmp still works; not a reason to refuse to start.
        log.warning("could not create upload temp dir; falling back", extra={"path": tmp})
        return
    os.environ["TMPDIR"] = tmp
    tempfile.tempdir = None  # force gettempdir() to re-read TMPDIR
    MultiPartParser.spool_max_size = s.upload_spool_max_bytes


class NoStoreMiddleware:
    """Mark every API response uncacheable unless the route says otherwise.

    A cached failed /auth/me keeps the SPA on the sign-in page after login.
    Pure ASGI so streamed uploads and background tasks pass through untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_with_header)


class RotatingSessionMiddleware(SessionMiddleware):
    """Signs with the current secret and still accepts the old ones, so
    rotating the session secret does not sign everyone out."""

    def __init__(
        self,
        app: ASGIApp,
        secret_key: str,
        old_secrets: list[str],
        session_cookie: str,
        max_age: int,
        https_only: bool,
    ) -> None:
        super().__init__(
            app,
            secret_key,
            session_cookie=session_cookie,
            max_age=max_age,
            same_site="lax",
            https_only=https_only,
        )
        # itsdangerous signs with the last key and verifies with any.
        self.signer = itsdangerous.TimestampSigner([*old_secrets, secret_key])


def create_app() -> FastAPI:
    s = get_settings()
    s.require(*API_SECRETS)
    if s.dev_login:
        check_dev_login_safe(s)
    configure_logging(level=s.log_level, json_output=s.log_json)
    _configure_upload_spooling(s)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        init_oauth()
        log.info("api started")
        yield

    app = FastAPI(
        title="Open Worlds API",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    # Signed session cookie: user id, session version and session id; no secrets.
    app.add_middleware(
        RotatingSessionMiddleware,
        old_secrets=split_keys(s.session_old_secrets),
        secret_key=s.session_secret,
        session_cookie=s.session_cookie_name,
        https_only=s.cookie_secure,
        max_age=s.session_max_age_seconds,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[s.web_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # No docs or OpenAPI schema in any environment, with no flag to get wrong:
    # anything served here is public, and the schema maps the control plane.
    # Set docs_url/openapi_url by hand while developing.
    api = FastAPI(
        title="Open Worlds API (v1)",
        docs_url=None,
        openapi_url=None,
        redoc_url=None,
    )
    api.include_router(auth.router)
    api.include_router(worlds.router)
    api.include_router(collaborators.router)
    api.include_router(admin.router)
    api.include_router(games.router)
    # Routes a game brings, such as Minecraft's content catalog.
    for adapter in all_adapters():
        for game_router in adapter.api_routers():
            api.include_router(game_router)
    if s.dev_login:
        api.include_router(dev_login.router)
        log.warning("DEV LOGIN ENABLED: /api/auth/dev/login signs anyone on loopback in")

    @api.get("/healthz", tags=["meta"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    api.add_middleware(NoStoreMiddleware)
    app.mount("/api", api)

    # Prometheus metrics at /metrics (LAN-only via network policy at the edge).
    Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

    return app


def create_internal_app() -> FastAPI:
    """Only the endpoints on-host infrastructure needs.

    Runs as its own service on the `games` network, so the full API is never
    reachable from world containers.
    """
    s = get_settings()
    s.require(*INTERNAL_API_SECRETS)
    configure_logging(level=s.log_level, json_output=s.log_json)
    app = FastAPI(
        title="Open Worlds Internal",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(internal.router, prefix="/api")

    # So internal-api going down, and the wakes it drops, are visible. Not to
    # the games network it serves, though: see common/metrics.py.
    @app.middleware("http")
    async def metrics_off_the_games_network(request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path == "/metrics" and not scrape_allowed(
            request.client.host if request.client else None, s.games_subnet
        ):
            return PlainTextResponse("forbidden\n", status_code=403)
        return await call_next(request)

    Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

    @app.get("/api/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


# Built on attribute access, since this module defines both ASGI targets and
# each process holds only its own secrets. uvicorn resolves "module:attr" with
# getattr, so each process builds and validates only the app it asked for.
_APP_FACTORIES = {"app": create_app, "internal_app": create_internal_app}


def __getattr__(name: str) -> FastAPI:
    try:
        factory = _APP_FACTORIES[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    built = factory()
    globals()[name] = built  # cache, so repeated access reuses the one instance
    return built
