"""What a backup archive actually contains.

The policy object was covered; the filter that is supposed to apply it was not,
and the two disagreed. A Palworld world with 548 KB of saves produced a 4.55 GB
archive because ``partial_subtrees`` was honoured on import and ignored on
backup. These tests are about the archive, not the policy, so that can't recur.
"""

from __future__ import annotations

import os
import tarfile

import pytest

from open_worlds.common.games import adapter_for
from open_worlds.worker.backups import _archive


def _write(root: str, rel: str, size: int = 16) -> None:
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"x" * size)


def _names(tar_path: str) -> set[str]:
    with tarfile.open(tar_path, "r:gz") as tar:
        return {m.name for m in tar.getmembers()}


@pytest.fixture
def palworld_world(tmp_path, monkeypatch):
    """A data dir shaped like a real Palworld install: saves inside the game."""
    data = tmp_path / "worlds" / "pal1" / "data"
    data.mkdir(parents=True)
    root = str(data)
    _write(root, "Pal/Saved/SaveGames/0/ABC/Level.sav")
    _write(root, "Pal/Saved/Config/LinuxServer/PalWorldSettings.ini")
    _write(root, "Pal/Content/Paks/Pal-Linux.pak", size=4096)
    _write(root, "Pal/Binaries/Linux/PalServer-Linux-Shipping", size=2048)
    _write(root, "Pal/Plugins/Something/lib.so")
    _write(root, "Engine/Binaries/ThirdParty/blob.so", size=2048)
    _write(root, "steamapps/appmanifest.acf")
    _write(root, "linux64/steamclient.so", size=2048)
    _write(root, "steamclient.so", size=2048)
    _write(root, "Manifest_UFSFiles_Linux.txt", size=4096)
    _write(root, "Manifest_NonUFSFiles_Linux.txt")
    _write(root, "Manifest_DebugFiles_Linux.txt")
    monkeypatch.setattr("open_worlds.worker.backups._data_dir", lambda subdomain: root)
    return tmp_path


def test_palworld_backup_keeps_the_save_tree(palworld_world, tmp_path):
    out = str(tmp_path / "b.tar.gz")
    _archive("pal1", out, adapter_for("PALWORLD").import_policy)
    names = _names(out)

    assert "data/Pal/Saved/SaveGames/0/ABC/Level.sav" in names
    assert "data/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini" in names


def test_palworld_backup_drops_the_reinstallable_server(palworld_world, tmp_path):
    """The reinstallable game (gigabytes) stays out of a backup of the save."""
    out = str(tmp_path / "b.tar.gz")
    _archive("pal1", out, adapter_for("PALWORLD").import_policy)
    names = _names(out)

    # partial_subtrees: under Pal/ only saved/ survives.
    assert not any(n.startswith("data/Pal/Content") for n in names)
    assert not any(n.startswith("data/Pal/Binaries") for n in names)
    assert not any(n.startswith("data/Pal/Plugins") for n in names)
    # backup_exclude_dirs, unchanged behaviour.
    assert not any(n.startswith("data/Engine") for n in names)
    assert not any(n.startswith("data/steamapps") for n in names)
    # Nested and loose executables.
    assert "data/linux64/steamclient.so" not in names
    assert "data/steamclient.so" not in names


def test_palworld_backup_keeps_the_directory_holding_the_saves(palworld_world, tmp_path):
    """``Pal`` is depth 1 and must survive: what we want is inside it.

    ``is_prunable_nested`` calls a bare top-level name prunable, having no kept
    child to match against. Asking it about ``Pal`` would drop the save tree
    along with the game -- an empty backup that still looks like a success.
    """
    out = str(tmp_path / "b.tar.gz")
    _archive("pal1", out, adapter_for("PALWORLD").import_policy)
    names = _names(out)

    assert "data/Pal" in names
    assert "data/Pal/Saved" in names


def test_palworld_backup_is_the_size_of_the_world_not_the_install(palworld_world, tmp_path):
    """The point of all of it, stated as a ratio rather than a rule."""
    out = str(tmp_path / "b.tar.gz")
    _archive("pal1", out, adapter_for("PALWORLD").import_policy)

    kept = sum(m.size for m in tarfile.open(out, "r:gz").getmembers() if m.isfile())
    assert kept < 200, f"archived {kept} bytes; the install should not be in here"


def test_minecraft_backup_keeps_the_world_and_drops_loadable_code(tmp_path, monkeypatch):
    """Nested jars go too, which is a decision rather than an accident.

    A restore reinstalls every tracked mod from the catalog afterwards
    (``reinstall_world_content``), and an untracked jar would be stripped by the
    import prune on the way back in regardless. So carrying jars in the archive
    buys nothing and costs space.
    """
    data = tmp_path / "worlds" / "mc1" / "data"
    data.mkdir(parents=True)
    root = str(data)
    _write(root, "world/level.dat")
    _write(root, "server.properties")
    _write(root, "mods/some-mod.jar", size=4096)
    _write(root, "libraries/net/x/lib.jar", size=4096)
    _write(root, "server.jar", size=4096)
    monkeypatch.setattr("open_worlds.worker.backups._data_dir", lambda subdomain: root)

    out = str(tmp_path / "b.tar.gz")
    _archive("mc1", out, adapter_for("MINECRAFT").import_policy)
    names = _names(out)

    assert "data/world/level.dat" in names
    assert "data/server.properties" in names
    assert "data/mods/some-mod.jar" not in names
    assert not any(n.startswith("data/libraries") for n in names)
    assert "data/server.jar" not in names


def test_palworld_backup_drops_the_unreal_manifests(palworld_world, tmp_path):
    """They only describe the shipped game files, which we already drop.

    Matched case-insensitively, as the filter lowercases before comparing --
    these ship with capitals on disk, so an exact-case list would miss them.
    """
    out = str(tmp_path / "b.tar.gz")
    _archive("pal1", out, adapter_for("PALWORLD").import_policy)
    names = _names(out)

    assert "data/Manifest_UFSFiles_Linux.txt" not in names
    assert "data/Manifest_NonUFSFiles_Linux.txt" not in names
    assert "data/Manifest_DebugFiles_Linux.txt" not in names


def test_palworld_backup_drops_the_games_own_snapshots(palworld_world, tmp_path):
    """Palworld snapshots its save into ``<id>/backup/`` on every
    autosave. A restore never kept them, so a 3 MB world shipped 45 MB of
    history to Drive only for it to be deleted on the way back in."""
    root = str(palworld_world / "worlds" / "pal1" / "data")
    _write(root, "Pal/Saved/SaveGames/0/ABC/backup/world/2026.09.16-09.38.26/Level.sav", 4096)
    _write(root, "Pal/Saved/SaveGames/0/ABC/backup/local/2026.09.16-09.38.26/LocalData.sav")
    out = str(tmp_path / "b.tar.gz")
    policy = adapter_for("PALWORLD").import_policy
    _archive("pal1", out, policy)
    names = _names(out)

    assert not any("/backup" in n for n in names)
    assert "data/Pal/Saved/SaveGames/0/ABC/Level.sav" in names
    # The disk guard agrees, and never walks into the subtree either.
    from open_worlds.worker.backups import _archivable_bytes

    assert _archivable_bytes(root, policy) < 200


def test_palworld_backup_keeps_everything_an_owner_would_miss(palworld_world, tmp_path):
    """What the allow-list keeps, laid out as a real world on disk has it --
    every save, both settings files, the engine config and the world icon --
    while the engine's stub configs, crash dumps and ImGui layout go."""
    root = str(palworld_world / "worlds" / "pal1" / "data")
    keep = [
        "Pal/Saved/SaveGames/0/ABC/LevelMeta.sav",
        "Pal/Saved/SaveGames/0/ABC/Players/41C793E7000000000000000000000000.sav",
        "Pal/Saved/SaveGames/0/ABC/Players/41C793E7000000000000000000000000_dps.sav",
        "Pal/Saved/Config/LinuxServer/GameUserSettings.ini",
        "Pal/Saved/Config/LinuxServer/Engine.ini",
        "Pal/Saved/open-worlds-icon.png",
        "Pal/Saved/gameplatform-icon.png",
    ]
    drop = [
        "Pal/Saved/Config/LinuxServer/Niagara.ini",
        "Pal/Saved/Config/CrashReportClient/UECC-Linux-1/CrashReportClient.ini",
        "Pal/Saved/Crashes/crashinfo-Pal-pid-1/Diagnostics.txt",
        "Pal/Saved/ImGui/Game.ini",
    ]
    for rel in keep + drop:
        _write(root, rel)
    out = str(tmp_path / "b.tar.gz")
    _archive("pal1", out, adapter_for("PALWORLD").import_policy)
    names = _names(out)

    for rel in keep:
        assert f"data/{rel}" in names, rel
    for rel in drop:
        assert f"data/{rel}" not in names, rel


# --------------------------------------------------------------------------
# Sizing the disk guard to the work, not to a fixed floor
# --------------------------------------------------------------------------


def test_the_guard_measures_what_the_archive_will_contain(palworld_world, tmp_path):
    """Not the directory total.

    The whole point of the backup filter is that the reinstallable game is most
    of the bytes and none of the archive. Sizing against the directory would
    refuse a backup of a 5 GB install holding half a megabyte of saves --
    exactly the shape of a real Palworld world.
    """
    from open_worlds.worker.backups import _archivable_bytes, _data_dir

    data = _data_dir("pal1")
    policy = adapter_for("PALWORLD").import_policy

    on_disk = sum(os.path.getsize(os.path.join(r, f)) for r, _d, fs in os.walk(data) for f in fs)
    archivable = _archivable_bytes(data, policy)

    assert archivable < on_disk, "the install must not be counted"
    # The save tree and its config are what remains.
    assert 0 < archivable < 200


def test_the_guard_refuses_work_that_would_not_fit(tmp_path, monkeypatch):
    """A fixed floor answered "is there N free?" and never "is there enough for
    this?" -- so a large world passed with a little free space and filled the
    disk."""
    import shutil as _shutil

    from open_worlds.common.config import get_settings
    from open_worlds.worker import backups as b

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(
        b,
        "get_settings",
        lambda: get_settings().model_copy(update={"min_free_bytes_for_backup": 1000}),
    )
    monkeypatch.setattr(
        _shutil, "disk_usage", lambda _p: _shutil._ntuple_diskusage(10_000, 4_000, 6_000)
    )

    # Fits: 1000 headroom + 4000 needed <= 6000 free.
    b._check_free_space(str(staging), 4_000)

    # Does not: 1000 + 5500 > 6000.
    with pytest.raises(b.BackupError, match="Insufficient free disk space"):
        b._check_free_space(str(staging), 5_500)


def test_a_tiny_world_is_not_refused_for_want_of_headroom(tmp_path, monkeypatch):
    """The other direction the floor got wrong: refusing a 500 KB backup
    because free space was under a fixed multi-gigabyte number."""
    import shutil as _shutil

    from open_worlds.common.config import get_settings
    from open_worlds.worker import backups as b

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(
        b,
        "get_settings",
        lambda: get_settings().model_copy(update={"min_free_bytes_for_backup": 0}),
    )
    monkeypatch.setattr(
        _shutil, "disk_usage", lambda _p: _shutil._ntuple_diskusage(10_000, 9_000, 1_000)
    )

    b._check_free_space(str(staging), 500)  # must not raise
