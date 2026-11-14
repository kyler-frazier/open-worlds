"""A Minecraft world's own settings: its pinned version and server flavour."""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .versions import is_pinnable_version

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...models import World

VERSION_HELP = (
    "Pin the world to a specific Minecraft version — a release like 1.21.4 or a "
    "snapshot like 24w14a. The rolling LATEST/SNAPSHOT aliases are not allowed, so "
    "a world never auto-updates into a game version that could break its data or mods."
)


class ServerType(str, enum.Enum):
    """Server flavour: which loader, and so which mods and plugins, it runs."""

    VANILLA = "VANILLA"
    PAPER = "PAPER"
    FABRIC = "FABRIC"
    FORGE = "FORGE"


class MinecraftSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_type: ServerType = Field(default=ServerType.VANILLA, title="Server type")
    # Empty only on the way in: a new world with no version gets the latest
    # stable release (MinecraftAdapter.new_settings), so a stored world always
    # has a concrete one.
    version: str = Field(
        default="",
        max_length=32,
        title="Version",
        description="A release like 1.21.4 or a snapshot like 24w14a.",
    )

    @field_validator("version")
    @classmethod
    def _pinned_version(cls, v: str) -> str:
        v = (v or "").strip()
        if v and not is_pinnable_version(v):
            raise ValueError(VERSION_HELP)
        return v


def settings_of(world: World) -> MinecraftSettings:
    """A stored world's settings, typed."""
    return MinecraftSettings.model_validate(world.game_settings or {})
