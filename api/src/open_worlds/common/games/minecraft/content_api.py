"""Curated content routes.

- Admin catalog CRUD under ``/api/admin/content`` (add by Modrinth slug or a
  pinned URL+hash, list/toggle/remove).
- Per-world content under ``/api/worlds/{id}/content`` (installed, available
  for the world's loader and version, apply/remove).
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ....api.deps import get_principal, load_world_for, require_admin, require_csrf
from ....api.schemas import (
    AvailableContentOut,
    ContentApplyIn,
    ContentCatalogCreate,
    ContentCatalogOut,
    ContentCatalogUpdate,
    JobAcceptedOut,
    JobStatusOut,
    MessageOut,
    WorldContentOut,
)
from ...authz import AccessLevel, Principal
from ...db import get_db
from ...logging import get_logger
from ...models import (
    ContentCatalog,
    ContentKind,
    ContentSource,
    Job,
    JobStatus,
    JobType,
    User,
    World,
    WorldContent,
    WorldState,
)
from .. import adapter_for
from .content import (
    ContentError,
    ModrinthClient,
    is_compatible,
    validate_project_kind,
)
from .settings import ServerType, settings_of

admin_router = APIRouter(
    prefix="/admin/content", tags=["admin", "content"], dependencies=[Depends(require_csrf)]
)
log = get_logger(__name__)


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def _to_out(entry: ContentCatalog) -> ContentCatalogOut:
    return ContentCatalogOut(
        id=entry.id,
        kind=entry.kind,
        source=entry.source,
        project_slug=entry.project_slug,
        name=entry.name,
        icon_url=entry.icon_url,
        enabled=entry.enabled,
        loaders=_json_list(entry.cached_loaders),
        game_versions=_json_list(entry.cached_game_versions),
        created_at=entry.created_at,
    )


@admin_router.get("", response_model=list[ContentCatalogOut])
def list_catalog(
    kind: ContentKind | None = None,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[ContentCatalogOut]:
    stmt = select(ContentCatalog).order_by(ContentCatalog.kind, ContentCatalog.name)
    if kind is not None:
        stmt = stmt.where(ContentCatalog.kind == kind)
    return [_to_out(e) for e in db.execute(stmt).scalars()]


@admin_router.post("", response_model=ContentCatalogOut, status_code=status.HTTP_201_CREATED)
def add_catalog_entry(
    payload: ContentCatalogCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> ContentCatalogOut:
    if payload.source is ContentSource.MODRINTH:
        entry = _build_modrinth_entry(payload)
    else:
        entry = _build_pinned_entry(payload)

    db.add(entry)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That project is already in the catalog for this content kind.",
        ) from None
    db.refresh(entry)
    log.info(
        "content catalog entry added",
        extra={"by": str(admin.id), "kind": entry.kind.value, "slug": entry.project_slug},
    )
    return _to_out(entry)


def _build_modrinth_entry(payload: ContentCatalogCreate) -> ContentCatalog:
    slug = payload.project_slug.strip()
    try:
        project = ModrinthClient().get_project(slug)
        validate_project_kind(project, payload.kind)
    except ContentError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return ContentCatalog(
        kind=payload.kind,
        source=ContentSource.MODRINTH,
        project_slug=project.slug or slug,
        modrinth_project_id=project.id or None,
        name=project.title,
        icon_url=project.icon_url,
        cached_loaders=json.dumps(project.loaders),
        cached_game_versions=json.dumps(project.game_versions),
        cached_client_side=project.client_side,
        cached_server_side=project.server_side,
    )


def _build_pinned_entry(payload: ContentCatalogCreate) -> ContentCatalog:
    missing = [
        f
        for f, v in (
            ("name", payload.name),
            ("loaders", payload.loaders),
            ("game_versions", payload.game_versions),
            ("download_url", payload.download_url),
            ("sha512", payload.sha512),
        )
        if not v
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Pinned entries require: {', '.join(missing)}.",
        )
    return ContentCatalog(
        kind=payload.kind,
        source=ContentSource.PINNED,
        project_slug=payload.project_slug.strip(),
        name=payload.name or payload.project_slug,
        cached_loaders=json.dumps(payload.loaders or []),
        cached_game_versions=json.dumps(payload.game_versions or []),
        pinned_download_url=payload.download_url,
        pinned_sha512=(payload.sha512 or "").lower() or None,
        pinned_sha1=(payload.sha1 or "").lower() or None,
    )


@admin_router.patch("/{entry_id}", response_model=ContentCatalogOut)
def update_catalog_entry(
    entry_id: str,
    payload: ContentCatalogUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> ContentCatalogOut:
    entry = db.get(ContentCatalog, uuid.UUID(entry_id))
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Catalog entry not found")
    if payload.enabled is not None:
        entry.enabled = payload.enabled
    db.commit()
    db.refresh(entry)
    log.info("content catalog entry updated", extra={"by": str(admin.id), "id": entry_id})
    return _to_out(entry)


@admin_router.delete("/{entry_id}", response_model=MessageOut)
def delete_catalog_entry(
    entry_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> MessageOut:
    entry = db.get(ContentCatalog, uuid.UUID(entry_id))
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Catalog entry not found")
    db.delete(entry)
    db.commit()
    log.info("content catalog entry removed", extra={"by": str(admin.id), "id": entry_id})
    return MessageOut(message="Catalog entry removed")


# --- Per-world content ---------------------------------------------------------

world_router = APIRouter(
    prefix="/worlds", tags=["worlds", "content"], dependencies=[Depends(require_csrf)]
)


def _load_world(world_id: str, principal: Principal, db: Session, *, need: AccessLevel) -> World:
    """Authorize via the shared loader, then apply this router's own gate."""
    world, _ = load_world_for(world_id, principal, db, need=need)
    # A curated catalog needs a trusted registry to curate from. Games without
    # one (Palworld has no Modrinth equivalent) install nothing at all, so these
    # routes must refuse rather than half-work.
    adapter = adapter_for(world.game)
    if not adapter.supports_content_catalog:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{adapter.display_name} worlds have no installable content catalog.",
        )
    return world


def _require_off(world: World) -> None:
    if world.state in (WorldState.RUNNING, WorldState.SLEEPING, WorldState.STOPPING):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Turn the world off before changing its content.",
        )


#: Shared with the world PATCH guard, which needs the same answer to decide
#: whether a server-type switch would strand installed content.
_is_compatible = is_compatible


@world_router.get("/{world_id}/content", response_model=list[WorldContentOut])
def list_world_content(
    world_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[WorldContentOut]:
    world = _load_world(world_id, principal, db, need=AccessLevel.OPERATOR)
    rows = db.execute(select(WorldContent).where(WorldContent.world_id == world.id)).scalars()
    out: list[WorldContentOut] = []
    for wc in rows:
        cat = wc.catalog
        out.append(
            WorldContentOut(
                id=wc.id,
                catalog_id=wc.catalog_id,
                kind=wc.kind,
                name=cat.name if cat else "(removed)",
                icon_url=cat.icon_url if cat else None,
                filename=wc.filename,
                version_id=wc.version_id,
                installed_at=wc.installed_at,
            )
        )
    return out


@world_router.get("/{world_id}/content/available", response_model=list[AvailableContentOut])
def available_content(
    world_id: str,
    kind: ContentKind | None = None,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[AvailableContentOut]:
    world = _load_world(world_id, principal, db, need=AccessLevel.OPERATOR)
    # catalog_id -> enforce flag, for everything installed on this world.
    installed = dict(
        db.execute(
            select(WorldContent.catalog_id, WorldContent.enforce).where(
                WorldContent.world_id == world.id
            )
        ).all()
    )
    stmt = select(ContentCatalog).where(ContentCatalog.enabled.is_(True))
    if kind is not None:
        stmt = stmt.where(ContentCatalog.kind == kind)
    stmt = stmt.order_by(ContentCatalog.kind, ContentCatalog.name)
    out: list[AvailableContentOut] = []
    for e in db.execute(stmt).scalars():
        if not _is_compatible(e, settings_of(world).server_type, settings_of(world).version):
            continue
        out.append(
            AvailableContentOut(
                catalog_id=e.id,
                kind=e.kind,
                source=e.source,
                name=e.name,
                icon_url=e.icon_url,
                installed=e.id in installed,
                server_side_only=_server_side_only(e),
                enforce=(
                    bool(installed[e.id])
                    if e.kind is ContentKind.RESOURCEPACK and e.id in installed
                    else None
                ),
            )
        )
    return out


def _server_side_only(entry: ContentCatalog) -> bool | None:
    """Whether a mod needs no client-side install (client side optional/unsupported).

    Only meaningful for mods; ``None`` for other kinds or when Modrinth didn't
    report a client side (e.g. PINNED entries).
    """
    if entry.kind is not ContentKind.MOD:
        return None
    cs = (entry.cached_client_side or "").lower()
    if not cs or cs == "unknown":
        return None
    return cs in ("unsupported", "optional")


def _enqueue_content_job(db: Session, world: World, action: str, catalog_id: str, **extra) -> Job:
    payload = {"action": action, "catalog_id": catalog_id, **extra}
    job = Job(
        type=JobType.CONTENT,
        status=JobStatus.PENDING,
        world_id=world.id,
        payload=json.dumps(payload),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


@world_router.post(
    "/{world_id}/content", response_model=JobAcceptedOut, status_code=status.HTTP_202_ACCEPTED
)
def apply_content(
    world_id: str,
    payload: ContentApplyIn,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> JobAcceptedOut:
    world = _load_world(world_id, principal, db, need=AccessLevel.MANAGER)
    _require_off(world)
    entry = db.get(ContentCatalog, payload.catalog_id)
    if entry is None or not entry.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Catalog entry not found")
    if not _is_compatible(entry, settings_of(world).server_type, settings_of(world).version):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This entry has no compatible version for the world's type/version.",
        )
    existing = db.execute(
        select(WorldContent).where(
            WorldContent.world_id == world.id, WorldContent.catalog_id == entry.id
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Already installed on this world."
        )
    job = _enqueue_content_job(db, world, "apply", str(entry.id), enforce=bool(payload.enforce))
    log.info(
        "content apply queued",
        extra={"world_id": str(world.id), "catalog_id": str(entry.id), "job_id": str(job.id)},
    )
    return JobAcceptedOut(message="Content install queued. It will apply shortly.", job_id=job.id)


@world_router.delete("/{world_id}/content/{catalog_id}", response_model=JobAcceptedOut)
def remove_content(
    world_id: str,
    catalog_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> JobAcceptedOut:
    world = _load_world(world_id, principal, db, need=AccessLevel.MANAGER)
    _require_off(world)
    try:
        cid = uuid.UUID(catalog_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not installed") from exc
    wc = db.execute(
        select(WorldContent).where(
            WorldContent.world_id == world.id, WorldContent.catalog_id == cid
        )
    ).scalar_one_or_none()
    if wc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not installed")
    job = _enqueue_content_job(db, world, "remove", str(cid))
    log.info(
        "content remove queued",
        extra={"world_id": str(world.id), "catalog_id": catalog_id, "job_id": str(job.id)},
    )
    return JobAcceptedOut(message="Content removal queued.", job_id=job.id)


@world_router.get("/{world_id}/jobs/{job_id}", response_model=JobStatusOut)
def get_job_status(
    world_id: str,
    job_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> JobStatusOut:
    """Follow a queued job to completion.

    Scoped under the world so a job id is not a bearer token. OPERATOR, to
    match the content reads it reports on.
    """
    world = _load_world(world_id, principal, db, need=AccessLevel.OPERATOR)
    try:
        jid = uuid.UUID(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found") from exc
    job = db.get(Job, jid)
    # Another world's job reads as absent.
    if job is None or job.world_id != world.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return JobStatusOut(
        id=job.id,
        type=job.type,
        status=job.status,
        error=job.error,
        attempts=job.attempts,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def stranded_by(db: Session, world: World, server_type: ServerType, version: str) -> list[str]:
    """Names of installed content a switch to this loader/version would strand.

    The jars stay on disk but do nothing on the other loader. The caller
    refuses and names them, since only the owner knows which side they meant.
    """
    if settings_of(world).server_type is server_type:
        return []
    installed = db.execute(
        select(ContentCatalog)
        .join(WorldContent, WorldContent.catalog_id == ContentCatalog.id)
        .where(WorldContent.world_id == world.id)
    ).scalars()
    return [e.name for e in installed if not is_compatible(e, server_type, version)]


def resource_pack_for(db: Session, world: World) -> WorldContent | None:
    """The world's installed resource pack, if it has one."""
    return db.execute(
        select(WorldContent).where(
            WorldContent.world_id == world.id,
            WorldContent.kind == ContentKind.RESOURCEPACK,
        )
    ).scalar_one_or_none()
