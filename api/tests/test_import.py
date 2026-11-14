"""Tests for world-import archive handling: format support, layout normalization,
and path-traversal safety. No database or Docker is contacted."""

from __future__ import annotations

import stat
import tarfile
import types
import zipfile

import pytest

from open_worlds.common.config import get_settings
from open_worlds.common.games import adapter_for
from open_worlds.worker import backups


def _fake_settings(worlds_dir: str, staging_dir: str):
    """Real Settings with the paths redirected.

    Derived from the real object rather than hand-listed: a stand-in that
    enumerates fields goes stale the moment one is added, and the failure is a
    confusing AttributeError deep inside the code under test rather than
    anything about the test's subject.
    """
    return get_settings().model_copy(
        update={
            "worlds_dir": worlds_dir,
            "backup_staging_dir": staging_dir,
            # 0 means "any free space is fine" -- these tests are about what
            # gets written, not about the guard.
            "min_free_bytes_for_backup": 0,
        }
    )


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    worlds = tmp_path / "worlds"
    staging = tmp_path / "staging"
    worlds.mkdir()
    staging.mkdir()
    monkeypatch.setattr(backups, "get_settings", lambda: _fake_settings(str(worlds), str(staging)))
    return worlds, staging


def _world(subdomain: str = "w1", game="MINECRAFT"):
    """A stand-in world row. The game decides which import policy applies."""
    return types.SimpleNamespace(subdomain=subdomain, game=game)


def test_restore_our_tar_gz_strips_data_wrapper(dirs):
    """Our own backups wrap contents in `data/`; import must place them correctly."""
    worlds, staging = dirs
    src = staging / "src" / "data"
    (src / "world").mkdir(parents=True)
    (src / "world" / "level.dat").write_text("lvl")
    (src / "server.properties").write_text("props")
    archive = staging / "backup.tar.gz"
    with tarfile.open(archive, "w:gz") as t:
        t.add(src, arcname="data")

    backups._restore_into_world(_world(game="MINECRAFT"), str(archive))

    data_dir = worlds / "w1" / "data"
    assert (data_dir / "world" / "level.dat").read_text() == "lvl"
    assert (data_dir / "server.properties").read_text() == "props"
    # No double-nested data/data directory.
    assert not (data_dir / "data").exists()


def test_restore_zip_of_data_contents(dirs):
    """A zip whose top level is already the data contents imports as-is."""
    worlds, staging = dirs
    archive = staging / "world.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("world/level.dat", "lvl")
        z.writestr("server.properties", "props")

    backups._restore_into_world(_world(game="MINECRAFT"), str(archive))

    data_dir = worlds / "w1" / "data"
    assert (data_dir / "world" / "level.dat").read_text() == "lvl"
    assert (data_dir / "server.properties").read_text() == "props"


def test_restore_bare_save_folder_is_placed_in_the_save_tree(dirs):
    """A bare save folder (Level.sav at its root) is nested where the server looks.

    People commonly zip just the world-GUID directory. Its own name is consumed
    by the descent to the data root, so a container is recreated for it under
    SaveGames/0/ rather than dropping Level.sav loose in the data dir where the
    server would never find it.
    """
    worlds, staging = dirs
    src = staging / "src" / "A1B2C3"
    (src / "Players").mkdir(parents=True)
    (src / "Level.sav").write_text("lvl")
    archive = staging / "bare-save.tar.gz"
    with tarfile.open(archive, "w:gz") as t:
        t.add(src, arcname="A1B2C3")

    backups._restore_into_world(_world(game="PALWORLD"), str(archive))

    saves = worlds / "w1" / "data" / "Pal" / "Saved" / "SaveGames" / "0" / "imported"
    assert (saves / "Level.sav").read_text() == "lvl"
    assert (saves / "Players").is_dir()


def test_restore_places_archive_by_its_starting_depth(dirs):
    """An archive rooted at Saved/ lands under Pal/, not at the data root."""
    worlds, staging = dirs
    archive = staging / "saved.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("Saved/SaveGames/0/A1B2/Level.sav", "lvl")

    backups._restore_into_world(_world(game="PALWORLD"), str(archive))

    data_dir = worlds / "w1" / "data"
    assert (data_dir / "Pal" / "Saved" / "SaveGames" / "0" / "A1B2" / "Level.sav").exists()


def test_replace_wipes_existing_data(dirs):
    """Replace makes the world exactly the uploaded content (stale files gone)."""
    worlds, staging = dirs
    data_dir = worlds / "w1" / "data"
    old_save = data_dir / "Pal" / "Saved" / "SaveGames" / "0" / "OLD"
    old_save.mkdir(parents=True)
    (old_save / "Level.sav").write_text("old")
    (data_dir / "stale.txt").write_text("stale")
    archive = staging / "new.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("Pal/Saved/SaveGames/0/NEW/Level.sav", "new")

    backups._restore_into_world(_world(game="PALWORLD"), str(archive))

    new_save = data_dir / "Pal" / "Saved" / "SaveGames" / "0" / "NEW"
    assert (new_save / "Level.sav").read_text() == "new"
    assert not (data_dir / "stale.txt").exists()
    assert not old_save.exists()


def test_an_import_replaces_unrelated_existing_files(dirs):
    worlds, staging = dirs
    data_dir = worlds / "w1" / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "stale.txt").write_text("from the previous world")
    archive = staging / "add.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("Pal/Saved/SaveGames/0/A1B2/Level.sav", "lvl")

    backups._restore_into_world(_world(game="PALWORLD"), str(archive))

    # The world is exactly the archive; nothing old is left to interleave.
    assert not (data_dir / "stale.txt").exists()
    assert (data_dir / "Pal" / "Saved" / "SaveGames" / "0" / "A1B2" / "Level.sav").exists()


def test_backup_excludes_regenerable_runtime_files(dirs):
    """A backup is the save, not the ~10 GB game install sitting beside it."""
    worlds, _ = dirs
    data = worlds / "w1" / "data"
    save = data / "Pal" / "Saved" / "SaveGames" / "0" / "A1B2"
    save.mkdir(parents=True)
    (save / "Level.sav").write_text("lvl")
    (data / "Pal" / "Saved" / "Config" / "LinuxServer").mkdir(parents=True)
    (data / "Pal" / "Saved" / "Config" / "LinuxServer" / "PalWorldSettings.ini").write_text("cfg")
    # Regenerable install/state that should be dropped.
    (data / "steamapps").mkdir()
    (data / "steamapps" / "manifest.acf").write_text("acf")
    (data / "Engine").mkdir()
    (data / "Engine" / "big.pak").write_text("PAK")
    (data / "logs").mkdir()
    (data / "logs" / "PalServer.log").write_text("log")
    (data / "backups").mkdir()
    (data / "backups" / "old.tar.gz").write_text("old")

    archive = worlds / "w1" / "out.tar.gz"
    backups._archive("w1", str(archive), adapter_for("PALWORLD").import_policy)

    with tarfile.open(archive) as t:
        names = set(t.getnames())
    assert "data/Pal/Saved/SaveGames/0/A1B2/Level.sav" in names
    assert "data/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini" in names
    assert not any(n.startswith("data/steamapps") for n in names)
    assert not any(n.startswith("data/Engine") for n in names)
    assert not any(n.startswith("data/logs") for n in names)
    assert not any(n.startswith("data/backups") for n in names)


def test_prune_removes_unknown_entries(dirs, monkeypatch):
    worlds, staging = dirs
    monkeypatch.setattr(
        backups, "get_runtime_config", lambda: types.SimpleNamespace(extra_allowed_root_names=[])
    )
    archive = staging / "u.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("data/Pal/Saved/SaveGames/0/A1B2/Level.sav", "x")
        z.writestr("data/evil.exe", "j")
        z.writestr("data/junk/readme.txt", "r")

    backups._restore_into_world(_world(game="PALWORLD"), str(archive), prune=True)

    data_dir = worlds / "w1" / "data"
    assert (data_dir / "Pal" / "Saved" / "SaveGames" / "0" / "A1B2" / "Level.sav").exists()
    assert not (data_dir / "evil.exe").exists()
    assert not (data_dir / "junk").exists()


def test_prune_removes_nested_mod_payloads(dirs, monkeypatch):
    """Uploads lose loadable code and the game install; the save survives."""
    worlds, staging = dirs
    monkeypatch.setattr(
        backups, "get_runtime_config", lambda: types.SimpleNamespace(extra_allowed_root_names=[])
    )
    archive = staging / "modded.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("data/Pal/Saved/SaveGames/0/A1B2/Level.sav", "x")
        z.writestr("data/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini", "cfg")
        z.writestr("data/Pal/Saved/Mods/evil_P.pak", "MALWARE")
        z.writestr("data/Pal/Saved/Mods/UE4SS.dll", "MALWARE2")
        z.writestr("data/Pal/Binaries/Linux/PalServer-Linux-Shipping", "BIN")

    backups._restore_into_world(_world(game="PALWORLD"), str(archive), prune=True)

    data_dir = worlds / "w1" / "data"
    pal_saved = data_dir / "Pal" / "Saved"
    assert (pal_saved / "SaveGames" / "0" / "A1B2" / "Level.sav").exists()
    assert (pal_saved / "Config" / "LinuxServer" / "PalWorldSettings.ini").exists()
    assert not (pal_saved / "Mods" / "evil_P.pak").exists()
    assert not (pal_saved / "Mods" / "UE4SS.dll").exists()
    # The game install under Pal/ goes too -- it is reinstalled on boot.
    assert not (data_dir / "Pal" / "Binaries" / "Linux" / "PalServer-Linux-Shipping").exists()


def test_no_upload_may_keep_an_executable(dirs, monkeypatch):
    """There is no managed-content exemption: every loadable file is stripped.

    The Minecraft platform let admin-curated jars survive a prune because a
    trusted catalog vouched for them. Palworld has no equivalent registry, so the
    rule here is absolute -- which is the stricter posture, not a gap.
    """
    from open_worlds.common import upload_inspect

    _worlds, staging = dirs
    root = staging / "extracted"
    (root / "Pal" / "Saved" / "SaveGames" / "0" / "A1B2").mkdir(parents=True)
    (root / "Pal" / "Saved" / "SaveGames" / "0" / "A1B2" / "Level.sav").write_text("x")
    (root / "Pal" / "Saved" / "Mods").mkdir(parents=True)
    (root / "Pal" / "Saved" / "Mods" / "anything_P.pak").write_bytes(b"even-a-nice-one")

    remove, _report = upload_inspect.plan_dir_prune(
        str(root), adapter_for("PALWORLD").import_policy, extra_allowed=[]
    )
    assert "Pal/Saved/Mods/anything_P.pak" in remove
    assert "Pal/Saved/SaveGames/0/A1B2/Level.sav" not in remove


def test_rejects_zip_slip(dirs):
    _, staging = dirs
    archive = staging / "evil.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("../escape.txt", "pwned")
    with pytest.raises(backups.BackupError):
        backups._extract_archive(str(archive), str(staging / "dest"))


def test_rejects_zip_symlink_escape(dirs):
    """A symlink member pointing outside the world must be rejected, not extracted."""
    _, staging = dirs
    archive = staging / "symlink.zip"
    # Craft a zip entry marked as a Unix symlink whose contents are the target.
    info = zipfile.ZipInfo("escape")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(info, "/etc/passwd")
    with pytest.raises(backups.BackupError):
        backups._extract_archive(str(archive), str(staging / "dest-symlink"))


def test_import_free_disk_guard(dirs, monkeypatch):
    """Import bails out when free space is below the configured floor."""
    worlds, staging = dirs
    monkeypatch.setattr(
        backups,
        "get_settings",
        lambda: _fake_settings(str(worlds), str(staging)).model_copy(
            update={"min_free_bytes_for_backup": 10**15}
        ),
    )
    archive = staging / "ok.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("world/level.dat", "x")
    with pytest.raises(backups.BackupError):
        backups._restore_into_world(_world(game="PALWORLD"), str(archive))


def test_rejects_unknown_format(dirs):
    _, staging = dirs
    bogus = staging / "not-an-archive.bin"
    bogus.write_text("just some text, not a zip or tar")
    with pytest.raises(backups.BackupError):
        backups._extract_archive(str(bogus), str(staging / "dest2"))


# --- The preflight gate: one world per upload -------------------------------


def _archive_with(tmp_path, *member_paths):
    out = tmp_path / "up.tar.gz"
    payload = tmp_path / "payload"
    payload.write_bytes(b"x" * 2048)
    with tarfile.open(out, "w:gz") as tf:
        for m in member_paths:
            tf.add(payload, arcname=m)
    return str(out)


def _gate(tmp_path, *member_paths, game="PALWORLD", entries=()):
    from fastapi import HTTPException

    from open_worlds.api.routers.worlds import _require_single_world

    world = types.SimpleNamespace(game=game)
    inspection = types.SimpleNamespace(entries=[types.SimpleNamespace(name=n) for n in entries])
    archive = _archive_with(tmp_path, *member_paths)
    try:
        _require_single_world(world, archive, inspection)
    except HTTPException as exc:
        return str(exc.detail)
    return None


def test_a_single_world_upload_passes(tmp_path):
    assert _gate(tmp_path, "data/Pal/Saved/SaveGames/0/ABC/Level.sav") is None


def test_a_two_world_upload_is_rejected_and_names_both(tmp_path):
    detail = _gate(
        tmp_path,
        "data/Pal/Saved/SaveGames/0/ABC/Level.sav",
        "data/Pal/Saved/SaveGames/0/DEF/Level.sav",
    )
    assert detail is not None
    assert "2 worlds" in detail
    # Naming them is the point: the user has to know which one to keep.
    assert "ABC" in detail and "DEF" in detail


def test_an_upload_with_no_world_is_rejected_and_says_what_it_found(tmp_path):
    """A zero is more often "zipped one level too deep" than "not there", so the
    message has to give the user something to compare against."""
    detail = _gate(
        tmp_path,
        "data/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini",
        entries=("Pal", "steamapps"),
    )
    assert detail is not None
    # The marker is stored lowercase on the policy, and that is what the
    # message quotes; filenames are case-insensitive where users will look.
    assert "level.sav" in detail
    assert "Pal" in detail and "steamapps" in detail


def test_minecraft_uploads_are_not_gated(tmp_path):
    """Three level.dat files is a normal Minecraft server, not three worlds."""
    assert (
        _gate(
            tmp_path,
            "data/world/level.dat",
            "data/world_nether/level.dat",
            "data/world_the_end/level.dat",
            game="MINECRAFT",
        )
        is None
    )


def test_datapacks_survive_an_import(dirs):
    """Datapacks are kept on purpose; see _KNOWN_ROOTS in the Minecraft adapter.

    Their .mcfunction runs with server authority, but an upload can already set
    ops.json. Changing this should be a deliberate decision.
    """
    worlds, staging = dirs
    archive = staging / "dp.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("world/level.dat", "lvl")
        z.writestr("world/datapacks/pack/data/ns/functions/tick.mcfunction", "say hi")
        z.writestr("mods/evil.jar", "MZ")

    backups._restore_into_world(_world(game="MINECRAFT"), str(archive), prune=True)

    data_dir = worlds / "w1" / "data"
    pack = data_dir / "world" / "datapacks" / "pack" / "data" / "ns" / "functions"
    assert (pack / "tick.mcfunction").exists(), "datapacks are trusted world content"
    # The contrast that makes the boundary meaningful: jars are not.
    assert not (data_dir / "mods" / "evil.jar").exists()


# --- Palworld: only the named paths survive an import ------------------------


def _palworld_import(dirs, *members, prune=True):
    worlds, staging = dirs
    archive = staging / "pal.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for m in members:
            z.writestr(m, "x")
    backups._restore_into_world(_world(game="PALWORLD"), str(archive), prune=prune)
    return worlds / "w1" / "data"


def test_the_install_markers_never_survive_an_import(dirs):
    """The reason this policy exists.

    The image decides the server is already installed by testing that
    PalServer.sh and steamapps/appmanifest_2394010.acf exist -- no hash, no
    signature -- and with UPDATE_ON_BOOT=false it then skips SteamCMD and runs
    whatever binaries are on disk. An upload must not be able to supply them.
    """
    data = _palworld_import(
        dirs,
        "data/Pal/Saved/SaveGames/0/ABC/Level.sav",
        "data/PalServer.sh",
        "data/steamapps/appmanifest_2394010.acf",
        "data/Pal/Binaries/Linux/PalServer-Linux-Shipping",
    )
    assert not (data / "PalServer.sh").exists()
    assert not (data / "steamapps" / "appmanifest_2394010.acf").exists()
    assert not (data / "Pal" / "Binaries" / "Linux" / "PalServer-Linux-Shipping").exists()
    assert (data / "Pal" / "Saved" / "SaveGames" / "0" / "ABC" / "Level.sav").exists()


def test_the_world_and_its_config_survive(dirs):
    save = "data/Pal/Saved/SaveGames/0/ABC"
    cfg = "data/Pal/Saved/Config/LinuxServer"
    data = _palworld_import(
        dirs,
        f"{save}/Level.sav",
        f"{save}/LevelMeta.sav",
        f"{save}/Players/41C793E7.sav",
        # Dimensional Pal Storage: easy to miss, and losing it loses a player's
        # pal storage.
        f"{save}/Players/41C793E7_dps.sav",
        f"{cfg}/PalWorldSettings.ini",
        f"{cfg}/GameUserSettings.ini",
        f"{cfg}/Engine.ini",
    )
    kept = data / "Pal" / "Saved"
    assert (kept / "SaveGames" / "0" / "ABC" / "Level.sav").exists()
    assert (kept / "SaveGames" / "0" / "ABC" / "LevelMeta.sav").exists()
    assert (kept / "SaveGames" / "0" / "ABC" / "Players" / "41C793E7.sav").exists()
    assert (kept / "SaveGames" / "0" / "ABC" / "Players" / "41C793E7_dps.sav").exists()
    for name in ("PalWorldSettings.ini", "GameUserSettings.ini", "Engine.ini"):
        assert (kept / "Config" / "LinuxServer" / name).exists(), name


def test_config_from_a_windows_hosted_server_is_kept(dirs):
    """Dropping someone's settings because they used to host on Windows would be
    worse than keeping a file this server happens not to read."""
    data = _palworld_import(
        dirs,
        "data/Pal/Saved/SaveGames/0/ABC/Level.sav",
        "data/Pal/Saved/Config/WindowsServer/PalWorldSettings.ini",
    )
    assert (data / "Pal" / "Saved" / "Config" / "WindowsServer" / "PalWorldSettings.ini").exists()


def test_regenerated_noise_and_client_state_are_dropped(dirs):
    data = _palworld_import(
        dirs,
        "data/Pal/Saved/SaveGames/0/ABC/Level.sav",
        # Client-side only: fog-of-war and co-op world rules.
        "data/Pal/Saved/SaveGames/0/ABC/LocalData.sav",
        "data/Pal/Saved/SaveGames/0/ABC/WorldOption.sav",
        # The game's own snapshots: meaningless in a world that did not exist
        # when they were taken.
        "data/Pal/Saved/SaveGames/0/ABC/backup/world/2026.09.16-09.38.26/Level.sav",
        # Engine plugin defaults, rewritten on boot.
        "data/Pal/Saved/Config/LinuxServer/Wwise.ini",
        "data/Pal/Saved/Config/LinuxServer/Niagara.ini",
        # Crash reporting, one directory per crash.
        "data/Pal/Saved/Config/CrashReportClient/UECC-Linux-ABC/CrashReportClient.ini",
        "data/Pal/Saved/Crashes/crashinfo-Pal-pid-1-ABC/Diagnostics.txt",
    )
    save = data / "Pal" / "Saved" / "SaveGames" / "0" / "ABC"
    assert (save / "Level.sav").exists()
    assert not (save / "LocalData.sav").exists()
    assert not (save / "WorldOption.sav").exists()
    assert not (save / "backup").exists()
    cfg = data / "Pal" / "Saved" / "Config"
    assert not (cfg / "LinuxServer" / "Wwise.ini").exists()
    assert not (cfg / "LinuxServer" / "Niagara.ini").exists()
    assert not (cfg / "CrashReportClient").exists()
    assert not (data / "Pal" / "Saved" / "Crashes").exists()


def test_minecraft_is_untouched_by_the_allow_list(dirs):
    """Opt-in per policy: a game that names no paths keeps what it always did."""
    worlds, staging = dirs
    archive = staging / "mc.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("world/level.dat", "lvl")
        z.writestr("server.properties", "motd=hi")
        z.writestr("config/somemod/whatever.toml", "cfg")
    backups._restore_into_world(_world(game="MINECRAFT"), str(archive), prune=True)
    data = worlds / "w1" / "data"
    assert (data / "world" / "level.dat").exists()
    assert (data / "server.properties").exists()
    assert (data / "config" / "somemod" / "whatever.toml").exists()
