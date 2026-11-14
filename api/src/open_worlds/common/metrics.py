"""Prometheus metrics shared across the API and worker.

Kept in one module so metric names are defined exactly once and reused, which
avoids duplicate-registration errors and keeps dashboards consistent.
"""

from __future__ import annotations

import ipaddress
import threading
from collections.abc import Callable, Iterable
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, make_server

from prometheus_client import Counter, Gauge, Histogram, make_wsgi_app
from prometheus_client.exposition import ThreadingWSGIServer


# --- Who may scrape ---
# Worker and internal-api sit on the games network beside every world. Guards
# already stop worlds reaching private addresses; this keeps world names and
# states out of reach of anything else there too (a router, an unguarded
# world when the guard switch is off).
def scrape_allowed(peer: str | None, games_subnet: str) -> bool:
    try:
        return ipaddress.ip_address(peer or "") not in ipaddress.ip_network(games_subnet)
    except ValueError:
        return False


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, *_args: Any) -> None:
        pass


def start_metrics_server(port: int, games_subnet: str) -> None:
    """prometheus_client's start_http_server, refusing the games network."""
    metrics = make_wsgi_app()

    def app(environ: dict, start_response: Callable) -> Iterable[bytes]:
        if not scrape_allowed(environ.get("REMOTE_ADDR"), games_subnet):
            start_response("403 Forbidden", [("Content-Type", "text/plain")])
            return [b"forbidden\n"]
        return metrics(environ, start_response)

    httpd = make_server("", port, app, ThreadingWSGIServer, handler_class=_QuietHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()


# --- World lifecycle ---
world_actions_total = Counter(
    "pal_world_actions_total",
    "Count of world lifecycle actions requested.",
    ["action", "result"],
)

worlds_by_state = Gauge(
    "pal_worlds_by_state",
    "Number of worlds currently in each state (as recorded in the database).",
    ["state"],
)

# --- Backups ---
backups_total = Counter(
    "pal_backups_total",
    "Count of backup attempts.",
    ["kind", "result"],
)

backup_duration_seconds = Histogram(
    "pal_backup_duration_seconds",
    "Wall-clock duration of backup jobs.",
    ["kind"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800),
)

backup_bytes = Histogram(
    "pal_backup_bytes",
    "Size of produced backup archives in bytes.",
    buckets=(
        1 * 1024 * 1024,
        10 * 1024 * 1024,
        100 * 1024 * 1024,
        512 * 1024 * 1024,
        1024 * 1024 * 1024,
        5 * 1024 * 1024 * 1024,
    ),
)

# --- Worker job queue ---
jobs_processed_total = Counter(
    "pal_jobs_processed_total",
    "Count of worker jobs processed.",
    ["type", "result"],
)

jobs_pending = Gauge(
    "pal_jobs_pending",
    "Number of jobs currently pending in the queue.",
)

# --- Resilience / self-healing (worker maintenance sweep) ---
jobs_reaped_total = Counter(
    "pal_jobs_reaped_total",
    "Jobs recovered by the maintenance sweep, by disposition.",
    ["disposition"],  # requeued | failed
)

worlds_reconciled_total = Counter(
    "pal_worlds_reconciled_total",
    "Worlds whose stored state was corrected to match the live container.",
)

deletes_finished_total = Counter(
    "pal_deletes_finished_total",
    "Worlds wedged in DELETING that the maintenance sweep finished tearing down.",
)

orphan_containers_removed_total = Counter(
    "pal_orphan_containers_removed_total",
    "Managed containers found with no world row, stopped and removed by the sweep.",
)

world_unexpected_exits_total = Counter(
    "pal_world_unexpected_exits_total",
    "Managed containers that crashed: exited with a non-graceful code (crash/OOM), or, "
    "for a game whose every stop goes through the platform, exited without being asked.",
)

# Free space where backups are staged. Alerting needs a number to compare
# against, and the app is the only thing that knows which filesystem matters:
# cadvisor reports per-container usage, not the host path backups land on.
backup_staging_free_bytes = Gauge(
    "pal_backup_staging_free_bytes",
    "Free bytes on the filesystem holding the backup staging directory.",
)
