"""The exporter: dns-observer health and the guards' unexplained refusals."""

from __future__ import annotations

import exporter


def test_refused_names_on_the_deny_list_are_not_counted():
    report = {
        "generated_at": 1000,
        "refused": [
            {"name": "telemetry.palworldgame.com"},  # DENY: refused on purpose
            {"name": "api.github.com"},  # DENY
            {"name": "cdn.new-game-host.example"},  # on no list: worth a look
        ],
    }
    out = exporter.render_dns(report, now=1010)
    assert "egress_refused_unlisted 1" in out
    assert "dns_report_stale 0" in out


def test_refusals_are_judged_against_their_own_games_deny_list():
    report = {
        "generated_at": 1000,
        "refused": [
            # On DENY for Palworld, so expected from a Palworld world...
            {"game": "PALWORLD", "layer": "dns", "reason": "denied", "name": "b.palworldgame.com"},
            # ...but a Minecraft world reaching for Palworld telemetry is news.
            {"game": "MINECRAFT", "layer": "dns", "reason": "unlisted_name",
             "name": "telemetry.palworldgame.com"},
            # An allowed name on a new port is news.
            {"game": "PALWORLD", "layer": "gate", "reason": "port_not_allowed", "proto": "tls",
             "name": "api.steampowered.com", "port": 8443},
            # A UDP flow has no name and is news.
            {"game": "PALWORLD", "layer": "firewall", "reason": "not_tcp", "proto": "udp",
             "dst": "198.51.100.4", "port": 3478},
        ],
    }
    out = exporter.render_dns(report, now=1010)
    assert "egress_refused_unlisted 3" in out
    assert "dns_report_stale 0" in out


def test_a_silent_dns_observer_reads_as_stale():
    out = exporter.render_dns({"generated_at": 0, "refused": []}, now=10_000)
    assert "dns_report_stale 1" in out


def test_a_missing_dns_report_is_said_plainly_and_alerts():
    out = exporter.render_dns(None)
    assert "dns_report_present 0" in out
    assert "dns_report_stale 1" in out


def test_the_report_is_read_from_the_dns_observers_file(tmp_path):
    path = tmp_path / "dns-observations.json"
    assert exporter._read_or_none(str(path)) is None
    path.write_text('{"generated_at": 1, "refused": []}')
    assert exporter._read_or_none(str(path)) == {"generated_at": 1, "refused": []}


def test_a_refusal_is_judged_against_its_own_stages_deny_list():
    # Forge's installer tries a mirror it never needs: expected while
    # installing, news from a running server (which has no reason to).
    def unlisted(stage, variant="FORGE"):
        row = {"game": "MINECRAFT", "variant": variant, "stage": stage, "layer": "gate", "reason": "unlisted_name",
               "proto": "tls", "name": "maven.creeperhost.net", "port": 443}
        return exporter.render_dns({"generated_at": 1000, "refused": [row]}, now=1010)

    assert "egress_refused_unlisted 0" in unlisted("install")
    assert "egress_refused_unlisted 1" in unlisted("play")
    assert "egress_refused_unlisted 1" in unlisted("install", "PAPER")
    # A record from a guard that predates stages: only an every-stage denial
    # would explain it.
    assert "egress_refused_unlisted 1" in unlisted(None)
