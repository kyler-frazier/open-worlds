"""The world icon: one exact file per game, exactly 64x64, on every way in.

The icon lives in a directory an import can also fill, so the shape check has
to hold for the upload route *and* for imports and restores -- otherwise an
archive is a way round the size cap. These cover the check itself, where each
game keeps the file, that backups carry it, and that imports enforce it.
"""

from __future__ import annotations

import os
import struct
import tarfile
import types
import zlib

import pytest
from tests.test_import import _fake_settings

from open_worlds.common.games import adapter_for
from open_worlds.common.upload_inspect import archive_icon_changes, inspect_archive
from open_worlds.common.world_icon import (
    ICON_MAX_BYTES,
    InvalidIcon,
    adopt_legacy_icons,
    clear_icons,
    enforce_icon,
    icon_files,
    icon_path,
    resolve_icon,
    validate_icon,
    write_icon,
)
from open_worlds.worker import backups
from open_worlds.worker.backups import _archive

MC = adapter_for("MINECRAFT").import_policy
PAL = adapter_for("PALWORLD").import_policy
PAL_ICON = "Pal/Saved/open-worlds-icon.png"
PAL_LEGACY_ICON = "Pal/Saved/gameplatform-icon.png"


def png(width: int = 64, height: int = 64, pad: int = 0) -> bytes:
    """A real, decodable PNG. ``pad`` appends a text chunk to inflate the size."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x7f" * (width * 3) for _ in range(height))
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
    if pad:
        out += chunk(b"tEXt", b"c\x00" + os.urandom(pad))
    return out + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


# --- the check ---------------------------------------------------------------


def test_a_64px_png_is_an_icon():
    validate_icon(png())


@pytest.mark.parametrize(
    "data,why",
    [
        (png(63, 64), "63x64"),
        (png(128, 128), "128x128"),
        (png(64, 64, pad=ICON_MAX_BYTES), "KB or smaller"),
        (b"GIF89a" + b"\x00" * 64, "PNG"),
        (b"", "PNG"),
    ],
)
def test_anything_else_is_refused(data, why):
    with pytest.raises(InvalidIcon, match=why):
        validate_icon(data)


# --- where each game keeps it --------------------------------------------------


@pytest.mark.parametrize("policy,rel", [(MC, "server-icon.png"), (PAL, PAL_ICON)])
def test_each_game_writes_its_own_path(tmp_path, policy, rel):
    data_dir = str(tmp_path)
    assert resolve_icon(data_dir, policy) is None
    write_icon(data_dir, policy, png())
    assert (tmp_path / rel).read_bytes() == png()
    assert icon_path(data_dir, policy) == os.path.join(data_dir, *rel.split("/"))
    assert resolve_icon(data_dir, policy) is not None
    assert clear_icons(data_dir, policy) == [rel]
    assert not (tmp_path / rel).exists()
    assert clear_icons(data_dir, policy) == []


def test_write_refuses_a_bad_icon_and_leaves_the_old_one(tmp_path):
    write_icon(str(tmp_path), MC, png())
    with pytest.raises(InvalidIcon):
        write_icon(str(tmp_path), MC, png(32, 32))
    assert (tmp_path / "server-icon.png").read_bytes() == png()
    assert [p for p in os.listdir(tmp_path) if p.endswith(".tmp")] == []


def test_palworld_allows_exactly_one_image_path():
    assert PAL.path_allowed(PAL_ICON)
    for near_miss in (
        "Pal/Saved/other.png",
        "Pal/Saved/open-worlds-icon.png.bak",
        "Pal/Saved/SaveGames/open-worlds-icon.png",
        "open-worlds-icon.png",
        "Pal/Saved/SaveGames/gameplatform-icon.png",
    ):
        assert not PAL.path_allowed(near_miss), near_miss


# --- backups carry it -----------------------------------------------------------


def test_palworld_backup_includes_the_icon(tmp_path, monkeypatch):
    data = tmp_path / "data"
    write_icon(str(data), PAL, png())
    (data / "Pal" / "Saved" / "SaveGames" / "0" / "ABC").mkdir(parents=True)
    (data / "Pal" / "Saved" / "SaveGames" / "0" / "ABC" / "Level.sav").write_bytes(b"lvl")
    monkeypatch.setattr(backups, "_data_dir", lambda subdomain: str(data))
    out = str(tmp_path / "b.tar.gz")
    _archive("pal1", out, PAL)
    with tarfile.open(out, "r:gz") as tar:
        assert f"data/{PAL_ICON}" in tar.getnames()


# --- imports and restores enforce it ---------------------------------------------


@pytest.fixture
def worlds(tmp_path, monkeypatch):
    worlds_dir = tmp_path / "worlds"
    staging = tmp_path / "staging"
    worlds_dir.mkdir()
    staging.mkdir()
    monkeypatch.setattr(
        backups, "get_settings", lambda: _fake_settings(str(worlds_dir), str(staging))
    )
    return worlds_dir, staging


def _archive_of(staging, files: dict[str, bytes]) -> str:
    src = staging / "src" / "data"
    for rel, body in files.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    out = staging / "import.tar.gz"
    with tarfile.open(out, "w:gz") as t:
        t.add(src, arcname="data")
    return str(out)


_SAVES = {
    "MINECRAFT": {"world/level.dat": b"lvl", "server.properties": b"p"},
    "PALWORLD": {"Pal/Saved/SaveGames/0/ABC/Level.sav": b"lvl"},
}
_ICON_REL = {"MINECRAFT": "server-icon.png", "PALWORLD": PAL_ICON}


@pytest.mark.parametrize("game", ["MINECRAFT", "PALWORLD"])
def test_import_keeps_a_valid_icon(worlds, game):
    worlds_dir, staging = worlds
    archive = _archive_of(staging, {**_SAVES[game], _ICON_REL[game]: png()})
    backups._restore_into_world(types.SimpleNamespace(subdomain="w1", game=game), archive)
    assert (worlds_dir / "w1" / "data" / _ICON_REL[game]).read_bytes() == png()


@pytest.mark.parametrize("game", ["MINECRAFT", "PALWORLD"])
@pytest.mark.parametrize("icon", [png(512, 512), png(64, 64, pad=ICON_MAX_BYTES * 4), b"not a png"])
def test_import_drops_an_icon_that_is_not_one(worlds, game, icon):
    worlds_dir, staging = worlds
    archive = _archive_of(staging, {**_SAVES[game], _ICON_REL[game]: icon})
    backups._restore_into_world(types.SimpleNamespace(subdomain="w1", game=game), archive)
    data_dir = worlds_dir / "w1" / "data"
    assert not (data_dir / _ICON_REL[game]).exists()
    # Only the icon went; the world is untouched.
    for rel in _SAVES[game]:
        assert (data_dir / rel).exists()


def test_enforce_leaves_a_directory_of_that_name_alone(tmp_path):
    (tmp_path / "server-icon.png").mkdir()
    assert not enforce_icon(str(tmp_path), MC)
    assert (tmp_path / "server-icon.png").is_dir()


@pytest.mark.parametrize("game", ["MINECRAFT", "PALWORLD"])
def test_preflight_reports_an_oversized_icon(tmp_path, game):
    archive = _archive_of(
        tmp_path, {**_SAVES[game], _ICON_REL[game]: png(64, 64, pad=ICON_MAX_BYTES * 2)}
    )
    policy = adapter_for(game).import_policy
    assert _ICON_REL[game] in inspect_archive(archive, policy).disallowed_paths


@pytest.mark.parametrize("game", ["MINECRAFT", "PALWORLD"])
def test_preflight_passes_a_normal_icon(tmp_path, game):
    archive = _archive_of(tmp_path, {**_SAVES[game], _ICON_REL[game]: png()})
    policy = adapter_for(game).import_policy
    assert _ICON_REL[game] not in inspect_archive(archive, policy).disallowed_paths


# --- the save folder's own icon ------------------------------------------------


def _mc_world(tmp_path, level: str | None = None, icon: bytes | None = None):
    if level is not None:
        (tmp_path / "server.properties").write_text(f"motd=x\nlevel-name={level}\n")
    if icon is not None:
        folder = tmp_path / (level or "world")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "icon.png").write_bytes(icon)
    return str(tmp_path)


def test_a_save_icon_shows_when_the_world_has_none(tmp_path):
    data_dir = _mc_world(tmp_path, level="No Lag Plz", icon=png())
    assert resolve_icon(data_dir, MC) == (str(tmp_path / "No Lag Plz" / "icon.png"), False)
    assert resolve_icon(data_dir, MC) is not None


def test_level_name_defaults_to_world(tmp_path):
    data_dir = _mc_world(tmp_path, icon=png())
    assert resolve_icon(data_dir, MC) == (str(tmp_path / "world" / "icon.png"), False)


def test_the_worlds_own_icon_wins(tmp_path):
    data_dir = _mc_world(tmp_path, level="world", icon=png(64, 64, pad=10))
    write_icon(data_dir, MC, png())
    assert resolve_icon(data_dir, MC) == (str(tmp_path / "server-icon.png"), True)
    # The game falls back to the save's icon, so clearing takes both.
    assert clear_icons(data_dir, MC) == ["server-icon.png", "world/icon.png"]
    assert resolve_icon(data_dir, MC) is None


@pytest.mark.parametrize("icon", [png(128, 128), b"not a png"])
def test_an_invalid_save_icon_is_not_shown(tmp_path, icon):
    assert resolve_icon(_mc_world(tmp_path, level="world", icon=icon), MC) is None


@pytest.mark.parametrize("level", ["../escape", "a/b", ".."])
def test_a_level_name_outside_the_data_dir_is_ignored(tmp_path, level):
    (tmp_path / "server.properties").write_text(f"level-name={level}\n")
    (tmp_path.parent / "escape").mkdir(exist_ok=True)
    (tmp_path.parent / "escape" / "icon.png").write_bytes(png())
    assert resolve_icon(str(tmp_path), MC) is None


def test_palworld_has_no_save_icon(tmp_path):
    assert resolve_icon(_mc_world(tmp_path, level="world", icon=png()), PAL) is None


def test_icon_files_lists_both_in_the_games_order(tmp_path):
    data_dir = _mc_world(tmp_path, level="No Lag Plz", icon=png(128, 128))
    write_icon(data_dir, MC, png())
    files = icon_files(data_dir, MC)
    assert [(f.kind, f.relpath, f.valid) for f in files] == [
        ("server", "server-icon.png", True),
        # Listed although the game would refuse it: it is still a file a clear deletes.
        ("save", "No Lag Plz/icon.png", False),
    ]


def test_clear_leaves_a_directory_of_that_name(tmp_path):
    data_dir = _mc_world(tmp_path, level="world")
    (tmp_path / "world" / "icon.png").mkdir(parents=True)
    assert icon_files(data_dir, MC) == []
    assert clear_icons(data_dir, MC) == []
    assert (tmp_path / "world" / "icon.png").is_dir()


def test_palworld_lists_only_its_own_icon(tmp_path):
    write_icon(str(tmp_path), PAL, png())
    assert [f.relpath for f in icon_files(str(tmp_path), PAL)] == [PAL_ICON]


# --- imports keep one icon, in the game's own path --------------------------------
# The preflight reads the archive and the worker reads the disk; both must plan
# the same changes, or the owner is told one thing and another happens.


def _import(worlds, game, files):
    worlds_dir, staging = worlds
    archive = _archive_of(staging, files)
    told = [
        (c.action, c.path, c.to)
        for c in archive_icon_changes(archive, adapter_for(game).import_policy)
    ]
    backups._restore_into_world(types.SimpleNamespace(subdomain="w1", game=game), archive)
    return told, worlds_dir / "w1" / "data"


def test_both_icons_deletes_the_save_one(worlds):
    told, data = _import(
        worlds,
        "MINECRAFT",
        {
            **_SAVES["MINECRAFT"],
            "server-icon.png": png(),
            "world/icon.png": png(64, 64, pad=8),
        },
    )
    assert told == [("delete", "world/icon.png", None)]
    assert (data / "server-icon.png").read_bytes() == png()
    assert not (data / "world" / "icon.png").exists()


def test_only_a_save_icon_is_moved_to_the_server_path(worlds):
    files = {
        "No Lag Plz/level.dat": b"lvl",
        "server.properties": b"level-name=No Lag Plz\n",
        "No Lag Plz/icon.png": png(),
    }
    told, data = _import(worlds, "MINECRAFT", files)
    assert told == [("move", "No Lag Plz/icon.png", "server-icon.png")]
    assert (data / "server-icon.png").read_bytes() == png()
    assert not (data / "No Lag Plz" / "icon.png").exists()
    assert (data / "No Lag Plz" / "level.dat").exists()


def test_an_invalid_server_icon_lets_the_save_one_move(worlds):
    told, data = _import(
        worlds,
        "MINECRAFT",
        {**_SAVES["MINECRAFT"], "server-icon.png": png(128, 128), "world/icon.png": png()},
    )
    assert told == [("move", "world/icon.png", "server-icon.png")]
    assert (data / "server-icon.png").read_bytes() == png()


def test_an_invalid_save_icon_alone_is_left_alone(worlds):
    told, data = _import(
        worlds, "MINECRAFT", {**_SAVES["MINECRAFT"], "world/icon.png": png(128, 128)}
    )
    assert told == []
    assert not (data / "server-icon.png").exists()
    assert (data / "world" / "icon.png").exists()


def test_no_save_icon_means_no_changes(worlds):
    told, data = _import(worlds, "MINECRAFT", {**_SAVES["MINECRAFT"], "server-icon.png": png()})
    assert told == []
    assert (data / "server-icon.png").exists()


def test_palworld_imports_have_no_icon_changes(worlds):
    told, _ = _import(worlds, "PALWORLD", {**_SAVES["PALWORLD"], PAL_ICON: png()})
    assert told == []


def test_palworld_still_allows_the_legacy_icon_path():
    # Archives from before the rename carry it; the import moves it.
    assert PAL.path_allowed(PAL_LEGACY_ICON)


def test_a_legacy_icon_moves_to_the_current_path(tmp_path):
    legacy = tmp_path / PAL_LEGACY_ICON
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(png())
    assert adopt_legacy_icons(str(tmp_path), PAL) == [PAL_LEGACY_ICON]
    assert (tmp_path / PAL_ICON).read_bytes() == png()
    assert not legacy.exists()


def test_the_current_icon_wins_over_a_legacy_one(tmp_path):
    current = tmp_path / PAL_ICON
    current.parent.mkdir(parents=True)
    current.write_bytes(png())
    (tmp_path / PAL_LEGACY_ICON).write_bytes(png(pad=8))
    assert adopt_legacy_icons(str(tmp_path), PAL) == []
    assert current.read_bytes() == png()
    assert not (tmp_path / PAL_LEGACY_ICON).exists()


def test_a_game_without_legacy_paths_is_untouched(tmp_path):
    (tmp_path / "server-icon.png").write_bytes(png())
    assert adopt_legacy_icons(str(tmp_path), MC) == []
    assert (tmp_path / "server-icon.png").exists()


def test_importing_an_archive_with_the_legacy_icon_keeps_it(worlds):
    _, data = _import(worlds, "PALWORLD", {**_SAVES["PALWORLD"], PAL_LEGACY_ICON: png()})
    assert (data / PAL_ICON).read_bytes() == png()
    assert not (data / PAL_LEGACY_ICON).exists()


def test_a_zip_is_planned_the_same_way(tmp_path):
    import zipfile as zf

    archive = tmp_path / "w.zip"
    with zf.ZipFile(archive, "w") as z:
        z.writestr("myworld/server.properties", "level-name=world\n")
        z.writestr("myworld/world/level.dat", "lvl")
        z.writestr("myworld/world/icon.png", png())
    changes = archive_icon_changes(str(archive), MC)
    assert [(c.action, c.path, c.to) for c in changes] == [
        ("move", "world/icon.png", "server-icon.png")
    ]
