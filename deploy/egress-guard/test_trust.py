"""The egress policy's own rules: every entry scoped and explained.

Run from deploy/egress-guard:  python -m pytest
"""

import ipaddress
from dataclasses import replace

import pytest
import trust
from trust import DENY, STAGES, Policy, _e, validate

MC = frozenset({"MINECRAFT"})
PAL = frozenset({"PALWORLD"})


def test_the_shipped_lists_validate():
    validate()


@pytest.mark.parametrize("reason", ["", "TODO", "todo", " tbd ", "?"])
def test_every_entry_needs_a_real_reason(reason):
    with pytest.raises(ValueError, match="real reason"):
        validate((_e(PAL, "new.example.com", "tls", ((443, 443),), reason),), ())


@pytest.mark.parametrize(
    "entry, message",
    [
        (_e(PAL, "a.example.com", None, ((443, 443),), "r"), "protocol"),
        (_e(PAL, "a.example.com", "udp", ((443, 443),), "r"), "protocol"),
        (_e(PAL, "a.example.com", "tls", (), "r"), "ports"),
        (_e(PAL, "a.example.com", "tls", ((0, 443),), "r"), "ports"),
        (_e(PAL, "a.example.com", "tls", ((443, 80),), "r"), "ports"),
        (_e({"TERRARIA"}, "a.example.com", "tls", ((443, 443),), "r"), "game"),
        (_e(PAL, "a.example.com", "tls", ((443, 443),), "r", {"PAPER"}), "variant"),
        (_e(PAL, "A.example.com", "tls", ((443, 443),), "r"), "lower-case"),
        (_e(PAL, "*.com", "tls", ((443, 443),), "r"), "wildcard"),
        (_e(PAL, "a.*.example.com", "tls", ((443, 443),), "r"), "wildcard"),
        (_e(PAL, "1.1.1.1", "tls", ((443, 443),), "r"), "names, not addresses"),
    ],
)
def test_allow_entries_are_fully_scoped(entry, message):
    with pytest.raises(ValueError, match=message):
        validate((entry,), ())


def test_deny_entries_carry_no_protocol_or_ports():
    with pytest.raises(ValueError, match="DENY"):
        validate((), (_e(PAL, "x.example.com", "tls", ((443, 443),), "r"),))


def _staged(entry, stage="play"):
    """An entry as loaded from a stage's list."""
    return replace(entry, stages=frozenset({stage}))


def test_duplicates_are_refused():
    e = _staged(_e(PAL, "a.example.com", "tls", ((443, 443),), "r"))
    with pytest.raises(ValueError, match="duplicate"):
        validate((e, e), ())
    # The same name for another protocol is a different entry.
    validate((e, _staged(_e(PAL, "a.example.com", "http", ((80, 80),), "r"))), ())
    # So is the same name in the other stage's list.
    validate((e, _staged(e, "install")), ())


def test_every_allowed_entry_is_in_exactly_one_stage():
    assert all(len(e.stages) == 1 for e in trust.ALLOW)
    e = _e(PAL, "a.example.com", "tls", ((443, 443),), "r")
    with pytest.raises(ValueError, match="exactly one"):
        validate((e,), ())
    with pytest.raises(ValueError, match="exactly one"):
        validate((replace(e, stages=frozenset(STAGES)),), ())


def test_a_denial_cannot_overlap_what_the_same_stage_allows():
    allowed = _staged(_e(PAL, "*.example.com", "tls", ((443, 443),), "r"), "install")
    for denied in (
        _e(PAL, "cdn.example.com", None, (), "r"),
        _e(PAL, "cdn.example.com", None, (), "r", stages={"install"}),
        _e(PAL, "*.example.com", None, (), "r"),
    ):
        with pytest.raises(ValueError, match="also allowed"):
            validate((allowed,), (denied,))
    # Denied only while playing: the install's entry stands.
    validate((allowed,), (_e(PAL, "cdn.example.com", None, (), "r", stages={"play"}),))
    # Another game's entry is no overlap.
    validate((allowed,), (_e(MC, "cdn.example.com", None, (), "r"),))


def test_a_denial_names_real_stages():
    with pytest.raises(ValueError):
        validate((), (_e(PAL, "x.example.com", None, (), "r", stages={"boot"}),))


def test_a_game_module_defines_its_lists_per_stage(tmp_path, monkeypatch):
    # A module still written the old way would otherwise load with nothing.
    (tmp_path / "egress.py").write_text(
        "from trust import _e\nGAME = 'X'\nVARIANTS = frozenset()\nALLOW = ()\nDENY = ()\n"
    )
    with pytest.raises(ValueError, match="INSTALL"):
        trust._load_game_file(tmp_path / "egress.py")


def test_no_entry_is_an_address():
    for e in trust.ALLOW + DENY:
        with pytest.raises(ValueError):
            ipaddress.ip_network(e.name.lstrip("*."), strict=False)


def test_no_suffix_anyone_can_register_under():
    # An Azure CDN endpoint name is chosen by whoever signs up; permitting the
    # suffix would permit them.
    assert not any(e.name.endswith("azureedge.net") for e in trust.ALLOW)


def test_policies_are_per_game_variant_and_stage():
    pal, paper = Policy("PALWORLD", stage="install"), Policy("MINECRAFT", "PAPER", "install")
    assert pal.match("cmp1-xyz1.steamserver.net", "tls", 27020)
    assert not paper.name_allowed("cmp1-xyz1.steamserver.net")
    assert paper.match("fill.papermc.io", "tls", 443)
    assert not Policy("MINECRAFT", "FABRIC", "install").name_allowed("fill.papermc.io")
    # A world whose variant is unknown gets only what every variant gets.
    assert Policy("MINECRAFT", stage="play").name_allowed("sessionserver.mojang.com")
    assert not Policy("MINECRAFT", stage="install").name_allowed("fill.papermc.io")
    assert pal.ports == ((443, 443), (27018, 27038))
    # A running server: HTTPS only, no Steam ports.
    assert Policy("PALWORLD", stage="play").ports == ((443, 443),)
    assert Policy("MINECRAFT", "VANILLA", "play").ports == ((443, 443),)


def test_the_running_server_gets_none_of_the_installs_hosts():
    for game, variants in trust.GAMES.items():
        for variant in sorted(variants) or [""]:
            play = Policy(game, variant, "play")
            for e in Policy(game, variant, "install").allow:
                if not any(p.name == e.name for p in play.allow):
                    assert not play.name_allowed(e.name.lstrip("*.")), (game, variant, e.name)


def test_unknown_games_and_variants_are_refused():
    with pytest.raises(ValueError):
        Policy("TERRARIA")
    with pytest.raises(ValueError):
        Policy("PALWORLD", "PAPER")


def test_deny_is_per_game_too():
    assert Policy("PALWORLD").denied("telemetry.palworldgame.com")
    assert not Policy("MINECRAFT").denied("telemetry.palworldgame.com")
    assert Policy("MINECRAFT").denied("bstats.org")
    # Each game denies it in its own file.
    assert Policy("MINECRAFT").denied("api.github.com")
    assert Policy("PALWORLD").denied("api.github.com")


def test_each_game_file_defines_its_own_entries_only():
    for e in trust.ALLOW + DENY:
        assert len(e.games) == 1, e.name


def test_the_union_leaves_out_a_single_stages_denials():
    union = Policy()
    assert union.name_allowed("cmp1-xyz1.steamserver.net")
    # Denied by one stage (Forge's install), allowed by the other: the union,
    # which cannot tell them apart, is not the place to call it denied.
    assert not union.denied("sessionserver.mojang.com")
    assert not union.denied("fill.papermc.io")
    assert union.denied("bstats.org")


def test_a_stages_denials_apply_in_that_stage():
    assert Policy("MINECRAFT", "PAPER", "play").denied("fill.papermc.io")
    assert not Policy("MINECRAFT", "PAPER", "install").denied("fill.papermc.io")
    assert Policy("MINECRAFT", "FORGE", "install").denied("maven.creeperhost.net")
    assert not Policy("MINECRAFT", "FORGE", "play").denied("maven.creeperhost.net")


def test_suggest_writes_a_pastable_entry():
    s = trust.suggest("MINECRAFT", "FORGE", "new.example.com", "tls", 443)
    entry = eval(s, {"_e": _e})  # noqa: S307 - our own output
    assert entry == _e(MC, "new.example.com", "tls", ((443, 443),), "TODO", {"FORGE"})
