"""Allocation of the per-world UDP game port.

Minecraft routes every world through one TCP port by the hostname in its
handshake. Palworld's UDP carries no such field, so the port is the routing
key and each world needs its own. The adapter's ``allocates_port`` decides.

Ports come from the configured range, and a world holds one from create to
delete. Uniqueness is the database's job (a unique constraint; callers retry).
A freed port is reused last, so old bookmarks do not point at a new world,
except by the same subdomain and owner recreating it, who get it back.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .logging import get_logger
from .models import PortRelease, World

log = get_logger(__name__)


class NoPortAvailable(RuntimeError):
    """Every port in the configured range is already assigned to a world."""


def allocate_port(
    db: Session,
    settings: Settings | None = None,
    subdomain: str | None = None,
    owner_id: uuid.UUID | None = None,
) -> int:
    """Return a free game port for a world.

    Prefers this world's last port, then a never-used one, then the longest
    released. Reclaim needs both ``subdomain`` and ``owner_id``: subdomains are
    reusable, so a name alone could hand a stranger someone else's address.

    Raises :class:`NoPortAvailable` when the range is exhausted, which is also
    the cap on how many worlds can exist.
    """
    s = settings or get_settings()
    taken = {p for p in db.execute(select(World.game_port)).scalars() if p is not None}
    free = [p for p in s.game_ports if p not in taken]
    if not free:
        raise NoPortAvailable(
            f"All {s.max_worlds} game ports are in use. Delete a world to free one."
        )

    reclaimed = _reclaimable_port(db, free, subdomain, owner_id)
    if reclaimed is not None:
        log.info(
            "reclaiming this world's previous port",
            extra={"subdomain": subdomain, "port": reclaimed},
        )
        return reclaimed

    released = {
        port: at
        for port, at in db.execute(
            select(PortRelease.port, PortRelease.released_at).where(PortRelease.port.in_(free))
        ).all()
    }
    # Never-released first, then oldest release, then port number.
    free.sort(key=lambda p: (p in released, released.get(p), p))
    return free[0]


def _reclaimable_port(
    db: Session,
    free: list[int],
    subdomain: str | None,
    owner_id: uuid.UUID | None,
) -> int | None:
    """The port this subdomain and owner last released, if still free.

    Most recent first, since that is the address most likely saved.
    """
    if not subdomain or owner_id is None:
        return None
    return db.execute(
        select(PortRelease.port)
        .where(
            PortRelease.subdomain == subdomain,
            PortRelease.owner_id == owner_id,
            PortRelease.port.in_(free),
        )
        .order_by(PortRelease.released_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def release_port(
    db: Session,
    port: int,
    subdomain: str | None = None,
    owner_id: uuid.UUID | None = None,
) -> None:
    """Record that ``port`` is free again, and who freed it."""
    row = db.get(PortRelease, port)
    now = datetime.now(UTC)
    if row is None:
        db.add(PortRelease(port=port, released_at=now, subdomain=subdomain, owner_id=owner_id))
        return
    row.released_at = now
    # Only the last holder may reclaim it.
    row.subdomain = subdomain
    row.owner_id = owner_id
