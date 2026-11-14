"""Unit tests for the guard's decisions. The firewall itself needs a network
namespace and CAP_NET_ADMIN, so it is exercised end to end instead (see
RUNBOOK.md, "Egress guard"); these cover what can go wrong in plain Python.

Run from the repo root:  python -m pytest egress-guard
"""

import asyncio
import json
import pathlib
import socket
import struct
import sys
from dataclasses import replace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "egress-guard"), str(ROOT / "dns-observer")]

import guard  # noqa: E402
import pytest  # noqa: E402
import trust  # noqa: E402


@pytest.fixture(autouse=True)
def palworld_guard(monkeypatch):
    """Most tests run as a running Palworld server's guard; the others say so."""
    _as(monkeypatch, "PALWORLD")
    guard._refusals.clear()
    guard._lookups.clear()


def _as(monkeypatch, game, variant="", stage="play"):
    monkeypatch.setattr(guard, "GAME", game)
    monkeypatch.setattr(guard, "VARIANT", variant)
    monkeypatch.setattr(guard, "STAGE", stage)
    monkeypatch.setattr(guard, "POLICY", trust.Policy(game, variant, stage))


def _query(name: str, qtype: int = 1, qid: int = 0x1234) -> bytes:
    qname = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
    return struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0) + qname + struct.pack("!HH", qtype, 1)


def _answer(query: bytes, records: list[tuple[int, int, bytes]]) -> bytes:
    """A response to ``query`` with (type, ttl, rdata) answers, names compressed."""
    head = struct.pack("!HHHHHH", struct.unpack("!H", query[:2])[0], 0x8180, 1, len(records), 0, 0)
    body = query[12:]
    for rtype, ttl, rdata in records:
        body += b"\xc0\x0c" + struct.pack("!HHIH", rtype, 1, ttl, len(rdata)) + rdata
    return head + body


def test_only_this_games_names_resolve(monkeypatch):
    assert guard.is_approved("auth.game.pocketpair.jp")
    assert guard.is_approved("Auth.Game.Pocketpair.jp.")  # case and trailing dot
    # Minecraft's names are not Palworld's business, and the reverse.
    assert not guard.is_approved("sessionserver.mojang.com")
    _as(monkeypatch, "MINECRAFT", "PAPER")
    assert guard.is_approved("sessionserver.mojang.com")
    assert not guard.is_approved("auth.game.pocketpair.jp")
    _as(monkeypatch, "MINECRAFT", "PAPER", "install")
    assert not guard.is_approved("cache1-xyz1.steamcontent.com")


def test_a_running_server_cannot_reach_what_its_install_downloaded_from(monkeypatch):
    # The point of the split: a mod in the running server gets no CDN, no maven.
    assert not guard.is_approved("cache1-xyz1.steamcontent.com")
    assert not guard.is_approved("cmp1-xyz1.steamserver.net")
    _as(monkeypatch, "PALWORLD", stage="install")
    assert guard.is_approved("cache1-xyz1.steamcontent.com")  # *.steamcontent.com
    assert guard.is_approved("cmp1-xyz1.steamserver.net")
    for variant, host in [("PAPER", "fill-data.papermc.io"), ("FABRIC", "maven.fabricmc.net"),
                          ("FORGE", "maven.minecraftforge.net"), ("VANILLA", "piston-data.mojang.com")]:
        _as(monkeypatch, "MINECRAFT", variant, "install")
        assert guard.is_approved(host), (variant, host)
        _as(monkeypatch, "MINECRAFT", variant, "play")
        assert not guard.is_approved(host), (variant, host)


def test_an_install_cannot_reach_what_only_the_server_needs(monkeypatch):
    _as(monkeypatch, "PALWORLD", stage="install")
    assert not guard.is_approved("auth.game.pocketpair.jp")
    assert not guard.is_approved("api.epicgames.dev")
    _as(monkeypatch, "MINECRAFT", "VANILLA", "install")
    assert not guard.is_approved("api.minecraftservices.com")


def test_a_name_can_be_needed_to_install_and_refused_while_playing(monkeypatch):
    # Paper's build service: its download at install, its update check at boot.
    _as(monkeypatch, "MINECRAFT", "PAPER", "install")
    assert guard.dns_reason("fill.papermc.io") is None
    _as(monkeypatch, "MINECRAFT", "PAPER", "play")
    assert guard.dns_reason("fill.papermc.io") == "denied"


def test_a_variant_gets_only_its_own_loader(monkeypatch):
    _as(monkeypatch, "MINECRAFT", "PAPER", "install")
    assert guard.is_approved("fill.papermc.io")
    assert not guard.is_approved("meta.fabricmc.net")
    _as(monkeypatch, "MINECRAFT", "FABRIC", "install")
    assert guard.is_approved("meta.fabricmc.net")
    assert not guard.is_approved("maven.minecraftforge.net")


def test_denied_names_are_refused_with_their_own_reason(monkeypatch):
    for stage in trust.STAGES:
        _as(monkeypatch, "PALWORLD", stage=stage)
        for entry in trust.DENY:
            if entry.applies_to("PALWORLD", "", stage):
                assert guard.dns_reason(entry.name.lstrip("*.")) == "denied", entry.name
    _as(monkeypatch, "PALWORLD")
    assert guard.dns_reason("example.org") == "unlisted_name"
    assert guard.dns_reason("api.epicgames.dev") is None


def test_a_suffix_does_not_match_a_lookalike_domain():
    # ".steamcontent.com" must not match "evilsteamcontent.com".
    assert not guard.is_approved("evilsteamcontent.com")
    assert not guard.is_approved("steamcontent.com.evil.example")


def test_search_suffixed_retries_are_refused():
    # Resolvers retry a refused name with the host's search domain appended; the
    # retry must not be forwarded either.
    assert guard.is_approved("api.epicgames.dev")
    assert not guard.is_approved("api.epicgames.dev.lan")


def test_a_suffix_does_not_match_its_bare_domain(monkeypatch):
    _as(monkeypatch, "PALWORLD", stage="install")
    assert guard.is_approved("cache1-xyz1.steamcontent.com")
    assert not guard.is_approved("steamcontent.com")


def test_the_guard_will_not_start_without_a_game(monkeypatch):
    monkeypatch.setattr(guard, "GAME", "")
    with pytest.raises(SystemExit):
        guard.configure()
    monkeypatch.setattr(guard, "GAME", "MINECRAFT")
    monkeypatch.setattr(guard, "VARIANT", "SPIGOT")
    with pytest.raises(SystemExit):
        guard.configure()


@pytest.mark.parametrize("stage", ["", "both", "PLAY "])
def test_the_guard_will_not_start_without_a_stage(monkeypatch, stage):
    # No default: guessing could hand a running server the install's list.
    monkeypatch.setattr(guard, "GAME", "PALWORLD")
    monkeypatch.setattr(guard, "VARIANT", "")
    monkeypatch.setattr(guard, "STAGE", stage)
    with pytest.raises(SystemExit, match="GUARD_STAGE"):
        guard.configure()


def test_the_guard_takes_only_its_stages_entries(monkeypatch):
    monkeypatch.setattr(guard, "GAME", "PALWORLD")
    monkeypatch.setattr(guard, "VARIANT", "")
    monkeypatch.setattr(guard, "STAGE", "play")
    guard.configure()
    assert {e.name for e in guard.POLICY.allow} == {e.name for e in trust.ALLOW
                                                  if e.applies_to("PALWORLD", "", "play")}
    assert all(e.stages == {"play"} for e in guard.POLICY.allow)


def test_refusal_is_an_nxdomain_answer_to_the_same_question():
    q = _query("telemetry.palworldgame.com")
    r = guard.refusal(q)
    qid, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", r[:12])
    assert qid == 0x1234
    assert flags & 0x8000  # a response
    assert flags & 0x000F == 3  # NXDOMAIN
    assert flags & 0x0100  # RD echoed
    assert (qd, an, ns, ar) == (1, 0, 0, 0)
    assert r[12:] == q[12:]


def test_answers_carry_their_ttls_for_both_families():
    q = _query("api.minecraftservices.com")
    reply = _answer(
        q,
        [
            (5, 60, b"\x03cdn\x00"),  # a CNAME before the addresses, as Mojang's are
            (1, 42, socket.inet_pton(socket.AF_INET, "203.0.113.7")),
            (28, 300, socket.inet_pton(socket.AF_INET6, "2001:db8::1")),
        ],
    )
    assert guard.parse_answers_ttl(reply) == [("203.0.113.7", 42), ("2001:db8::1", 300)]


def test_lookups_name_addresses_for_their_window(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(guard.time, "monotonic", lambda: clock[0])
    guard.note_lookup("CMP1-XYZ1.steamserver.net.", [("192.0.2.1", 5)])  # below the floor
    assert guard.names_for("192.0.2.1") == ["cmp1-xyz1.steamserver.net"]
    clock[0] += guard.WINDOW_MIN - 1
    assert guard.names_for("192.0.2.1") == ["cmp1-xyz1.steamserver.net"]
    clock[0] += 2
    assert guard.names_for("192.0.2.1") == []


def test_ruleset_lets_only_the_guard_reach_its_upstreams():
    rules = guard.ruleset(["172.19.255.53", "1.1.1.1"])
    assert "policy drop" in rules
    assert "ip daddr { 172.19.255.53, 1.1.1.1 } udp sport 600-999 udp dport 53 accept" in rules
    # The source-port mark only means anything if the world cannot bind there.
    assert guard.SPORT_HI < 1024
    # Rejected, not just dropped, so software fails fast instead of hanging.
    assert "reject with tcp reset" in rules


def test_log_only_ruleset_accepts_what_enforcement_rejects():
    enforcing = guard.ruleset(["172.19.255.53"])
    logging_ = guard.ruleset(["172.19.255.53"], log_only=True)
    # Only the world's own fence loosens; the private chain refuses either way.
    out = logging_[logging_.index("chain out"):]
    assert "reject" in enforcing[enforcing.index("chain out"):] and "reject" not in out
    # Still counted, so "would have blocked" stays measurable.
    assert "counter name blocked" in logging_
    # The upstream rule is unchanged: log-only loosens the fence, not who may
    # talk to the resolvers.
    assert "udp sport 600-999" in logging_


@pytest.mark.parametrize("log_only", [False, True])
def test_no_mode_lets_a_world_reach_a_private_network(log_only):
    rules = guard.ruleset(["172.19.255.53", "1.1.1.1"], log_only=log_only, report=("172.19.255.53", 5354))
    out = rules[rules.index("chain out"):]
    lan = out.index("192.168.0.0/16")
    # The guard's own resolver and report port sit in a private range: they
    # are accepted first, and only from the guard's source ports.
    assert out.index("udp dport 53 accept") < lan
    assert out.index("udp dport 5354 accept") < lan
    # Before the gate's marked leg, so a name answering with a LAN address is
    # refused too, and before log-only's final accept.
    assert lan < out.index(f"meta mark {guard.MARK:#x}")
    assert "fe80::/10" in out and "10.0.0.0/8" in out
    private = rules[rules.index("chain private"):rules.index("chain out")]
    assert "reject" in private


@pytest.mark.parametrize(
    "addr, private",
    [("192.168.0.10", True), ("10.1.2.3", True), ("172.19.0.5", True), ("169.254.169.254", True),
     ("100.100.1.1", True), ("fd00::1", True), ("fe80::1", True), ("8.8.8.8", False),
     ("2606:4700::1111", False), ("not-an-ip", False)],
)
def test_the_gate_knows_a_private_address(addr, private):
    assert guard.is_private(addr) is private


def test_the_gate_refuses_to_relay_to_a_private_address(capsys):
    class W:
        closed = False

        def close(self):
            self.closed = True

    w = W()
    asyncio.run(guard._relay(None, w, "192.168.0.10", 8080, b""))
    assert w.closed
    assert "private_address" in capsys.readouterr().out


def test_a_stage_with_no_ports_still_makes_a_valid_ruleset():
    # nft refuses an empty set; nothing marked may leave instead.
    rules = guard.ruleset(["172.19.255.53"], ports=())
    assert "{  }" not in rules
    assert f"meta mark {guard.MARK:#x} tcp dport" not in rules
    assert "reject with tcp reset" in rules


def test_only_the_guard_can_reach_the_refusal_report_port():
    rules = guard.ruleset(["172.19.255.53"], report=("172.19.255.53", 5354))
    assert "ip daddr 172.19.255.53 udp sport 600-999 udp dport 5354 accept" in rules
    assert "dport 5354" not in guard.ruleset(["172.19.255.53"])


def test_the_dns_observer_records_a_guard_report():
    import resolver

    resolver._refused.clear()
    record = {
        "world": "test", "game": "PALWORLD", "variant": "", "mode": "log",
        "layer": "gate", "reason": "port_not_allowed", "proto": "tls",
        "name": "API.Steampowered.com.", "dst": "192.0.2.9", "port": 8443,
        "suggest": "_e(...)",
    }
    resolver.record_refused("172.19.0.7", json.dumps(record).encode())
    resolver.record_refused("172.19.0.8", json.dumps({**record, "mode": "enforce", "world": "b"}).encode())
    resolver.record_refused("172.19.0.7", b"not json")
    # The old shape, from a guard not yet replaced, still counts.
    resolver.record_refused("172.19.0.7", b'{"name": "cdn.new-host.example", "mode": "log"}')
    rows = {r["name"]: r for r in resolver.snapshot()["refused"]}
    row = rows["api.steampowered.com"]
    assert row["count"] == 2 and row["worlds"] == ["test", "b"]
    assert sorted(row["modes"]) == ["enforce", "log"]
    assert (row["game"], row["layer"], row["reason"], row["proto"], row["port"]) == (
        "PALWORLD", "gate", "port_not_allowed", "tls", 8443
    )
    assert row["suggest"] == "_e(...)"
    assert rows["cdn.new-host.example"]["layer"] == "dns"


def test_the_dns_observer_keeps_each_stages_refusals_apart():
    # They go in different lists, so one row for both would hide which.
    import resolver

    resolver._refused.clear()
    record = {"world": "w", "game": "PALWORLD", "mode": "enforce", "layer": "dns",
              "reason": "unlisted_name", "name": "new.example.com"}
    for stage in ("install", "play", "play"):
        resolver.record_refused("172.19.0.7", json.dumps({**record, "stage": stage}).encode())
    rows = sorted((r["stage"], r["count"]) for r in resolver.snapshot()["refused"])
    assert rows == [("install", 1), ("play", 2)]


# --- web gate ----------------------------------------------------------------

import webgate  # noqa: E402


def _hello(sni=None, ech=False):
    """A minimal hand-built ClientHello, for cases a real client won't produce."""
    exts = b""
    if sni is not None:
        name = sni.encode()
        entry = b"\x00" + struct.pack("!H", len(name)) + name
        body = struct.pack("!H", len(entry)) + entry
        exts += struct.pack("!HH", 0x0000, len(body)) + body
    if ech:
        exts += struct.pack("!HH", 0xFE0D, 4) + b"\x00\x01\x02\x03"
    hello = (
        b"\x03\x03" + b"\x00" * 32  # version, random
        + b"\x00"  # session id
        + b"\x00\x02\x13\x01"  # one cipher suite
        + b"\x01\x00"  # compression: null
        + struct.pack("!H", len(exts)) + exts
    )
    hs = b"\x01" + len(hello).to_bytes(3, "big") + hello
    return b"\x16\x03\x01" + struct.pack("!H", len(hs)) + hs


def test_reads_the_sni_from_a_real_client_hello():
    status, sni, ech, alpn = webgate.parse_client_hello(guard.client_hello("Piston-Data.Mojang.com"))
    assert (status, sni, ech) == (webgate.OK, "piston-data.mojang.com", False)
    assert alpn == ()  # the self-test's context offers none


def test_waits_for_the_rest_of_a_split_hello():
    full = guard.client_hello("api.epicgames.dev")
    assert webgate.parse_client_hello(full[:20])[0] == webgate.NEED_MORE
    assert webgate.parse_client_hello(full)[1] == "api.epicgames.dev"


def test_a_hello_split_across_two_records_is_reassembled():
    one = _hello("auth.game.pocketpair.jp")
    hs = one[5:]
    two = (
        b"\x16\x03\x01" + struct.pack("!H", 10) + hs[:10]
        + b"\x16\x03\x01" + struct.pack("!H", len(hs) - 10) + hs[10:]
    )
    assert webgate.parse_client_hello(two)[1] == "auth.game.pocketpair.jp"


def test_encrypted_client_hello_is_detected():
    status, sni, ech, _ = webgate.parse_client_hello(_hello("cloudflare-ech.com", ech=True))
    assert status == webgate.OK and ech is True
    assert guard.decide(sni, "tls", 443, ech) == "ech"


def test_no_name_and_bare_addresses_are_refused():
    assert webgate.parse_client_hello(_hello(None))[1] is None
    assert guard.decide(None, "tls", 443) == "no_name"
    assert guard.decide("104.18.124.108", "tls", 443) == "no_name"
    assert guard.decide("api.epicgames.dev", "tls", 443) is None
    assert webgate.parse_client_hello(b"GET / HTTP/1.1\r\n")[0] == webgate.NOT_TLS


def test_every_host_on_a_kept_alive_connection_is_seen():
    scan = webgate.HostScanner()
    assert scan.feed(b"GET / HTTP/1.1\r\nHost: steamconnecttest.com\r\n\r\n") == [
        "steamconnecttest.com"
    ]
    # The second request names another site; split mid-header across chunks.
    assert scan.feed(b"GET /x HTTP/1.1\r\nHo") == []
    assert scan.feed(b"st: Evil.Example:80\r\n\r\n") == ["evil.example"]


def test_every_tcp_port_goes_to_the_gate_and_only_dns_may_use_udp():
    rules = guard.ruleset(["172.19.255.53"], ((80, 80), (443, 443), (27018, 27038)))
    assert f"meta l4proto tcp redirect to :{guard.GATE_PORT}" in rules
    # The gate's own upstream leg, only on the game's ports.
    assert f"meta mark {guard.MARK:#x} tcp dport {{ 80, 443, 27018-27038 }} accept" in rules
    # Every UDP flow but the guard's DNS falls to the drop sets and the reject.
    assert rules.count("udp dport") == 1
    assert "add @refused4" in rules and "add @refused6" in rules
    logging_ = guard.ruleset(["172.19.255.53"], log_only=True)
    assert f"meta mark {guard.MARK:#x} accept" in logging_
    assert "add @refused4" in logging_  # would-refuse is still recorded


def test_the_gate_decides_by_name_protocol_and_port(monkeypatch):
    d = guard.decide
    _as(monkeypatch, "PALWORLD", stage="install")
    assert d("cmp1-xyz1.steamserver.net", "tls", 27020) is None
    assert d("cmp1-xyz1.steamserver.net", "tls", 27039) == "port_not_allowed"
    assert d("client-update.steamstatic.com", "http", 80) == "proto_not_allowed"
    assert d("api.steampowered.com", "tls", 443) == "denied"
    assert d("telemetry.palworldgame.com", "tls", 443) == "denied"
    assert d("example.org", "tls", 443) == "unlisted_name"
    _as(monkeypatch, "PALWORLD")
    assert d("auth.game.pocketpair.jp", "tls", 443) is None
    assert d("steamconnecttest.com", "http", 80) == "denied"
    assert d("cmp1-xyz1.steamserver.net", "tls", 27020) == "unlisted_name"
    _as(monkeypatch, "MINECRAFT", "VANILLA", "install")
    assert d("cmp1-xyz1.steamserver.net", "tls", 27020) == "unlisted_name"


def test_stream_entries_need_the_address_looked_up_as_their_name(monkeypatch):
    entry = trust.Entry(frozenset({"PALWORLD"}), "relay.example.net", "stream", ((7000, 7000),), "test",
                        stages=frozenset({"play"}))
    monkeypatch.setattr(trust, "ALLOW", trust.ALLOW + (entry,))
    monkeypatch.setattr(guard, "POLICY", trust.Policy("PALWORLD", stage="play"))
    assert not guard.stream_allowed("192.0.2.7", 7000)
    guard.note_lookup("relay.example.net", [("192.0.2.7", 60)])
    assert guard.stream_allowed("192.0.2.7", 7000)
    assert not guard.stream_allowed("192.0.2.7", 7001)
    assert not guard.stream_allowed("192.0.2.8", 7000)


def test_a_refusal_carries_enough_to_write_its_entry(capsys, monkeypatch):
    _as(monkeypatch, "PALWORLD", stage="install")
    guard.note_lookup("cmp1-xyz1.steamserver.net", [("192.0.2.40", 60)])
    rec = guard.refuse(
        "gate", "port_not_allowed", proto="tls", name="cmp1-xyz1.steamserver.net",
        dst="192.0.2.40", port=27039, alpn=("http/1.1",),
    )
    assert rec["names_for_dst"] == ["cmp1-xyz1.steamserver.net"]
    assert rec["alpn"] == ["http/1.1"]
    # Which list the entry goes in.
    assert rec["stage"] == "install"
    assert rec["suggest"] == (
        "_e({'PALWORLD'}, 'cmp1-xyz1.steamserver.net', 'tls', ((27039, 27039),), 'TODO')"
    )
    printed = capsys.readouterr().out
    assert printed.startswith("refused {") and '"port":27039' in printed


def test_a_suggested_entry_fails_validation_until_it_has_a_reason():
    rec = guard.refuse("gate", "unlisted_name", proto="tls", name="new.example.com", dst="192.0.2.1", port=443)
    assert rec is None  # the self-test address is never reported
    rec = guard.refuse("gate", "unlisted_name", proto="tls", name="new.example.com", dst="192.0.2.50", port=443)
    entry = eval(rec["suggest"], {"_e": trust._e})  # noqa: S307 - our own output
    with pytest.raises(ValueError, match="real reason"):
        trust.validate(trust.ALLOW + (entry,), trust.DENY)
    fixed = replace(entry, reason="A real reason.")
    with pytest.raises(ValueError, match="exactly one"):
        # Pasting it into a stage's list is what gives it the stage.
        trust.validate(trust.ALLOW + (fixed,), trust.DENY)
    trust.validate(trust.ALLOW + (replace(fixed, stages=frozenset({rec["stage"]})),), trust.DENY)


def test_a_nameless_refusal_is_named_by_the_worlds_lookups():
    guard.note_lookup("relay.example.net", [("192.0.2.60", 60)])
    rec = guard.refuse("gate", "proto_not_allowed", proto="stream", dst="192.0.2.60", port=7000,
                       first_bytes=b"\x00\x01binary")
    assert "relay.example.net', 'stream', ((7000, 7000),)" in rec["suggest"]
    assert rec["first_bytes"] == "0001" + b"binary".hex()
    udp = guard.refuse("firewall", "not_tcp", proto="udp", dst="192.0.2.60", port=3478)
    assert "suggest" not in udp and "cannot be an entry" in udp["hint"]


def test_refusals_are_deduplicated_and_reported_once(monkeypatch):
    sent: list[bytes] = []
    monkeypatch.setattr(guard, "_send_report", sent.append)
    for _ in range(150):
        guard.refuse("dns", "unlisted_name", name="new.example.com", qtype=1)
    assert len(sent) == 1
    rec = json.loads(sent[0])
    assert rec["layer"] == "dns" and "guessed tls/443" in rec["hint"]
    assert guard._refusals[("dns", "unlisted_name", None, "new.example.com", None)] == 150


def test_firewall_drops_are_read_from_nft_json():
    listing = {"nftables": [{"metainfo": {}}, {"set": {"name": "refused4", "elem": [
        {"elem": {"val": {"concat": ["192.0.2.1", "udp", 9]}, "expires": 3599}},
        {"elem": {"val": {"concat": ["198.51.100.4", 17, 3478]}}},
    ]}}]}
    assert guard.firewall_elements(listing) == [("192.0.2.1", "udp", 9), ("198.51.100.4", "udp", 3478)]
    assert guard.firewall_reason("udp") == "not_tcp"
    assert guard.firewall_reason("tcp") == "port_not_allowed"


def test_ech_key_records_are_answered_empty_not_nxdomain():
    q = _query("api.epicgames.dev", qtype=guard.QTYPE_HTTPS)
    flags = struct.unpack("!H", guard.refusal(q, guard.NOERROR)[2:4])[0]
    assert flags & 0x000F == 0  # NOERROR: the name exists, the record does not
