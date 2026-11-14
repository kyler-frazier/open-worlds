"""Authorization primitives, pure so they can be unit tested without FastAPI.

Two separate axes:

1. **How much you can do**: the :class:`AccessLevel` ladder, with admins at
   the top.
2. **Whether you are the owner**: :func:`is_world_owner`, which admins fail.
   It guards imports and restores.

Plus the closed-garden gate on Google `sub` values.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    """The minimal identity needed to make authorization decisions."""

    user_id: str
    google_sub: str
    is_admin: bool


def is_allowed(google_sub: str, approved_subs: set[str], admin_subs: set[str]) -> bool:
    """Closed-garden gate: a subject may enter only if explicitly approved.

    Admins seeded via configuration are implicitly approved.
    """
    return google_sub in approved_subs or google_sub in admin_subs


class AccessLevel(str, enum.Enum):
    """What a caller may do to one world, as a ladder.

    Unlike the stored :class:`~open_worlds.common.models.CollaboratorLevel`, it
    has NONE and OWNER, which are never rows.
    """

    NONE = "NONE"
    OPERATOR = "OPERATOR"
    MANAGER = "MANAGER"
    OWNER = "OWNER"


_RANK: dict[AccessLevel, int] = {
    AccessLevel.NONE: 0,
    AccessLevel.OPERATOR: 1,
    AccessLevel.MANAGER: 2,
    AccessLevel.OWNER: 3,
}


def at_least(have: AccessLevel, need: AccessLevel) -> bool:
    """Whether ``have`` is at or above ``need`` on the ladder."""
    return _RANK[have] >= _RANK[need]


def resolve_access(
    principal: Principal,
    world_owner_id: str,
    collaborator_level: str | None,
) -> AccessLevel:
    """What this principal may do to this world.

    ``collaborator_level`` is the stored grant as a plain string, or None.
    Owner and admin are checked before the grant, so a grant can only add
    access, never demote.
    """
    if principal.user_id == world_owner_id:
        return AccessLevel.OWNER
    # Admins rank as OWNER; is_world_owner still keeps them out of imports
    # and restores.
    if principal.is_admin:
        return AccessLevel.OWNER
    if collaborator_level is None:
        return AccessLevel.NONE
    try:
        return AccessLevel(collaborator_level)
    except ValueError:
        # An unrecognised stored value must never fail open.
        return AccessLevel.NONE


def is_world_owner(principal: Principal, world_owner_id: str) -> bool:
    """Owner only; admin status does not count.

    An import overwrites what the owner built and a restore reads the owner's
    Drive, so both are the owner's alone.
    """
    return principal.user_id == world_owner_id
