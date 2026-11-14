"""Tests for the PalWorldSettings.ini parser/renderer.

The load-bearing property is round-trip fidelity: this file is the source of
truth for a world's rules, so anything we fail to preserve is a setting silently
reset on the owner's next start.
"""

from __future__ import annotations

import pytest

from open_worlds.common.games.palworld import settings_file as ps

# A real file's shape, including the two things that break a naive comma split:
# a quoted value and a parenthesised list, each containing commas.
SAMPLE = (
    "[/Script/Pal.PalGameWorldSettings]\n"
    "OptionSettings=(Difficulty=None,DayTimeSpeedRate=1.000000,"
    'ServerName="my server",CrossplayPlatforms=(Steam,Xbox,PS5,Mac),'
    'ServerDescription="",BaseCampWorkerMaxNum=15,bUseAuth=True)\n'
)


def test_round_trip_is_byte_exact():
    """Re-serialising an untouched file must reproduce it exactly."""
    assert ps.serialize(ps.parse(SAMPLE)) == SAMPLE


def test_parses_values_containing_commas():
    parsed = ps.parse(SAMPLE)
    assert parsed["CrossplayPlatforms"] == "(Steam,Xbox,PS5,Mac)"
    assert parsed["ServerName"] == '"my server"'
    assert parsed["ServerDescription"] == '""'


def test_unknown_keys_survive_a_render():
    """Keys we have never heard of are the whole reason the file is truth."""
    text, _ = ps.render(SAMPLE, {"ServerName": '"other"'})
    parsed = ps.parse(text)
    assert parsed["CrossplayPlatforms"] == "(Steam,Xbox,PS5,Mac)"
    assert parsed["Difficulty"] == "None"
    assert parsed["ServerName"] == '"other"'


def test_render_without_existing_settings_emits_only_overrides():
    """A new world gets our keys; the game supplies real vanilla defaults."""
    text, _ = ps.render(None, {"ServerName": '"new"'})
    assert ps.parse(text) == {"ServerName": '"new"'}


@pytest.mark.parametrize("blank", ["", "   ", "\n\n"])
def test_blank_file_is_treated_as_no_settings(blank):
    text, _ = ps.render(blank, {"ServerName": '"new"'})
    assert ps.parse(text) == {"ServerName": '"new"'}


def test_malformed_file_raises_rather_than_resetting_the_world():
    """Failing to parse must not look like "this world has no settings"."""
    with pytest.raises(ps.SettingsError):
        ps.parse("[/Script/Pal.PalGameWorldSettings]\nOptionSettings=Difficulty=None\n")
    with pytest.raises(ps.SettingsError):
        ps.parse("nothing to see here\n")


@pytest.mark.parametrize(
    "hostile",
    [
        'a",b',  # closes the quote, injects a new setting
        "a,b",  # splits the value in two
        "a)b",  # closes OptionSettings early
        "a\nb",  # breaks the single-line format
    ],
)
def test_quote_strips_characters_that_would_corrupt_the_line(hostile):
    """One stray quote in a description would corrupt every later setting."""
    text, _ = ps.render(SAMPLE, {"ServerDescription": ps.quote(hostile)})
    parsed = ps.parse(text)
    # Still parses, still has every original key, and nothing was injected.
    assert parsed["bUseAuth"] == "True"
    assert parsed["BaseCampWorkerMaxNum"] == "15"
    assert len(parsed) == len(ps.parse(SAMPLE))


def test_clamp_lowers_values_over_a_ceiling_and_reports_them():
    settings = {"MaxBuildingLimitNum": "50000", "DayTimeSpeedRate": "2.000000"}
    out, lowered = ps.clamp(settings, {"MaxBuildingLimitNum": 20000})
    assert out["MaxBuildingLimitNum"] == "20000"
    assert lowered == ["MaxBuildingLimitNum"]
    # Untouched keys keep their exact text.
    assert out["DayTimeSpeedRate"] == "2.000000"


def test_clamp_treats_unlimited_sentinels_as_exceeding_the_ceiling():
    """0 and -1 mean unlimited for these keys, so a ceiling must catch them."""
    out, lowered = ps.clamp(
        {"MaxBuildingLimitNum": "0", "PhysicsActiveDropItemMaxNum": "-1"},
        {"MaxBuildingLimitNum": 20000, "PhysicsActiveDropItemMaxNum": 500},
    )
    assert out["MaxBuildingLimitNum"] == "20000"
    assert out["PhysicsActiveDropItemMaxNum"] == "500"
    assert sorted(lowered) == ["MaxBuildingLimitNum", "PhysicsActiveDropItemMaxNum"]


def test_no_ceilings_by_default_so_vanilla_behaviour_is_preserved():
    assert ps.DEFAULT_CEILINGS == {}
    settings = {"MaxBuildingLimitNum": "0"}
    out, lowered = ps.clamp(settings, ps.DEFAULT_CEILINGS)
    assert out == settings
    assert lowered == []


# --- Adapter integration: what actually reaches a world's data directory -----


def _palworld_spec(**kw):
    from open_worlds.common.orchestrator import WorldSpec

    base = dict(
        game="PALWORLD",
        world_id="w1",
        owner_id="o1",
        subdomain="demo",
        display_name="Demo",
        memory_mb=8192,
        max_players=16,
        admin_password="adminpw",
        server_password="joinpw",
        settings={"server_description": kw.pop("server_description", "a nice world")},
        game_port=8213,
    )
    base.update(kw)
    return WorldSpec(**base)


def _render(tmp_path, **kw):
    from open_worlds.common.games import adapter_for
    from open_worlds.common.games.palworld.adapter import SETTINGS_RELPATH

    adapter = adapter_for("PALWORLD")
    adapter.prepare_data_dir(_palworld_spec(**kw), str(tmp_path))
    return ps.parse((tmp_path / SETTINGS_RELPATH).read_text())


def test_env_and_settings_agree_on_the_admin_surface(tmp_path):
    """The image's own scripts read these from the environment, not the file.

    helper_functions.sh builds the REST URL from REST_API_PORT and authenticates
    with admin:ADMIN_PASSWORD, so if the two halves disagree the image's player
    logging and backup hooks talk to the wrong port or with the wrong password.
    """
    from open_worlds.common.games import adapter_for

    spec = _palworld_spec()
    env = adapter_for("PALWORLD").environment(spec)
    rendered = _render(tmp_path)

    assert rendered["AdminPassword"] == ps.quote(env["ADMIN_PASSWORD"])
    assert rendered["RESTAPIPort"] == env["REST_API_PORT"]
    assert rendered["RESTAPIEnabled"].lower() == env["REST_API_ENABLED"].lower()
    assert rendered["RCONEnabled"].lower() == env["RCON_ENABLED"].lower()


def test_owner_fields_land_in_the_settings_file(tmp_path):
    rendered = _render(tmp_path)
    assert rendered["ServerPassword"] == '"joinpw"'
    assert rendered["ServerDescription"] == '"a nice world"'
    assert rendered["ServerPlayerMaxNum"] == "16"
    # Named for the immutable subdomain, never the renameable display name.
    assert rendered["ServerName"] == '"demo"'


def test_a_worlds_own_settings_survive_but_forced_keys_do_not(tmp_path):
    """An uploaded file keeps its rules and loses the keys we must control."""
    from open_worlds.common.games.palworld.adapter import SETTINGS_RELPATH

    path = tmp_path / SETTINGS_RELPATH
    path.parent.mkdir(parents=True)
    path.write_text(
        "[/Script/Pal.PalGameWorldSettings]\n"
        "OptionSettings=(BaseCampWorkerMaxNum=24,ExpRate=3.000000,"
        'PublicPort=8212,bUseAuth=False,AdminPassword="stolen",'
        "SomeKeyWeHaveNeverHeardOf=7)\n"
    )
    rendered = _render(tmp_path)

    # The owner's rules, including a key the platform knows nothing about.
    assert rendered["BaseCampWorkerMaxNum"] == "24"
    assert rendered["ExpRate"] == "3.000000"
    assert rendered["SomeKeyWeHaveNeverHeardOf"] == "7"
    # Ours, forced back regardless of what the file asked for.
    assert rendered["PublicPort"] == "8211"
    assert rendered["bUseAuth"] == "True"
    assert rendered["AdminPassword"] == '"adminpw"'


def test_a_new_world_gets_a_settings_file_with_our_keys(tmp_path):
    """Nothing on disk yet must still produce a usable, REST-reachable server."""
    rendered = _render(tmp_path)
    assert rendered["RESTAPIEnabled"] == "True"
    assert rendered["bUseAuth"] == "True"


def test_hostile_description_cannot_corrupt_the_file(tmp_path):
    """Defence in depth: the API rejects these, the renderer strips them."""
    rendered = _render(tmp_path, server_description='pwn",bUseAuth=False,x="')
    assert rendered["bUseAuth"] == "True"
    assert "," not in rendered["ServerDescription"]


def test_rendering_twice_is_stable(tmp_path):
    """Every start re-renders, so the file must not drift on repeat calls."""
    from open_worlds.common.games.palworld.adapter import SETTINGS_RELPATH

    first = _render(tmp_path)
    second = _render(tmp_path)
    assert first == second
    assert (tmp_path / SETTINGS_RELPATH).read_text().endswith(")\n")


# --- Reading settings back out of an upload ---------------------------------


def _archive(tmp_path, body: str | None, kind: str = "zip"):
    import tarfile
    import zipfile

    inner = "world/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini"
    out = tmp_path / f"upload.{kind}"
    if kind == "zip":
        with zipfile.ZipFile(out, "w") as zf:
            zf.writestr("world/Pal/Saved/SaveGames/0/abc/Level.sav", "x")
            if body is not None:
                zf.writestr(inner, body)
    else:
        src = tmp_path / "payload.ini"
        src.write_text(body or "")
        with tarfile.open(out, "w:gz") as tf:
            if body is not None:
                tf.add(src, arcname=inner)
    return str(out)


def _palworld_adapter():
    from open_worlds.common.games import adapter_for

    return adapter_for("PALWORLD")


@pytest.mark.parametrize("kind", ["zip", "tar.gz"])
def test_settings_are_read_out_of_an_upload(tmp_path, kind):
    """Preflight has to show the owner what an import will apply."""
    body = (
        "[/Script/Pal.PalGameWorldSettings]\n"
        "OptionSettings=(BaseCampWorkerMaxNum=24,ServerPlayerMaxNum=8,"
        'ServerPassword="fromfile",ServerDescription="imported world")\n'
    )
    imported = _palworld_adapter().settings_from_archive(_archive(tmp_path, body, kind))
    assert imported is not None
    assert imported.values["BaseCampWorkerMaxNum"] == "24"
    assert imported.max_players == 8
    assert imported.server_password == "fromfile"
    assert imported.settings == {"server_description": "imported world"}


def test_an_upload_without_settings_is_not_an_error(tmp_path):
    """A world with no config is still a world worth importing."""
    assert _palworld_adapter().settings_from_archive(_archive(tmp_path, None)) is None


def test_malformed_settings_in_an_upload_are_ignored_not_fatal(tmp_path):
    assert _palworld_adapter().settings_from_archive(_archive(tmp_path, "garbage\n")) is None


def test_games_without_a_settings_file_report_nothing(tmp_path):
    from open_worlds.common.games import adapter_for

    adapter = adapter_for("MINECRAFT")
    assert adapter.settings_from_archive(_archive(tmp_path, "whatever")) is None


# --- Pointing DedicatedServerName at the save directory on disk --------------


def _make_world(tmp_path, *names):
    from open_worlds.common.games.palworld.adapter import SAVEGAMES_RELPATH

    root = tmp_path / SAVEGAMES_RELPATH
    for name in names:
        (root / name).mkdir(parents=True)
        (root / name / "Level.sav").write_bytes(b"x")
    return root


def _gus(tmp_path):
    from open_worlds.common.games.palworld.adapter import GAME_USER_SETTINGS_RELPATH

    return tmp_path / GAME_USER_SETTINGS_RELPATH


def _point(tmp_path, **kw):
    from open_worlds.common.games import adapter_for

    adapter_for("PALWORLD").prepare_data_dir(_palworld_spec(**kw), str(tmp_path))


def test_one_save_directory_is_pointed_at(tmp_path):
    _make_world(tmp_path, "16540A98")
    _point(tmp_path)
    assert "DedicatedServerName=16540A98" in _gus(tmp_path).read_text()


def test_an_imported_world_is_pointed_at_too(tmp_path):
    """bare_world_placement lands a save in `imported`, and nothing used to
    point at it -- so that path could only ever produce an empty world."""
    _make_world(tmp_path, "imported")
    _point(tmp_path)
    assert "DedicatedServerName=imported" in _gus(tmp_path).read_text()


def test_a_wrong_pointer_is_corrected(tmp_path):
    """The directory is the truth; the key bends to it, not the other way."""
    _make_world(tmp_path, "REAL")
    gus = _gus(tmp_path)
    gus.parent.mkdir(parents=True)
    gus.write_text(
        "[/Script/Pal.PalGameLocalSettings]\nDedicatedServerName=STALE\nDLSSMode=Performance\n"
    )
    _point(tmp_path)
    text = gus.read_text()
    assert "DedicatedServerName=REAL" in text
    assert "STALE" not in text
    # The rest of the file is the game's, and must survive untouched.
    assert "DLSSMode=Performance" in text


def test_a_new_world_is_left_alone(tmp_path):
    """No save yet: the game creates the directory and writes the key itself."""
    _point(tmp_path)
    assert not _gus(tmp_path).exists()


def test_two_save_directories_refuse_to_start(tmp_path):
    """Guessing would mean players building on a world nobody chose."""
    _make_world(tmp_path, "AAA", "BBB")
    with pytest.raises(RuntimeError, match="exactly one is required"):
        _point(tmp_path)


def test_two_save_directories_refuse_even_when_the_pointer_resolves(tmp_path):
    """The invariant is exactly one, not "one we can still find".

    Starting anyway whenever the pointer happened to resolve would let a stray
    second directory persist unnoticed, which is how you end up unable to tell
    which world people have actually been playing on.
    """
    _make_world(tmp_path, "AAA", "BBB")
    gus = _gus(tmp_path)
    gus.parent.mkdir(parents=True)
    gus.write_text("[/Script/Pal.PalGameLocalSettings]\nDedicatedServerName=BBB\n")
    with pytest.raises(RuntimeError, match="exactly one is required"):
        _point(tmp_path)
