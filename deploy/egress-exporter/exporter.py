"""Serve the guards' refusals, from the dns-observer's report, as Prometheus metrics.

Every guard sends its refusals to the dns-observer, which writes them into its
report. This unprivileged process (no host namespace, root or capabilities, a
read-only view of that directory) reads the report and answers /metrics: is the
dns-observer alive, and how many refusals does no DENY entry explain.

Every series is a single unlabelled sample; the names stay in the JSON
(scripts/egress-refusals.sh prints them).
"""

from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import trust

DNS_REPORT_PATH = os.environ.get("DNS_REPORT_PATH", "/data/dns-observations.json")
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9109"))
#: Past this the report is stale: a dns-observer that died would otherwise
#: leave its last refusal count looking healthy forever.
STALE_AFTER_SECONDS = int(os.environ.get("STALE_AFTER", "300"))


def _read_or_none(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        # Includes scraping between the dns-observer's write and rename.
        return None


def render_dns(report: dict | None, *, now: float | None = None) -> str:
    """The dns-observer's health, and the refusals worth a look.

    ``egress_refused_unlisted`` counts distinct refusals guards made (or would
    have, with blocking off) that no DENY entry explains for the world's game:
    a name no entry covers, an allowed name on a new port or protocol, a UDP
    flow, a connection with no name. Each might be a game needing something
    new; scripts/egress-refusals.sh prints them with a suggested entry. A gauge
    rather than a counter: an entry added to DENY lowers it, so the alert asks
    whether it went *up* (delta), not increase().
    """
    now = time.time() if now is None else now
    if report is None:
        # Stale as well as absent: the alert watches dns_report_stale, and a
        # report nobody can read says no more than one nobody has written.
        return (
            "# HELP dns_report_present Whether the dns-observer's report could be read.\n"
            "# TYPE dns_report_present gauge\n"
            "dns_report_present 0\n"
            "# HELP dns_report_stale Whether the dns-observer's report is older than the threshold.\n"
            "# TYPE dns_report_stale gauge\n"
            "dns_report_stale 1\n"
        )
    age = max(0, int(now - report.get("generated_at", now)))
    unlisted = sum(
        1
        for row in report.get("refused", [])
        if isinstance(row, dict) and not trust.explained(row)
    )
    return "\n".join(
        [
            "# HELP dns_report_present Whether the dns-observer's report could be read.",
            "# TYPE dns_report_present gauge",
            "dns_report_present 1",
            "# HELP dns_report_age_seconds Seconds since the dns-observer last wrote its report.",
            "# TYPE dns_report_age_seconds gauge",
            f"dns_report_age_seconds {age}",
            "# HELP dns_report_stale Whether the dns-observer's report is older than the threshold.",
            "# TYPE dns_report_stale gauge",
            f"dns_report_stale {int(age > STALE_AFTER_SECONDS)}",
            "# HELP egress_refused_unlisted Distinct refusals by guards that no DENY entry explains.",
            "# TYPE egress_refused_unlisted gauge",
            f"egress_refused_unlisted {unlisted}",
        ]
    ) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - name fixed by http.server
        if self.path not in ("/metrics", "/"):
            self.send_response(404)
            self.end_headers()
            return
        body = render_dns(_read_or_none(DNS_REPORT_PATH)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Silence per-request logging; a scrape every 15s is not news."""


def main() -> None:
    print(f"exporting {DNS_REPORT_PATH} on :{METRICS_PORT}/metrics", flush=True)
    ThreadingHTTPServer(("", METRICS_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
