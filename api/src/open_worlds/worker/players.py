"""Refresh each running world's player count from its admin interface.

The API is kept off the `games` network, so the worker polls and writes the
count to the world row for the dashboard. A failed poll leaves the previous
value and timestamp, so a blip never makes a busy world look empty.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..common.config import get_settings
from ..common.crypto import decrypt
from ..common.games import AdminUnavailable, adapter_for
from ..common.logging import get_logger
from ..common.models import World, WorldState

log = get_logger(__name__)


def _poll_world(world: World) -> tuple[int, str | None] | None:
    """Ask one world's admin interface for its player list and version.

    Returns (count, version), or None when unreachable; neither RCON nor
    Palworld's REST API answers until the server has booted, so reaching it is
    the readiness signal. ``version`` is None for games that pin their version.
    """
    if not world.admin_password_enc:
        return None
    adapter = adapter_for(world.game)
    host = f"{get_settings().container_name_prefix}{world.subdomain}"
    admin = adapter.admin_client(host, decrypt(world.admin_password_enc))
    # Unreachable is the common case while a world boots, and Minecraft's
    # client holds an RCON socket.
    try:
        try:
            count = len(admin.players())
        except AdminUnavailable as exc:
            # Expected while a world is still booting; not worth an error log.
            log.debug("admin poll failed", extra={"subdomain": world.subdomain, "error": str(exc)})
            return None
        version: str | None = None
        if not adapter.supports_version_pinning:
            try:
                version = admin.server_version()
            except AdminUnavailable:
                # The count already proved the world is up.
                pass
        return count, version
    finally:
        admin.close()


def _record_poll(world: World, count: int, version: str | None, now: datetime) -> None:
    world.player_count = count
    world.players_updated_at = now
    # Only when somebody is on: idle-sleep measures from this.
    if count > 0:
        world.last_active_at = now
    # The only truth for a game that cannot pin a version; written on change.
    if version and version != world.reported_version:
        log.info(
            "detected server version",
            extra={"subdomain": world.subdomain, "version": version, "was": world.reported_version},
        )
        world.reported_version = version[:64]
    _mark_ready(world, now)


def _mark_ready(world: World, when: datetime) -> None:
    """Record the first successful contact after a start, once."""
    if world.ready_at is None:
        world.ready_at = when
        log.info(
            "world is accepting players",
            extra={"subdomain": world.subdomain, "game": world.game},
        )


def refresh_player_counts(db: Session) -> int:
    """Poll every RUNNING world; return how many answered.

    The slow loop for player counts. It stamps readiness too, rather than
    leaving a world to wait for the next probe.
    """
    worlds = list(db.execute(select(World).where(World.state == WorldState.RUNNING)).scalars())
    polled = 0
    now = datetime.now(UTC)
    for world in worlds:
        polled_world = _poll_world(world)
        if polled_world is None:
            continue
        _record_poll(world, *polled_world, now)
        polled += 1
    return polled


def probe_pending_worlds(db: Session) -> int:
    """Probe only worlds that are running but not yet confirmed joinable.

    The fast loop: its working set is the worlds currently booting, normally
    none. Returns how many became ready.
    """
    pending = list(
        db.execute(
            select(World).where(
                World.state == WorldState.RUNNING,
                World.ready_at.is_(None),
            )
        ).scalars()
    )
    if not pending:
        return 0
    became_ready = 0
    now = datetime.now(UTC)
    for world in pending:
        polled_world = _poll_world(world)
        if polled_world is None:
            continue
        _record_poll(world, *polled_world, now)
        became_ready += 1
    return became_ready
