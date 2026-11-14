"""Pure retention logic for pruning AUTO backups.

Deliberately dependency-free and side-effect-free so it is trivially unit
tested. MANUAL backups are never considered here: only AUTO backups count
toward, and are eligible for, the configurable retention limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class BackupRef:
    id: str
    kind: str  # "AUTO" or "MANUAL"
    created_at: datetime


def select_backups_to_prune(backups: list[BackupRef], max_auto: int) -> list[BackupRef]:
    """Return the AUTO backups that should be deleted to satisfy ``max_auto``.

    Keeps the ``max_auto`` most recent AUTO backups and returns the older ones.
    MANUAL backups are ignored entirely and never returned.
    """
    if max_auto < 0:
        raise ValueError("max_auto must be non-negative")
    autos = [b for b in backups if b.kind == "AUTO"]
    autos.sort(key=lambda b: b.created_at, reverse=True)
    return autos[max_auto:]
