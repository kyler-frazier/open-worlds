"""A world's container spec, built from its row: the api's starts and wakes and
the worker's install hand-off all start worlds from this."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .crypto import decrypt
from .games import adapter_for
from .models import World
from .orchestrator import WorldSpec


def spec_for(world: World, db: Session) -> WorldSpec:
    """Build the container spec from the world row (the source of truth)."""
    adapter = adapter_for(world.game)
    return WorldSpec(
        game=world.game,
        world_id=str(world.id),
        owner_id=str(world.owner_id),
        subdomain=world.subdomain,
        display_name=world.display_name,
        memory_mb=world.memory_mb,
        max_players=world.max_players,
        admin_password=decrypt(world.admin_password_enc) if world.admin_password_enc else "",
        server_password=(decrypt(world.server_password_enc) if world.server_password_enc else ""),
        game_port=world.game_port,
        settings=world.game_settings or {},
        extras=adapter.spec_extras(db, world),
        egress_log_only=bool(world.egress_log_only),
    )
