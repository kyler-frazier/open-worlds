"""World routes: CRUD, lifecycle, icons, backups and imports.

Container work goes through the orchestrator; backups and imports are queued
for the worker.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...common.authz import AccessLevel, Principal, at_least, is_world_owner, resolve_access
from ...common.capacity import CapacityExceeded, ensure_capacity_for
from ...common.config import Settings, get_settings
from ...common.crypto import decrypt, encrypt
from ...common.db import get_db
from ...common.drive import (
    DRIVE_REVOKED_MESSAGE,
    DriveClient,
    DriveFileTooLarge,
    is_not_found,
    unlink_if_revoked,
)
from ...common.game_availability import enabled_games
from ...common.games import (
    GameAdapter,
    GameSettingsConflict,
    GameSettingsError,
    UnknownGame,
    adapter_for,
    all_adapters,
    game_ids,
)
from ...common.logging import get_logger
from ...common.metrics import world_actions_total
from ...common.models import (
    Backup,
    BackupKind,
    BackupStatus,
    Job,
    JobStatus,
    JobType,
    Role,
    StagedUpload,
    User,
    World,
    WorldCollaborator,
    WorldState,
)
from ...common.orchestrator import (
    ContainerNameConflict,
    Orchestrator,
    RunState,
    WorldImageUnavailable,
    guard_enabled,
)
from ...common.ports import NoPortAvailable, allocate_port, release_port
from ...common.readiness import readiness_for
from ...common.runtime_config import get_runtime_config
from ...common.specs import spec_for
from ...common.startup_errors import summary
from ...common.stopping import begin_stop, finish_stop, finish_stop_within
from ...common.subdomain import InvalidSubdomain, validate_subdomain
from ...common.upload_inspect import (
    archive_icon_changes,
    inspect_archive,
    prunable_report,
    worlds_in_archive,
)
from ...common.world_icon import (
    ICON_MAX_BYTES,
    InvalidIcon,
    clear_icons,
    icon_files,
    resolve_icon,
    write_icon,
)
from ...common.world_status import TRANSIENT_STATES, apply_state, reconciled_state
from ..deps import get_current_user, get_orchestrator, get_principal, load_world_for, require_csrf
from ..schemas import (
    BackupCreate,
    BackupOut,
    DriveBackupOut,
    GameOut,
    IconChangeOut,
    IconFileOut,
    ImportCommit,
    ImportFromDrive,
    LimitsOut,
    MessageOut,
    RestorableBackupOut,
    StagedUploadOut,
    WorldCreate,
    WorldOut,
    WorldUpdate,
)

router = APIRouter(prefix="/worlds", tags=["worlds"], dependencies=[Depends(require_csrf)])
log = get_logger(__name__)


def _ensure_world_storage(subdomain: str) -> str:
    """Create ``worlds/<sub>/data`` and lock the world dir to 0700: game images
    write the admin password into it. Returns the data dir."""
    s = get_settings()
    world_root = os.path.join(s.worlds_dir, subdomain)
    data_dir = os.path.join(world_root, "data")
    os.makedirs(data_dir, exist_ok=True)
    try:
        os.chmod(world_root, 0o700)
    except OSError:
        log.warning("could not tighten world dir permissions", extra={"subdomain": subdomain})
    return data_dir


def _mark_started(world: World) -> None:
    """Move a world to RUNNING; ``apply_state`` resets its readiness clock."""
    apply_state(world, WorldState.RUNNING)


def _check_memory(memory_mb: int, s: Settings) -> None:
    """The operator's memory bounds, for create and update. Under the floor a
    world is too starved to boot."""
    if memory_mb > s.max_memory_mb:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Memory must be at most {s.max_memory_mb} MB on this server.",
        )
    if memory_mb < s.min_memory_mb:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Memory must be at least {s.min_memory_mb} MB; a world with "
            f"less will not start.",
        )


def _default_memory_for(adapter: GameAdapter, s: Settings) -> int:
    """Memory a new world gets when its owner named none: the operator's
    ``PLATFORM_DEFAULT_MEMORY_MB`` if set, else the game's.

    Clamped rather than refused: nobody chose this number, so the owner could
    not act on a 400.
    """
    chosen = s.default_memory_mb if s.default_memory_mb is not None else adapter.default_memory_mb
    return max(s.min_memory_mb, min(chosen, s.max_memory_mb))


def _discard_world_storage(world_root: str, subdomain: str) -> None:
    """Remove the storage prepared for a world whose creation then failed, so
    the next create need not wipe an orphan. Best-effort."""
    if not os.path.exists(world_root):
        return
    try:
        shutil.rmtree(world_root)
    except OSError:
        log.warning(
            "could not remove world dir after failed create", extra={"subdomain": subdomain}
        )


def world_to_out(w: World, access_level: AccessLevel) -> WorldOut:
    """Build the API representation of a world. The only place a ``WorldOut``
    is built, so no route can return a stale copy."""
    s = get_settings()
    trusted = at_least(access_level, AccessLevel.MANAGER)
    return WorldOut(
        id=w.id,
        subdomain=w.subdomain,
        game=w.game,
        display_name=w.display_name,
        settings=w.game_settings or {},
        reported_version=w.reported_version,
        game_port=w.game_port,
        # Whoever may change the join password may read it: managers, the
        # owner and admins. An operator only starts and stops.
        server_password=(
            decrypt(w.server_password_enc) if w.server_password_enc and trusted else ""
        ),
        memory_mb=w.memory_mb,
        max_players=w.max_players,
        state=w.state,
        player_count=w.player_count,
        players_updated_at=w.players_updated_at,
        # Per game: a Palworld world installing 4.5 GB is busy, not stuck.
        readiness=readiness_for(w, adapter_for(w.game).readiness_timeout_seconds),
        auto_backup_enabled=w.auto_backup_enabled,
        auto_backup_interval=w.auto_backup_interval,
        auto_backup_max=w.auto_backup_max,
        egress_guarded=guard_enabled(),
        egress_log_only=bool(w.egress_log_only),
        # Flags a world left open where blocking is the default.
        egress_open_exception=bool(w.egress_log_only) and get_settings().egress_blocking_default,
        # The raw log tail can carry paths and stack traces; operators get
        # the summary below.
        last_error=w.last_error if trusted else None,
        last_error_summary=(
            summary(w.last_error, adapter_for(w.game).fatal_log_patterns) if w.last_error else None
        ),
        last_error_at=w.last_error_at,
        created_at=w.created_at,
        last_active_at=w.last_active_at,
        started_at=w.started_at,
        connect_address=adapter_for(w.game).connect_address(
            w.subdomain, s.servers_domain, w.game_port
        ),
        owner_email=(w.owner.email if w.owner else None),
        owner_id=w.owner_id,
        access_level=access_level,
        **_icon_fields(w),
    )


def _icon_fields(w: World) -> dict[str, Any]:
    resolved = resolve_icon(_data_dir_for(w.subdomain), adapter_for(w.game).import_policy)
    if resolved is None:
        return {"icon_version": None, "icon_is_custom": False}
    path, is_custom = resolved
    try:
        version: int | None = int(os.stat(path).st_mtime * 1000)
    except OSError:
        version = None
    return {"icon_version": version, "icon_is_custom": is_custom and version is not None}


def _data_dir_for(subdomain: str) -> str:
    return os.path.join(get_settings().worlds_dir, subdomain, "data")


def _load_owned_world(world_id: str, principal: Principal, db: Session) -> World:
    """Load a world the caller owns; being an admin is not enough.

    For importing into a world and restoring from Drive. 403, not 404: an admin
    can already see the world.
    """
    world, _ = load_world_for(world_id, principal, db, need=AccessLevel.OWNER)
    if not is_world_owner(principal, str(world.owner_id)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the world's owner can import content into it.",
        )
    return world


@router.get("", response_model=list[WorldOut])
def list_worlds(
    user: User = Depends(get_current_user),
    orch: Orchestrator = Depends(get_orchestrator),
    db: Session = Depends(get_db),
) -> list[WorldOut]:
    # A subquery, not a join: a join returns a world twice for someone who is
    # both its owner and a collaborator.
    shared = select(WorldCollaborator.world_id).where(WorldCollaborator.user_id == user.id)
    rows = list(
        db.execute(
            select(World)
            .where(or_(World.owner_id == user.id, World.id.in_(shared)))
            .order_by(World.created_at)
        ).scalars()
    )
    # So the dashboard shows routers' auto-sleeps.
    reconcile_rows(rows, orch, db)
    # One query for every grant: the dashboard polls this.
    levels = {
        wid: lvl
        for wid, lvl in db.execute(
            select(WorldCollaborator.world_id, WorldCollaborator.level).where(
                WorldCollaborator.user_id == user.id,
                WorldCollaborator.world_id.in_([w.id for w in rows]),
            )
        )
    }
    principal = Principal(
        user_id=str(user.id), google_sub=user.google_sub, is_admin=user.role is Role.ADMIN
    )
    return [
        world_to_out(
            w,
            resolve_access(
                principal,
                str(w.owner_id),
                levels[w.id].value if w.id in levels else None,
            ),
        )
        for w in rows
    ]


def reconcile_rows(rows: list[World], orch: Orchestrator, db: Session) -> int:
    """Reconcile many worlds against one container listing; returns how many
    changed. A listing failure leaves rows as they were: a Docker hiccup must
    never fail a read."""
    if not rows:
        return 0
    try:
        managed = {cs.subdomain: cs for cs in orch.list_managed()}
    except Exception as exc:
        log.warning("list_managed failed; serving stored state", extra={"error": str(exc)})
        return 0
    changed = 0
    for w in rows:
        if w.state in TRANSIENT_STATES:
            continue
        cs = managed.get(w.subdomain)
        mapped = reconciled_state(
            w.state,
            cs.run_state if cs else RunState.ABSENT,
            cs.exit_code if cs else None,
            cs.oom_killed if cs else False,
        )
        if apply_state(w, mapped):
            changed += 1
    if changed:
        db.commit()
    return changed


@router.get("/limits", response_model=LimitsOut)
def get_limits(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> LimitsOut:
    """Caps plus per-game capabilities, so the UI builds the right create form."""
    s = get_settings()
    return LimitsOut(
        max_worlds_per_user=s.max_worlds_per_user,
        max_upload_bytes=get_runtime_config().max_upload_bytes,
        max_memory_mb=s.max_memory_mb,
        min_memory_mb=s.min_memory_mb,
        default_max_players=s.default_max_players,
        max_players_cap=s.max_players_cap,
        servers_domain=s.servers_domain,
        egress_override_actors=s.egress_override_actors,
        egress_blocking_default=s.egress_blocking_default,
        games=games_out(db, s),
    )


def _art_url(adapter: GameAdapter) -> str | None:
    path = adapter.art_path()
    if path is None:
        return None
    return f"/api/games/{adapter.game.lower()}/art?v={int(os.stat(path).st_mtime)}"


def games_out(db: Session, s: Settings) -> list[GameOut]:
    """What each game this platform offers supports, generated from its adapter."""
    used_ports = {p for p in db.execute(select(World.game_port)).scalars() if p is not None}
    offered = enabled_games(db)
    games = []
    for adapter in all_adapters():
        if adapter.game not in offered:
            continue
        games.append(
            GameOut(
                game=adapter.game,
                display_name=adapter.display_name,
                supports_version_pinning=adapter.supports_version_pinning,
                supports_content_catalog=adapter.supports_content_catalog,
                supports_server_password=adapter.supports_server_password,
                allocates_port=adapter.allocates_port,
                settings_schema=adapter.settings_schema(),
                spec_fields=list(adapter.spec_fields),
                art_url=_art_url(adapter),
                default_memory_mb=_default_memory_for(adapter, s),
                # Only a port-allocating game is capped by the range.
                ports_available=(
                    s.max_worlds - len(used_ports) if adapter.allocates_port else None
                ),
            )
        )
    return games


@router.post("", response_model=WorldOut, status_code=status.HTTP_201_CREATED)
def create_world(
    payload: WorldCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WorldOut:
    s = get_settings()
    try:
        subdomain = validate_subdomain(payload.subdomain)
    except InvalidSubdomain as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    try:
        adapter = adapter_for(payload.game)
    except UnknownGame as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if adapter.game not in enabled_games(db):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{adapter.display_name} isn't offered on this platform.",
        )

    # The adapter fills in defaults and refuses keys the game does not have.
    try:
        game_settings = adapter.new_settings(payload.settings)
    except GameSettingsError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # Refused rather than ignored, or the owner would believe the world is
    # protected.
    server_password = payload.server_password.strip()
    if server_password and not adapter.supports_server_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{adapter.display_name} servers have no join password.",
        )

    # An owner's figure is checked; the default is clamped.
    if payload.memory_mb:
        memory_mb = payload.memory_mb
        _check_memory(memory_mb, s)
    else:
        memory_mb = _default_memory_for(adapter, s)

    max_players = payload.max_players or s.default_max_players
    if max_players > s.max_players_cap:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Player cap exceeds the {s.max_players_cap} limit",
        )

    owned = db.execute(select(World.id).where(World.owner_id == user.id)).all()
    if len(owned) >= s.max_worlds_per_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"World limit reached ({s.max_worlds_per_user})",
        )

    taken = db.execute(select(World).where(World.subdomain == subdomain)).scalar_one_or_none()
    if taken is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Subdomain already in use")

    # The subdomain is free, so a directory here is a leftover from an
    # incomplete delete. Wipe it so a new owner never inherits old data, or
    # refuse.
    world_root = os.path.join(s.worlds_dir, subdomain)
    if os.path.exists(world_root):
        try:
            shutil.rmtree(world_root)
        except OSError as exc:
            log.exception("could not clear leftover world dir", extra={"subdomain": subdomain})
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not prepare world storage; please try again.",
            ) from exc
    # Before the row, so a world record always has storage.
    _ensure_world_storage(subdomain)

    # Allocation can race a concurrent create; the unique constraint on
    # game_port decides, and we retry.
    attempts = _PORT_ALLOCATION_ATTEMPTS if adapter.allocates_port else 1
    for attempt in range(attempts):
        game_port: int | None = None
        if adapter.allocates_port:
            try:
                # A recreated world reclaims its old port.
                game_port = allocate_port(db, s, subdomain, user.id)
            except NoPortAvailable as exc:
                _discard_world_storage(world_root, subdomain)
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        world = World(
            owner_id=user.id,
            game=adapter.game,
            subdomain=subdomain,
            display_name=payload.display_name,
            game_settings=game_settings,
            game_port=game_port,
            memory_mb=memory_mb,
            max_players=max_players,
            state=WorldState.OFF,
            admin_password_enc=encrypt(secrets.token_urlsafe(s.admin_password_bytes)),
            server_password_enc=encrypt(server_password) if server_password else None,
            # Afterwards only an override changes it.
            egress_log_only=not s.egress_blocking_default,
        )
        db.add(world)
        try:
            db.commit()
            break
        except IntegrityError:
            db.rollback()
            log.info(
                "world insert conflicted; retrying",
                extra={"subdomain": subdomain, "port": game_port, "attempt": attempt + 1},
            )
    else:
        # Without a port, the conflict was the subdomain.
        _discard_world_storage(world_root, subdomain)
        detail = (
            "Could not reserve a game port; please try again."
            if adapter.allocates_port
            else "That subdomain was just taken; please choose another."
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
    db.refresh(world)
    log.info("world created", extra={"world_id": str(world.id), "subdomain": subdomain})
    world_actions_total.labels(action="create", result="success").inc()
    return world_to_out(world, AccessLevel.OWNER)


@router.get("/restorable-backups", response_model=list[RestorableBackupOut])
def list_restorable_backups(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[RestorableBackupOut]:
    """Successful, Drive-stored backups of the user's worlds, for import."""
    rows = db.execute(
        select(Backup, World)
        .join(World, Backup.world_id == World.id)
        .where(
            World.owner_id == user.id,
            Backup.status == BackupStatus.SUCCESS,
            Backup.drive_file_id.is_not(None),
        )
        .order_by(Backup.created_at.desc())
    ).all()
    return [
        RestorableBackupOut(
            id=b.id,
            world_id=w.id,
            game=w.game,
            world_display_name=w.display_name,
            world_subdomain=w.subdomain,
            kind=b.kind,
            filename=b.filename,
            size_bytes=b.size_bytes,
            created_at=b.created_at,
        )
        for b, w in rows
    ]


@router.get("/drive-backups", response_model=list[DriveBackupOut])
def list_drive_backups(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[DriveBackupOut]:
    """Every archive in the caller's own Drive, including ones we no longer
    track (a backup row is deleted with its world).

    Owner-only by construction: it uses the caller's own token, and
    ``drive.file`` exposes only files this app created in that Drive.
    """
    if not user.drive_refresh_token_enc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Google Drive is not linked"
        )
    try:
        files = DriveClient(decrypt(user.drive_refresh_token_enc)).list_backups(game_ids())
    except Exception as exc:
        if unlink_if_revoked(exc, user):
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Google Drive access was revoked. Link it again to see your backups.",
            ) from exc
        log.warning("could not list Drive backups", extra={"error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach Google Drive. Try again shortly.",
        ) from exc

    tracked = {
        fid
        for (fid,) in db.execute(
            select(Backup.drive_file_id)
            .join(World, Backup.world_id == World.id)
            .where(World.owner_id == user.id, Backup.drive_file_id.is_not(None))
        ).all()
    }
    return [
        DriveBackupOut(
            drive_file_id=f["drive_file_id"],
            filename=f["filename"],
            world_subdomain=f["subdomain"],
            game=f["game"] or None,
            size_bytes=f["size_bytes"],
            modified_at=f["modified_at"],
            known=f["drive_file_id"] in tracked,
        )
        for f in sorted(files, key=lambda f: f.get("modified_at") or "", reverse=True)
    ]


@router.get("/{world_id}", response_model=WorldOut)
def get_world(
    world_id: str,
    principal: Principal = Depends(get_principal),
    orch: Orchestrator = Depends(get_orchestrator),
    db: Session = Depends(get_db),
) -> WorldOut:
    world, level = load_world_for(world_id, principal, db, need=AccessLevel.OPERATOR)
    _reconcile_state(world, orch, db)
    return world_to_out(world, level)


def _reconcile_state(world: World, orch: Orchestrator, db: Session) -> None:
    """Refresh stored state from the live container, unless mid-transition."""
    if world.state in TRANSIENT_STATES:
        return
    try:
        status_ = orch.inspect(world.subdomain)
    except Exception as exc:  # never fail a read because Docker hiccuped
        log.warning("inspect failed", extra={"subdomain": world.subdomain, "error": str(exc)})
        return
    mapped = reconciled_state(world.state, status_.run_state, status_.exit_code, status_.oom_killed)
    if apply_state(world, mapped):
        db.commit()


@router.post("/{world_id}/start", response_model=WorldOut)
def start_world(
    world_id: str,
    principal: Principal = Depends(get_principal),
    orch: Orchestrator = Depends(get_orchestrator),
    db: Session = Depends(get_db),
) -> WorldOut:
    world, level = load_world_for(world_id, principal, db, need=AccessLevel.OPERATOR)
    if world.state is WorldState.STOPPING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This world is still shutting down; try again once it shows Off.",
        )
    if world.game not in enabled_games(db):
        # The game was turned off on this host after the world was made.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{world.game} worlds are not enabled on this host.",
        )
    try:
        ensure_capacity_for(world, db, get_settings())
    except CapacityExceeded as exc:
        world_actions_total.labels(action="start", result="capacity").inc()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    _ensure_world_storage(world.subdomain)
    try:
        orch.start(spec_for(world, db))
    except WorldImageUnavailable as exc:
        # The image pin or the registry, not the world: leave it OFF and say
        # so, or the owner would hunt through their settings.
        world_actions_total.labels(action="start", result="error").inc()
        log.error(
            "start blocked by unavailable game image",
            extra={"world_id": str(world.id), "game": world.game, "detail": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"The {adapter_for(world.game).display_name} server image is "
            "unavailable right now. This is a platform problem, not a problem "
            "with your world — please contact an administrator.",
        ) from exc
    except ContainerNameConflict as exc:
        # Leave the world OFF; the details are for operators, not users.
        world_actions_total.labels(action="start", result="error").inc()
        log.warning(
            "start blocked by container name conflict",
            extra={"world_id": str(world.id), "subdomain": world.subdomain, "detail": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This world can't be started right now. Please try again later or "
            "contact an administrator.",
        ) from exc
    except Exception as exc:
        world.state = WorldState.ERROR
        db.commit()
        world_actions_total.labels(action="start", result="error").inc()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to start"
        ) from exc
    _mark_started(world)
    db.commit()
    world_actions_total.labels(action="start", result="success").inc()
    return world_to_out(world, level)


# Nothing to do: off, on its way there, or being deleted.
_STOP_NO_OP_STATES = (WorldState.OFF, WorldState.STOPPING, WorldState.DELETING)

# How long turning off a sleeping world may wait inline before the removal
# carries on in the background.
_INLINE_STOP_WAIT_SECONDS = 5.0


@router.post("/{world_id}/stop", response_model=WorldOut)
def stop_world(
    world_id: str,
    background: BackgroundTasks,
    principal: Principal = Depends(get_principal),
    orch: Orchestrator = Depends(get_orchestrator),
    db: Session = Depends(get_db),
) -> WorldOut:
    """Turn a world off. Returns at once with the world STOPPING, or OFF for a
    sleeping world.

    The graceful shutdown (minutes, for Palworld) runs after the response,
    since the edge's timeout is shorter. If this process dies first, the
    worker's sweep finishes the stop.
    """
    world, level = load_world_for(world_id, principal, db, need=AccessLevel.OPERATOR)
    if world.state in _STOP_NO_OP_STATES:
        return world_to_out(world, level)
    was_sleeping = world.state is WorldState.SLEEPING
    # STOPPING first: it refuses a start while the old container exists.
    begin_stop(world, WorldState.OFF, db)
    if was_sleeping:
        # Only a removal (the save is on disk), so finish it inline and answer
        # Off; in the background, Sleeping seemed to jump straight to Off.
        if (
            finish_stop_within(
                world.id, world.subdomain, WorldState.OFF, orch, _INLINE_STOP_WAIT_SECONDS
            )
            is not None
        ):
            db.refresh(world)
    else:
        # Never ready: nothing to save, and a failed start may ignore SIGTERM.
        background.add_task(
            finish_stop,
            world.id,
            world.subdomain,
            WorldState.OFF,
            orch,
            graceful=world.ready_at is not None,
        )
    world_actions_total.labels(action="stop", result="accepted").inc()
    return world_to_out(world, level)


# How many times create_world may retry after losing a port-allocation race.
_PORT_ALLOCATION_ATTEMPTS = 5


def _assess_world_changes(
    world: World, payload: WorldUpdate, new_settings: dict | None = None
) -> tuple[set[str], list[str]]:
    """Return (changed edit-modal fields, escalated-risk reasons).

    ``new_settings`` is the validated game settings after the update, or None
    when it leaves them alone.
    """
    adapter = adapter_for(world.game)
    changed: set[str] = set()
    risky: list[str] = []
    if payload.display_name is not None and payload.display_name != world.display_name:
        changed.add("display_name")
    if payload.memory_mb is not None and payload.memory_mb != world.memory_mb:
        changed.add("memory_mb")
    if payload.max_players is not None and payload.max_players != world.max_players:
        changed.add("max_players")
        if payload.max_players < world.max_players:
            risky.append(
                f"Lowering the player cap from {world.max_players} to "
                f"{payload.max_players} will keep players out once the world is full."
            )
    if adapter.supports_server_password and payload.server_password is not None:
        current = decrypt(world.server_password_enc) if world.server_password_enc else ""
        new = payload.server_password.strip()
        if new != current:
            changed.add("server_password")
            if current and not new:
                risky.append(
                    "Removing the join password opens this world to anyone who knows its address."
                )
            elif current:
                risky.append("Changing the join password locks out anyone still using the old one.")
    if payload.egress_log_only is not None and payload.egress_log_only != world.egress_log_only:
        changed.add("egress_log_only")
        if payload.egress_log_only:
            risky.append(
                "Allowing unknown network connections lets this world's server reach "
                "any address on the internet. Its traffic is still recorded."
            )
    if new_settings is not None:
        settings = world.game_settings or {}
        changed |= {k for k in new_settings if new_settings.get(k) != settings.get(k)}
        risky += adapter.settings_risks(settings, new_settings)
    return changed, risky


# Which fields of a WorldUpdate each tier may set; a test checks every field is
# in exactly one set. Automatic backups fill the owner's Drive, so only the
# owner may change them.
_OWNER_ONLY_UPDATE_FIELDS = frozenset(
    {"auto_backup_enabled", "auto_backup_interval", "auto_backup_max"}
)
# Loosening a world's fence is up to the operator's
# PLATFORM_EGRESS_OVERRIDE_ACTORS.
_EGRESS_POLICY_UPDATE_FIELDS = frozenset({"egress_log_only"})
_MANAGER_UPDATE_FIELDS = frozenset(
    {
        "display_name",
        "memory_mb",
        "max_players",
        "settings",
        "server_password",
    }
)


@router.patch("/{world_id}", response_model=WorldOut)
def update_world(
    world_id: str,
    payload: WorldUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> WorldOut:
    world, level = load_world_for(world_id, principal, db, need=AccessLevel.MANAGER)

    # model_fields_set, not "is not None": what was actually sent.
    touched = set(payload.model_fields_set) - {"confirm"}
    if touched & _OWNER_ONLY_UPDATE_FIELDS and not at_least(level, AccessLevel.OWNER):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only the world's owner can change automatic backups -- they "
                "upload to the owner's Google Drive."
            ),
        )

    s = get_settings()
    if touched & _EGRESS_POLICY_UPDATE_FIELDS:
        _require_egress_override(s.egress_override_actors, principal, level)

    adapter = adapter_for(world.game)
    new_settings: dict | None = None
    if payload.settings is not None:
        try:
            new_settings = adapter.updated_settings(world.game_settings or {}, payload.settings)
        except GameSettingsError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    changed, risky = _assess_world_changes(world, payload, new_settings)
    # Any change to a world must be confirmed.
    if changed and not payload.confirm:
        detail = "This change needs confirmation."
        if risky:
            detail += " " + " ".join(risky)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
    # These apply only when the container is recreated.
    spec_fields = set(adapter.spec_fields)
    if changed & spec_fields and world.state is not WorldState.OFF:
        pretty = ", ".join(f.replace("_", " ") for f in sorted(changed & spec_fields))
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Turn the world off before changing its {pretty}.",
        )

    if payload.server_password is not None and not adapter.supports_server_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{adapter.display_name} servers have no join password.",
        )
    if payload.server_password is not None:
        pw = payload.server_password.strip()
        # Empty clears it: opening the world back up.
        world.server_password_enc = encrypt(pw) if pw else None
    if payload.display_name is not None:
        world.display_name = payload.display_name
    if new_settings is not None:
        try:
            adapter.check_settings_change(db, world, new_settings)
        except GameSettingsConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except GameSettingsError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        world.game_settings = new_settings
    if payload.max_players is not None:
        if payload.max_players > s.max_players_cap:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Player cap too high"
            )
        world.max_players = payload.max_players
    if payload.memory_mb is not None:
        _check_memory(payload.memory_mb, s)
        world.memory_mb = payload.memory_mb
    if payload.auto_backup_enabled is not None:
        if payload.auto_backup_enabled and not world.owner.drive_linked:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Link Google Drive before enabling automatic backups",
            )
        world.auto_backup_enabled = payload.auto_backup_enabled
    if payload.egress_log_only is not None and payload.egress_log_only != world.egress_log_only:
        # An audit trail of who loosened or restored the fence.
        log.warning(
            "egress override changed",
            extra={
                "world_id": str(world.id),
                "subdomain": world.subdomain,
                "egress_log_only": payload.egress_log_only,
                "by_user_id": principal.user_id,
                "by_admin": principal.is_admin,
            },
        )
        world.egress_log_only = payload.egress_log_only
    if payload.auto_backup_interval is not None:
        world.auto_backup_interval = payload.auto_backup_interval
    if payload.auto_backup_max is not None:
        world.auto_backup_max = payload.auto_backup_max
    db.commit()
    db.refresh(world)
    return world_to_out(world, level)


def _require_egress_override(mode: str, principal: Principal, level: AccessLevel) -> None:
    if mode == "none":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Network protection can't be changed on this platform.",
        )
    if mode == "admin" and not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an administrator can change a world's network protection.",
        )
    if mode == "owner_and_manager" and not at_least(level, AccessLevel.MANAGER):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the world's owner or a manager can change its network protection.",
        )


class WorldDeleteError(RuntimeError):
    """A world's teardown could not complete; its row stays in DELETING."""


# --- World icon -------------------------------------------------------------
# Changing the icon takes what PATCH takes. Fine on a running world: the file
# is replaced atomically and Minecraft reads it only at startup.


@router.get("/{world_id}/icon", response_class=FileResponse)
def get_world_icon(
    world_id: str,
    kind: Literal["server", "save"] | None = None,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> FileResponse:
    """The icon the game would show, or with ``kind`` one specific icon file
    (to preview what a clear deletes). Only a valid icon is ever served."""
    world, _ = load_world_for(world_id, principal, db, need=AccessLevel.OPERATOR)
    data_dir = _data_dir_for(world.subdomain)
    policy = adapter_for(world.game).import_policy
    path: str | None
    if kind is None:
        resolved = resolve_icon(data_dir, policy)
        path = resolved[0] if resolved else None
    else:
        path = next(
            (f.path for f in icon_files(data_dir, policy) if f.kind == kind and f.valid), None
        )
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This world has no icon")
    # no-cache means revalidate: icon_version busts it, the ETag keeps 304s.
    return FileResponse(
        path, media_type="image/png", headers={"Cache-Control": "private, no-cache"}
    )


@router.put("/{world_id}/icon", response_model=WorldOut)
def put_world_icon(
    world_id: str,
    principal: Principal = Depends(get_principal),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> WorldOut:
    world, level = load_world_for(world_id, principal, db, need=AccessLevel.MANAGER)
    # One byte over the cap shows it is too big.
    data = file.file.read(ICON_MAX_BYTES + 1)
    try:
        write_icon(
            _ensure_world_storage(world.subdomain), adapter_for(world.game).import_policy, data
        )
    except InvalidIcon as exc:
        # 400, not 422, which means the request never reached the route.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return world_to_out(world, level)


@router.get("/{world_id}/icon/files", response_model=list[IconFileOut])
def list_world_icon_files(
    world_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[IconFileOut]:
    """Every icon file the world has -- what a clear would delete."""
    world, _ = load_world_for(world_id, principal, db, need=AccessLevel.OPERATOR)
    files = icon_files(_data_dir_for(world.subdomain), adapter_for(world.game).import_policy)
    return [
        IconFileOut(kind=f.kind, path=f.relpath, valid=f.valid, version=f.version) for f in files
    ]


@router.delete("/{world_id}/icon", response_model=WorldOut)
def delete_world_icon(
    world_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> WorldOut:
    """Clear every icon file, the save's included: the game falls back to the
    save's icon."""
    world, level = load_world_for(world_id, principal, db, need=AccessLevel.MANAGER)
    removed = clear_icons(_data_dir_for(world.subdomain), adapter_for(world.game).import_policy)
    log.info("cleared world icon", extra={"world_id": str(world.id), "removed": removed})
    return world_to_out(world, level)


def teardown_world(world: World, orch: Orchestrator, db: Session) -> None:
    """Remove a world's container, data, port and row. Idempotent.

    Behind both Delete and an admin revoking a user. On failure raises
    :class:`WorldDeleteError` with a user-safe message and leaves the row in
    DELETING, for a retry or the worker's sweep.
    """
    s = get_settings()
    subdomain = world.subdomain
    world.state = WorldState.DELETING
    db.commit()
    try:
        orch.delete(subdomain)
    except Exception as exc:
        world_actions_total.labels(action="delete", result="error").inc()
        log.exception("failed to remove world container", extra={"subdomain": subdomain})
        raise WorldDeleteError("Failed to delete") from exc
    data_root = os.path.join(s.worlds_dir, subdomain)
    if os.path.isdir(data_root):
        try:
            shutil.rmtree(data_root)
        except OSError as exc:
            world_actions_total.labels(action="delete", result="error").inc()
            log.exception("failed to remove world data", extra={"subdomain": subdomain})
            raise WorldDeleteError(
                "Failed to remove world data; please try deleting again."
            ) from exc
    # The staged file; its row cascades with the world.
    staged = db.execute(
        select(StagedUpload).where(StagedUpload.world_id == world.id)
    ).scalar_one_or_none()
    if staged is not None and staged.path and os.path.exists(staged.path):
        try:
            os.remove(staged.path)
        except OSError:
            log.warning("could not remove staged file on delete", extra={"path": staged.path})
    # Released with a timestamp so it is reused last: players bookmark
    # addresses.
    if world.game_port is not None:
        release_port(db, world.game_port, subdomain, world.owner_id)
    db.delete(world)
    db.commit()
    world_actions_total.labels(action="delete", result="success").inc()
    log.info("world deleted", extra={"subdomain": subdomain})


@router.delete("/{world_id}", response_model=MessageOut)
def delete_world(
    world_id: str,
    principal: Principal = Depends(get_principal),
    orch: Orchestrator = Depends(get_orchestrator),
    db: Session = Depends(get_db),
) -> MessageOut:
    world, _ = load_world_for(world_id, principal, db, need=AccessLevel.OWNER)
    try:
        teardown_world(world, orch, db)
    except WorldDeleteError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return MessageOut(message="World deleted")


# --- Backups ---
@router.get("/{world_id}/backups", response_model=list[BackupOut])
def list_backups(
    world_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[Backup]:
    world, _ = load_world_for(world_id, principal, db, need=AccessLevel.OPERATOR)
    return list(
        db.execute(
            select(Backup).where(Backup.world_id == world.id).order_by(Backup.created_at.desc())
        ).scalars()
    )


@router.post("/{world_id}/backups", response_model=BackupOut, status_code=status.HTTP_202_ACCEPTED)
def create_backup(
    world_id: str,
    payload: BackupCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> Backup:
    world, _ = load_world_for(world_id, principal, db, need=AccessLevel.OWNER)
    if not world.owner.drive_linked:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Link Google Drive before creating backups",
        )
    # Always MANUAL; only scheduled AUTO backups are pruned by retention.
    _ = payload
    backup = Backup(
        world_id=world.id,
        kind=BackupKind.MANUAL,
        status=BackupStatus.PENDING,
        filename=_backup_filename(world),
    )
    db.add(backup)
    db.flush()
    db.add(
        Job(
            type=JobType.BACKUP,
            status=JobStatus.PENDING,
            world_id=world.id,
            payload=str(backup.id),
        )
    )
    db.commit()
    db.refresh(backup)
    return backup


@router.delete("/{world_id}/backups/{backup_id}", response_model=MessageOut)
def delete_backup(
    world_id: str,
    backup_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MessageOut:
    """Delete a backup: its Drive file (best-effort) and its record. Refused
    while the worker still has it."""
    world, _ = load_world_for(world_id, principal, db, need=AccessLevel.OWNER)
    try:
        bid = uuid.UUID(backup_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Backup not found"
        ) from exc
    backup = db.get(Backup, bid)
    if backup is None or backup.world_id != world.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backup not found")
    if backup.status in (BackupStatus.PENDING, BackupStatus.RUNNING):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Wait for the backup to finish before deleting it",
        )

    if backup.drive_file_id and world.owner and world.owner.drive_refresh_token_enc:
        try:
            drive = DriveClient(decrypt(world.owner.drive_refresh_token_enc))
            drive.delete(backup.drive_file_id)
        except Exception as exc:  # noqa: BLE001 - best-effort; still drop the record
            log.warning(
                "failed to delete drive file",
                extra={"backup_id": backup_id, "error": str(exc)},
            )

    db.delete(backup)
    db.commit()
    log.info("backup deleted", extra={"world_id": str(world.id), "backup_id": backup_id})
    return MessageOut(message="Backup deleted")


def _require_single_world(world: World, path: str, inspection) -> None:  # type: ignore[no-untyped-def]
    """Hold single-world games to exactly one world per upload.

    Guessing between two saves risks players building on the wrong one. Both
    messages say what was found: zero usually means an archive zipped one
    directory too deep.
    """
    policy = adapter_for(world.game).import_policy
    if not policy.enforce_single_world:
        return
    try:
        worlds = worlds_in_archive(path, policy)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported archive; upload a .zip or .tar.gz.",
        ) from exc

    if len(worlds) == 1:
        return
    if not worlds:
        found = ", ".join(e.name for e in inspection.entries[:8]) or "nothing"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"No world found in this archive. Upload the folder containing "
                f"{policy.bare_world_marker}. Found instead: {found}."
            ),
        )
    listed = "; ".join(w.describe() for w in worlds)
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            f"This archive contains {len(worlds)} worlds and only one can be imported. "
            f"Upload a single world. Found: {listed}."
        ),
    )


def _staged_settings(world: World, path: str) -> dict[str, str]:
    """Settings the staged archive carries, for review. Read from the archive,
    which is kept until commit, rather than stored a second time."""
    imported = adapter_for(world.game).settings_from_archive(path)
    return imported.values if imported else {}


def _adopt_imported_settings(world: World, path: str) -> None:
    """Fill in settings the owner left blank from the ones the archive carries.

    The platform renders these keys itself, so without this an imported
    world's password or player cap would silently be replaced. The archive only
    fills gaps; a value the owner set wins. Blank means unset, or for the player
    cap still the platform default. An empty archive value counts as absent.
    """
    adapter = adapter_for(world.game)
    imported = adapter.settings_from_archive(path)
    if imported is None:
        return
    s = get_settings()
    adopted: list[str] = []
    if (
        imported.max_players is not None
        and world.max_players == s.default_max_players
        and imported.max_players != world.max_players
    ):
        world.max_players = imported.max_players
        adopted.append("max_players")
    # Truthiness, not ``is not None``: PalWorldSettings.ini always has every
    # key, so no password is ServerPassword="". Treating that as "clear it"
    # once opened a world whose owner had just set a password. An import can
    # add or change a password, never remove one.
    if adapter.supports_server_password and imported.server_password:
        if not world.server_password_enc:
            world.server_password_enc = encrypt(imported.server_password)
            adopted.append("server_password")
    # Game settings likewise fill only what the owner left unset.
    current = dict(world.game_settings or {})
    for key, value in imported.settings.items():
        if value and not current.get(key):
            current[key] = value
            adopted.append(key)
    if current != (world.game_settings or {}):
        world.game_settings = adapter.validate_settings(current)
    if adopted:
        log.info(
            "adopted settings from imported world",
            extra={"world_id": str(world.id), "fields": adopted},
        )


def _staged_out(
    staged: StagedUpload, world: World, settings: dict[str, str] | None = None
) -> StagedUploadOut:
    return StagedUploadOut(
        id=staged.id,
        filename=staged.filename,
        size_bytes=staged.size_bytes,
        file_count=staged.file_count,
        unknown_entries=json.loads(staged.unknown_entries or "[]"),
        settings=settings or {},
        icon_changes=_staged_icon_changes(world, staged.path),
    )


def _staged_icon_changes(world: World, path: str | None) -> list[IconChangeOut]:
    """What committing will do to the archive's icons, read from the archive."""
    if not path or not os.path.exists(path):
        return []
    try:
        changes = archive_icon_changes(path, adapter_for(world.game).import_policy)
    except (ValueError, OSError):
        return []
    return [IconChangeOut(action=c.action, path=c.path, to=c.to) for c in changes]


def _delete_staged(staged: StagedUpload, db: Session) -> None:
    if staged.path and os.path.exists(staged.path):
        try:
            os.remove(staged.path)
        except OSError:
            log.warning("could not remove staged upload file", extra={"path": staged.path})
    db.delete(staged)
    db.commit()


@router.post(
    "/{world_id}/import/preflight",
    response_model=StagedUploadOut,
    status_code=status.HTTP_201_CREATED,
)
def preflight_import(
    world_id: str,
    principal: Principal = Depends(get_principal),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> StagedUploadOut:
    """Stage and inspect an upload, reporting prunable entries before commit.

    One staged upload per world (a unique constraint), so the caller resumes or
    deletes it.
    """
    world = _load_owned_world(world_id, principal, db)
    _require_world_stopped(world)

    existing = db.execute(
        select(StagedUpload).where(StagedUpload.world_id == world.id)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An upload is already staged for this world. Review or delete it first.",
        )

    s = get_settings()
    cfg = get_runtime_config()
    os.makedirs(s.backup_staging_dir, exist_ok=True)
    dest = os.path.join(s.backup_staging_dir, f"staged-{world.id}-{uuid.uuid4().hex}.archive")
    written = 0
    try:
        with open(dest, "wb") as fh:
            # Uncompressed is at least this large.
            while chunk := file.file.read(1024 * 1024):
                written += len(chunk)
                if written > cfg.max_upload_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="Upload is too large.",
                    )
                fh.write(chunk)

        try:
            inspection = inspect_archive(dest, adapter_for(world.game).import_policy)
            _require_single_world(world, dest, inspection)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unsupported archive; upload a .zip or .tar.gz.",
            ) from exc

        if inspection.total_bytes > cfg.max_upload_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Upload is too large when uncompressed.",
            )
        if inspection.file_count > cfg.max_upload_file_count:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Upload contains too many files.",
            )

        unknown = prunable_report(
            inspection,
            adapter_for(world.game).import_policy,
            cfg.extra_allowed_root_names,
        )

        staged = StagedUpload(
            world_id=world.id,
            path=dest,
            filename=(file.filename or "upload.archive")[:255],
            size_bytes=inspection.total_bytes,
            file_count=inspection.file_count,
            unknown_entries=json.dumps(unknown),
        )
        db.add(staged)
        try:
            db.commit()
        except IntegrityError as exc:
            # A concurrent preflight for the same world.
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An upload is already staged for this world. Review or delete it first.",
            ) from exc
        db.refresh(staged)
        return _staged_out(staged, world, _staged_settings(world, dest))
    except Exception:
        if os.path.exists(dest):
            os.remove(dest)
        raise


@router.post(
    "/{world_id}/import/preflight-from-drive",
    response_model=StagedUploadOut,
    status_code=status.HTTP_201_CREATED,
)
def preflight_import_from_drive(
    world_id: str,
    payload: ImportFromDrive,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> StagedUploadOut:
    """Stage a Drive backup for import and report prunable entries.

    Inspected like an upload, since it may have been edited, and capped at the
    restore limits, which allow more than an upload. Downloaded once here; the
    commit reuses it.
    """
    world = _load_owned_world(world_id, principal, db)
    _require_world_stopped(world)

    existing = db.execute(
        select(StagedUpload).where(StagedUpload.world_id == world.id)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An upload is already staged for this world. Review or delete it first.",
        )

    owner = world.owner
    if owner is None or not owner.drive_refresh_token_enc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Google Drive is not linked"
        )

    if payload.backup_id is not None:
        backup = db.get(Backup, payload.backup_id)
        if backup is None or backup.drive_file_id is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backup not found")
        # One of the caller's own worlds, so its file is in the caller's Drive.
        source_world = db.get(World, backup.world_id)
        if source_world is None or not is_world_owner(principal, str(source_world.owner_id)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backup not found")
        drive_file_id = backup.drive_file_id
        source_filename = backup.filename
    else:
        # A file whose row went with its world. Ownership falls to Drive: the
        # owner's token resolves only files this app created in their Drive.
        assert payload.drive_file_id is not None  # schema guarantees exactly one source
        drive_file_id = payload.drive_file_id
        try:
            meta = DriveClient(decrypt(owner.drive_refresh_token_enc)).get_file(drive_file_id)
        except Exception as exc:
            if is_not_found(exc):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Backup not found"
                ) from exc
            if unlink_if_revoked(exc, owner):
                db.commit()
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail=DRIVE_REVOKED_MESSAGE
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Could not reach Google Drive. Try again shortly.",
            ) from exc
        source_filename = str(meta.get("name") or "backup.tar.gz")

    s = get_settings()
    cfg = get_runtime_config()
    os.makedirs(s.backup_staging_dir, exist_ok=True)
    dest = os.path.join(s.backup_staging_dir, f"staged-{world.id}-{uuid.uuid4().hex}.archive")
    try:
        try:
            DriveClient(decrypt(owner.drive_refresh_token_enc)).download(
                drive_file_id, dest, max_bytes=s.max_restore_bytes
            )
        except DriveFileTooLarge as exc:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Backup is too large to restore.",
            ) from exc

        try:
            inspection = inspect_archive(dest, adapter_for(world.game).import_policy)
            _require_single_world(world, dest, inspection)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Backup archive is unreadable.",
            ) from exc

        if inspection.total_bytes > s.max_restore_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Backup is too large to restore when uncompressed.",
            )
        if inspection.file_count > s.max_restore_file_count:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Backup contains too many files to restore.",
            )

        unknown = prunable_report(
            inspection,
            adapter_for(world.game).import_policy,
            cfg.extra_allowed_root_names,
        )

        staged = StagedUpload(
            world_id=world.id,
            path=dest,
            filename=source_filename[:255],
            size_bytes=inspection.total_bytes,
            file_count=inspection.file_count,
            unknown_entries=json.dumps(unknown),
        )
        db.add(staged)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An upload is already staged for this world. Review or delete it first.",
            ) from exc
        db.refresh(staged)
        return _staged_out(staged, world, _staged_settings(world, dest))
    except Exception:
        if os.path.exists(dest):
            os.remove(dest)
        raise


@router.get("/{world_id}/import/staged", response_model=StagedUploadOut)
def get_staged_import(
    world_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> StagedUploadOut:
    world = _load_owned_world(world_id, principal, db)
    staged = db.execute(
        select(StagedUpload).where(StagedUpload.world_id == world.id)
    ).scalar_one_or_none()
    if staged is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No staged upload")
    return _staged_out(staged, world)


@router.delete("/{world_id}/import/staged", response_model=MessageOut)
def delete_staged_import(
    world_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MessageOut:
    world = _load_owned_world(world_id, principal, db)
    staged = db.execute(
        select(StagedUpload).where(StagedUpload.world_id == world.id)
    ).scalar_one_or_none()
    if staged is not None:
        _delete_staged(staged, db)
    return MessageOut(message="Staged upload discarded")


@router.post(
    "/{world_id}/import/commit",
    response_model=MessageOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def commit_import(
    world_id: str,
    payload: ImportCommit,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MessageOut:
    world = _load_owned_world(world_id, principal, db)
    _require_world_stopped(world)
    staged = db.execute(
        select(StagedUpload).where(StagedUpload.world_id == world.id)
    ).scalar_one_or_none()
    if staged is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No staged upload to import; upload a file first.",
        )
    unknown = json.loads(staged.unknown_entries or "[]")
    if unknown and not payload.prune:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This upload contains items outside a normal world; confirm pruning to proceed.",
        )
    path = staged.path
    _adopt_imported_settings(world, path)
    # The job owns the file once queued. Preflight already applied the caps.
    _enqueue_import(
        db,
        world,
        {
            "source": "file",
            "path": path,
            "prune": payload.prune,
            "skip_limits": True,
        },
    )
    db.delete(staged)
    db.commit()
    return MessageOut(message="Import queued")


def _require_world_stopped(world: World) -> None:
    if world.state in (WorldState.RUNNING, WorldState.SLEEPING, WorldState.STOPPING):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Turn the world off before importing data into it",
        )


def _enqueue_import(db: Session, world: World, spec: dict[str, object]) -> None:
    db.add(
        Job(
            type=JobType.IMPORT,
            status=JobStatus.PENDING,
            world_id=world.id,
            payload=json.dumps(spec),
        )
    )
    db.commit()


def _backup_filename(world: World) -> str:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{world.subdomain}-{ts}.tar.gz"
