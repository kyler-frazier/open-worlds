"""Existing worlds keep their network open when every world gets a guard.

Until now a world only had a guard if the operator listed it, so every other
world reached the internet freely. From here on every world has a guard, and
egress_log_only is its own per-world switch rather than a debugging override.
Leaving existing rows at false would quietly start blocking worlds that have
never been tested behind a guard; setting them to log-only keeps their
behaviour and starts recording it. Blocking a world is then a deliberate
choice, made per world.
"""

from __future__ import annotations

from alembic import op

revision = "0006_existing_worlds_egress_open"
down_revision = "0005_world_egress_log_only"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE worlds SET egress_log_only = true")


def downgrade() -> None:
    # Not reversible: which worlds were blocked before is not recorded, and
    # guessing would loosen or tighten someone's world without asking.
    pass
