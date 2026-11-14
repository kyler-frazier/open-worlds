"""A Palworld world's own settings."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .settings_file import UNSAFE_VALUE_CHARS

#: Palworld writes its settings as one line of ``Key=Value`` pairs with no escape
#: syntax, so these characters in free text would truncate or corrupt every
#: setting after them. The renderer strips them defensively; rejecting here means
#: the owner finds out at the point of editing instead of wondering why their
#: description came back different.
DESCRIPTION_HELP = "Server description cannot contain quotes, commas, parentheses or line breaks."


class PalworldSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Shown beside the world in-game. Empty means unset. It reaches the server
    # through the settings file, which a running server never re-reads, so it
    # is one of the adapter's spec_fields.
    server_description: str = Field(
        default="",
        max_length=256,
        title="Server description (optional)",
        description="Shown beside the world in-game.",
    )

    @field_validator("server_description")
    @classmethod
    def _safe_description(cls, v: str) -> str:
        v = (v or "").strip()
        if any(ch in UNSAFE_VALUE_CHARS for ch in v):
            raise ValueError(DESCRIPTION_HELP)
        return v
