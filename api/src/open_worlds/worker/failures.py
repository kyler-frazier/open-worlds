"""Notice a world whose server failed to start, say why, and stop it.

Each case records a log excerpt on the world (``last_error``) for its card:

* A booting server logs a known fatal line (``fatal_log_patterns``): it is
  stopped at once, having nothing to save, and marked ERROR.
* A booting server outlives its boot window with no known fatal line: the end
  of its log is recorded and it is left running, since it may just be slow.
* A container exits with an error (the crash path in reconcile): its fatal
  line or tail is recorded.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..common.crypto import decrypt
from ..common.games import adapter_for
from ..common.logging import get_logger
from ..common.models import World, WorldState
from ..common.orchestrator import Orchestrator
from ..common.readiness import Readiness, readiness_for
from ..common.startup_errors import excerpt_from_fatal, excerpt_from_tail
from ..common.stopping import perform_stop
from ..common.world_status import apply_state

log = get_logger(__name__)


def _secrets(world: World) -> tuple[str, ...]:
    out = []
    for enc in (world.admin_password_enc, world.server_password_enc):
        if enc:
            try:
                out.append(decrypt(enc))
            except Exception:  # noqa: BLE001 - redaction is best effort, never fatal
                continue
    return tuple(out)


def _record(world: World, excerpt: str, now: datetime) -> None:
    # Only a changed excerpt is news: rewriting the timestamp every pass would
    # re-announce the same error on every dashboard poll.
    if excerpt and excerpt != world.last_error:
        world.last_error = excerpt
        world.last_error_at = now


def explain_crash(world: World, orch: Orchestrator, now: datetime | None = None) -> None:
    """Record why an exited container failed. Called as it is found exited
    with an error; the log survives on the exited container."""
    try:
        raw = orch.logs(world.subdomain)
    except Exception:  # noqa: BLE001 - a missing reason must not block the state change
        log.warning("could not read logs of crashed world", extra={"subdomain": world.subdomain})
        return
    if not raw:
        return
    adapter = adapter_for(world.game)
    secrets = _secrets(world)
    excerpt = excerpt_from_fatal(raw, adapter.fatal_log_patterns, secrets) or excerpt_from_tail(
        raw, secrets
    )
    _record(world, excerpt, now or datetime.now(UTC))


def check_startup_failures(db: Session, orch: Orchestrator) -> int:
    """Check every booting world's log; returns how many were stopped as failed."""
    booting = list(
        db.execute(
            select(World).where(World.state == WorldState.RUNNING, World.ready_at.is_(None))
        ).scalars()
    )
    stopped = 0
    now = datetime.now(UTC)
    for world in booting:
        adapter = adapter_for(world.game)
        try:
            raw = orch.logs(world.subdomain)
        except Exception:  # noqa: BLE001 - try again next tick
            log.debug("could not read logs", extra={"subdomain": world.subdomain})
            continue
        secrets = _secrets(world)
        fatal = excerpt_from_fatal(raw, adapter.fatal_log_patterns, secrets)
        if fatal:
            _record(world, fatal, now)
            log.warning(
                "world failed to start; stopping it",
                extra={"subdomain": world.subdomain, "game": world.game},
            )
            try:
                # Kept, not removed, so its full log stays readable; ERROR keeps
                # the waker from starting it again on the next connection.
                perform_stop(orch, world.subdomain, WorldState.SLEEPING, graceful=False)
            except Exception:  # noqa: BLE001 - recorded either way; reconcile sees it
                log.exception("could not stop failed world", extra={"subdomain": world.subdomain})
            apply_state(world, WorldState.ERROR)
            stopped += 1
        elif (
            world.last_error is None
            and readiness_for(world, adapter.readiness_timeout_seconds, now)
            is Readiness.UNRESPONSIVE
        ):
            _record(world, excerpt_from_tail(raw, secrets), now)
    return stopped
