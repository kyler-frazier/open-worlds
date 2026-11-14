"""Subdomain validation and normalization.

The subdomain is security-sensitive: it becomes both a public DNS label and a
Docker container name, so it must be a strict, lowercase DNS label. A curated
reserved list prevents users from claiming operationally significant names.
"""

from __future__ import annotations

import re

# RFC 1035 label: starts/ends alphanumeric, may contain hyphens, 1-63 chars.
# A DNS label. Also the only shape world-manager accepts, since a subdomain
# becomes a path (worlds/<subdomain>/data) and a container name there.
LABEL_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
_LABEL_RE = re.compile(LABEL_PATTERN)

# Docker container names must fit alongside the prefix; the DNS label cap of 63
# is the tighter constraint, so validating as a DNS label is sufficient.
MAX_LABEL_LENGTH = 63

RESERVED_SUBDOMAINS = frozenset(
    {
        "www",
        "api",
        "admin",
        "pal",
        "worlds",
        "world",
        "waker",
        "servers",
        "server",
        "router",
        "grafana",
        "prometheus",
        "metrics",
        "db",
        "database",
        "root",
        "mail",
        "ftp",
        "ns",
        "ns1",
        "ns2",
        "localhost",
        "internal",
        "static",
    }
)


class InvalidSubdomain(ValueError):
    """Raised when a proposed subdomain is not a valid, allowed DNS label."""


def is_dns_label(value: str) -> bool:
    return bool(_LABEL_RE.match(value))


def normalize_subdomain(raw: str) -> str:
    return (raw or "").strip().lower()


def _game_names() -> set[str]:
    """Every installed game's id, lower case: a world called "minecraft" reads
    as the platform's own address for the game."""
    from .games import game_ids  # local: the registry loads the adapters

    return {g.lower() for g in game_ids()}


def validate_subdomain(raw: str) -> str:
    """Return the normalized subdomain or raise :class:`InvalidSubdomain`."""
    value = normalize_subdomain(raw)
    if not value:
        raise InvalidSubdomain("Subdomain must not be empty.")
    if len(value) > MAX_LABEL_LENGTH:
        raise InvalidSubdomain(f"Subdomain must be at most {MAX_LABEL_LENGTH} characters.")
    if not _LABEL_RE.match(value):
        raise InvalidSubdomain(
            "Subdomain must be a valid DNS label: lowercase letters, digits, and "
            "hyphens only, and may not start or end with a hyphen."
        )
    if value in RESERVED_SUBDOMAINS or value in _game_names():
        raise InvalidSubdomain("That subdomain is reserved.")
    # The orchestrator names a world's egress guard game-world-<subdomain>--egress;
    # a world allowed to end in that suffix could take another world's guard slot.
    if value.endswith("--egress"):
        raise InvalidSubdomain("That subdomain is reserved.")
    return value
