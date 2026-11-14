"""Archive and data-root inspection and garbage classification, for both games.

A world is its save; everything else is regenerable or executable, and an
upload may never bring code the server would load.
"""

from __future__ import annotations

import tarfile
import zipfile

import pytest

from open_worlds.common.games import adapter_for
from open_worlds.common.upload_inspect import (
    classify,
    inspect_archive,
    inspect_dir,
    prunable_report,
)

MC = adapter_for("MINECRAFT").import_policy
PAL = adapter_for("PALWORLD").import_policy


def _zip(path, files: dict[str, str]):
    with zipfile.ZipFile(path, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)


# A realistic upload per game: the world beside the bits the image regenerates.
_MC_DIR = {
    "data/world/level.dat": "lvl",
    "data/server.properties": "props",
    "data/mods/cool-mod.jar": "JAR",
    "data/server.jar": "SERVER",
    "data/logs/latest.log": "log",
    "data/libraries/lib.jar": "lib",
}
_PAL_DIR = {
    "data/Pal/Saved/SaveGames/0/A1B2/Level.sav": "lvl",
    "data/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini": "cfg",
    "data/Pal/Binaries/Linux/PalServer-Linux-Shipping": "BIN",
    "data/steamapps/appmanifest.acf": "acf",
    "data/logs/PalServer.log": "log",
}


@pytest.mark.parametrize(
    "policy,files,keep,drop",
    [
        (MC, _MC_DIR, {"world", "server.properties", "mods"}, {"server.jar", "logs", "libraries"}),
        (PAL, _PAL_DIR, {"Pal"}, {"steamapps", "logs"}),
    ],
    ids=["minecraft", "palworld"],
)
def test_classify_keeps_the_world_and_flags_the_install(tmp_path, policy, files, keep, drop):
    archive = tmp_path / "srv.zip"
    _zip(archive, files)
    allowed, unknown = classify(inspect_archive(str(archive), policy).entries, policy)
    assert set(allowed) == keep
    assert set(unknown) == drop


@pytest.mark.parametrize(
    "policy,files,payload,kept",
    [
        (
            MC,
            {"data/world/level.dat": "x", "data/mods/evil.jar": "M"},
            "mods/evil.jar",
            "world/level.dat",
        ),
        (
            PAL,
            {
                "data/Pal/Saved/SaveGames/0/A1B2/Level.sav": "x",
                "data/Pal/Saved/Mods/evil_P.pak": "M",
            },
            "Pal/Saved/Mods/evil_P.pak",
            "Pal/Saved/SaveGames/0/A1B2/Level.sav",
        ),
    ],
    ids=["minecraft-jar", "palworld-pak"],
)
def test_loadable_code_is_stripped_from_inside_kept_dirs(tmp_path, policy, files, payload, kept):
    archive = tmp_path / "modded.zip"
    _zip(archive, files)
    payloads = set(inspect_archive(str(archive), policy).nested_payloads)
    assert payload in payloads
    assert kept not in payloads


def test_palworld_game_install_under_pal_is_prunable(tmp_path):
    # Pal/ is kept as a container, but only Pal/Saved/ survives inside it: the
    # binaries are the reinstallable game, not world data. Minecraft has no
    # equivalent partially-kept subtree.
    archive = tmp_path / "srv.zip"
    _zip(archive, _PAL_DIR)
    payloads = inspect_archive(str(archive), PAL).nested_payloads
    assert "Pal/Binaries/Linux/PalServer-Linux-Shipping" in payloads
    assert "Pal/Saved/SaveGames/0/A1B2/Level.sav" not in payloads


@pytest.mark.parametrize(
    "policy,files,expected",
    [
        (MC, {"MyWorld/level.dat": "x", "MyWorld/region/r.mca": "r"}, {"level.dat", "region"}),
        (PAL, {"A1B2/Level.sav": "x", "A1B2/Players/a.sav": "p"}, {"Level.sav", "Players"}),
    ],
    ids=["minecraft", "palworld"],
)
def test_bare_world_folder_is_descended_and_not_flagged(tmp_path, policy, files, expected):
    # The wrapper is descended, and the marker file inside identifies what we
    # landed on as a world, so none of its internals are reported as junk.
    archive = tmp_path / "bare.zip"
    _zip(archive, files)
    ins = inspect_archive(str(archive), policy)
    assert {e.name for e in ins.entries} == expected
    allowed, unknown = classify(ins.entries, policy)
    assert unknown == []
    assert set(allowed) == expected


@pytest.mark.parametrize("policy", [MC, PAL], ids=["minecraft", "palworld"])
def test_macos_compress_sidecar_is_ignored(tmp_path, policy):
    marker = "data/world/level.dat" if policy is MC else "data/Pal/Saved/Level.sav"
    archive = tmp_path / "mac.zip"
    _zip(archive, {marker: "x", "__MACOSX/._data": "junk", "data/.DS_Store": "junk"})
    ins = inspect_archive(str(archive), policy)
    _allowed, unknown = classify(ins.entries, policy)
    assert unknown == []


@pytest.mark.parametrize("policy", [MC, PAL], ids=["minecraft", "palworld"])
def test_extra_allowed_whitelists_name(tmp_path, policy):
    marker = "data/world/level.dat" if policy is MC else "data/Pal/Saved/Level.sav"
    archive = tmp_path / "srv.zip"
    _zip(archive, {marker: "x", "data/notes/readme.txt": "hello"})
    entries = inspect_archive(str(archive), policy).entries
    _allowed, unknown = classify(entries, policy)
    assert "notes" in unknown
    allowed2, unknown2 = classify(entries, policy, extra_allowed=["notes"])
    assert "notes" in allowed2
    assert unknown2 == []


def test_inspect_dir_matches_classification(tmp_path):
    root = tmp_path / "data"
    (root / "Pal" / "Saved" / "SaveGames").mkdir(parents=True)
    (root / "Pal" / "Saved" / "SaveGames" / "Level.sav").write_text("lvl")
    (root / "steamapps").mkdir()
    (root / "steamapps" / "m.acf").write_text("acf")
    allowed, unknown = classify(inspect_dir(str(root), PAL), PAL)
    assert set(allowed) == {"Pal"}
    assert set(unknown) == {"steamapps"}


def test_tar_archive_supported(tmp_path):
    src = tmp_path / "Level.sav"
    src.write_text("lvl")
    archive = tmp_path / "world.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(src, arcname="data/Pal/Saved/SaveGames/0/A1B2/Level.sav")
    assert {e.name for e in inspect_archive(str(archive), PAL).entries} == {"Pal"}


def test_unsupported_archive_rejected(tmp_path):
    bogus = tmp_path / "nope.bin"
    bogus.write_bytes(b"not an archive at all")
    with pytest.raises(ValueError):
        inspect_archive(str(bogus), PAL)


@pytest.mark.parametrize(
    "policy,files,named",
    [(MC, _MC_DIR, "server.jar"), (PAL, _PAL_DIR, "steamapps")],
    ids=["minecraft", "palworld"],
)
def test_prunable_report_names_junk_and_summarises_bulk(tmp_path, policy, files, named):
    archive = tmp_path / "srv.zip"
    _zip(archive, files)
    report = prunable_report(inspect_archive(str(archive), policy), policy)
    assert named in report
    # Bulk removals inside a kept dir are summarised rather than listed, so a
    # full game install doesn't dump tens of thousands of paths at the user.
    assert any("will be removed" in line for line in report)


@pytest.mark.parametrize(
    "policy,files",
    [
        (MC, {"data/world/level.dat": "x", "data/server.properties": "p"}),
        (
            PAL,
            {
                "data/Pal/Saved/SaveGames/0/A1B2/Level.sav": "x",
                "data/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini": "c",
            },
        ),
    ],
    ids=["minecraft", "palworld"],
)
def test_prunable_report_empty_for_a_clean_world(tmp_path, policy, files):
    archive = tmp_path / "clean.zip"
    _zip(archive, files)
    assert prunable_report(inspect_archive(str(archive), policy), policy) == []


# --- Counting the worlds in an upload ---------------------------------------


def _tar_with(tmp_path, *member_paths):
    import tarfile

    out = tmp_path / "a.tar.gz"
    payload = tmp_path / "payload"
    payload.write_bytes(b"x" * 1024)
    with tarfile.open(out, "w:gz") as tf:
        for m in member_paths:
            tf.add(payload, arcname=m)
    return str(out)


def _pal_policy():
    from open_worlds.common.games import adapter_for

    return adapter_for("PALWORLD").import_policy


def test_one_world_is_found(tmp_path):
    from open_worlds.common.upload_inspect import worlds_in_archive

    archive = _tar_with(
        tmp_path,
        "data/Pal/Saved/SaveGames/0/ABC/Level.sav",
        "data/Pal/Saved/SaveGames/0/ABC/Players/1.sav",
    )
    worlds = worlds_in_archive(archive, _pal_policy())
    assert [w.path.rsplit("/", 1)[-1] for w in worlds] == ["ABC"]


def test_each_save_directory_counts_once(tmp_path):
    from open_worlds.common.upload_inspect import worlds_in_archive

    archive = _tar_with(
        tmp_path,
        "data/Pal/Saved/SaveGames/0/ABC/Level.sav",
        "data/Pal/Saved/SaveGames/0/DEF/Level.sav",
    )
    assert len(worlds_in_archive(archive, _pal_policy())) == 2


def test_the_games_own_snapshots_are_not_separate_worlds(tmp_path):
    """Every backup/world/<timestamp>/ holds a Level.sav of its own; counting
    them would report a normal single-world upload as dozens of worlds."""
    from open_worlds.common.upload_inspect import worlds_in_archive

    archive = _tar_with(
        tmp_path,
        "data/Pal/Saved/SaveGames/0/ABC/Level.sav",
        "data/Pal/Saved/SaveGames/0/ABC/backup/world/2026.09.16-09.38.26/Level.sav",
        "data/Pal/Saved/SaveGames/0/ABC/backup/world/2026.09.17-23.59.43/Level.sav",
    )
    assert len(worlds_in_archive(archive, _pal_policy())) == 1


def test_an_archive_with_no_world_reports_none(tmp_path):
    from open_worlds.common.upload_inspect import worlds_in_archive

    archive = _tar_with(tmp_path, "data/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini")
    assert worlds_in_archive(archive, _pal_policy()) == []


def test_minecraft_is_not_held_to_one_world(tmp_path):
    """A normal server has a level.dat in world/, world_nether/ and
    world_the_end/, so enforcing this for Minecraft would reject every upload."""
    from open_worlds.common.games import adapter_for

    assert adapter_for("MINECRAFT").import_policy.enforce_single_world is False
    assert adapter_for("PALWORLD").import_policy.enforce_single_world is True


def test_the_description_names_size_and_date(tmp_path):
    """The rejection message is the whole UX for a multi-world upload, so it has
    to say enough for someone to tell which world is theirs."""
    from open_worlds.common.upload_inspect import worlds_in_archive

    archive = _tar_with(tmp_path, "data/Pal/Saved/SaveGames/0/ABC/Level.sav")
    described = worlds_in_archive(archive, _pal_policy())[0].describe()
    assert described.startswith("ABC (")
    assert ("MB" in described or "KB" in described) and "modified" in described


def test_allow_list_drops_are_reported_before_import(tmp_path):
    """An import must never quietly delete part of someone's upload.

    These files are neither unknown top-level entries nor matches for the deny
    rules, so without this they would go without ever being mentioned.
    """
    from open_worlds.common.upload_inspect import inspect_archive, prunable_report

    archive = tmp_path / "w.zip"
    _zip(
        archive,
        {
            "data/Pal/Saved/SaveGames/0/A1B2/Level.sav": "x",
            "data/Pal/Saved/SaveGames/0/A1B2/LocalData.sav": "fog",
            "data/Pal/Saved/Config/LinuxServer/Wwise.ini": "noise",
        },
    )
    policy = _pal_policy()
    report = prunable_report(inspect_archive(str(archive), policy), policy)
    assert report, "dropped files must be surfaced at preflight"
    assert any("Pal/" in line for line in report)


def test_a_bare_world_upload_is_judged_where_it_will_land(tmp_path):
    """Patterns describe the world directory, but archives are rooted anywhere.

    A bare save folder lands under SaveGames/0/imported, so the check has to be
    against the placed path or every bare upload reads as entirely disallowed.
    """
    from open_worlds.common.upload_inspect import inspect_archive

    archive = tmp_path / "bare.zip"
    _zip(archive, {"A1B2/Level.sav": "x", "A1B2/Players/abc.sav": "p"})
    inspection = inspect_archive(str(archive), _pal_policy())
    assert inspection.disallowed_paths == []
