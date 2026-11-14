"""Each game's install hooks: when to install, how, and what the server then runs.

The orchestrator's side (two containers, two egress lists) is in
test_orchestrator.py; this is what each adapter decides from the data dir.
"""

from __future__ import annotations

import json
import time

import pytest

from open_worlds.common.games import adapter_for
from open_worlds.common.games.base import InstallIncomplete
from open_worlds.common.games.minecraft.adapter import INSTALL_MARKER, INSTALL_MAX_AGE_SECONDS
from open_worlds.common.games.minecraft.settings import ServerType
from open_worlds.common.orchestrator import WorldSpec


def _spec(game: str, **settings: str) -> WorldSpec:
    return WorldSpec(
        game=game,
        world_id="w1",
        owner_id="o1",
        subdomain="demo",
        display_name="Demo",
        memory_mb=4096,
        max_players=8,
        admin_password="pw",
        settings=settings,
    )


def _mc(server_type: ServerType, version: str = "1.21.4") -> WorldSpec:
    return _spec("MINECRAFT", version=version, server_type=server_type.value)


MC = adapter_for("MINECRAFT")
PAL = adapter_for("PALWORLD")

# What the image leaves for each loader: its results file, and the server it names.
INSTALLS = {
    ServerType.VANILLA: (None, "minecraft_server.1.21.4.jar"),
    ServerType.PAPER: (".paper.env", "paper-1.21.4-232.jar"),
    ServerType.FABRIC: (
        ".install-fabric.env",
        "fabric-server-mc.1.21.4-loader.0.17.2-launcher.1.1.0.jar",
    ),
    ServerType.FORGE: (".run-forge.env", "run.sh"),
}


def _leave_install(data, server_type: ServerType) -> str:
    results, server = INSTALLS[server_type]
    (data / server).write_text("server")
    if results:
        # As the image writes them: absolute for Paper, relative for Fabric.
        path = f"/data/{server}" if server_type is ServerType.PAPER else f"./{server}"
        (data / results).write_text(f'SERVER="{path}"\n')
    (data / "libraries").mkdir()
    return server


# --- Minecraft ----------------------------------------------------------------


@pytest.mark.parametrize("server_type", list(ServerType))
def test_minecraft_installs_once_then_runs_what_it_installed(tmp_path, server_type):
    spec = _mc(server_type)
    assert MC.install_needed(spec, str(tmp_path))
    server = _leave_install(tmp_path, server_type)

    MC.finish_install(spec, str(tmp_path))

    assert not MC.install_needed(spec, str(tmp_path))
    env = MC.run_environment(spec, str(tmp_path))
    assert env["TYPE"] == "CUSTOM"
    assert env["CUSTOM_SERVER"] == f"/data/{server}"
    # The rest of the template is the world's: memory, version, RCON.
    base = MC.environment(spec)
    assert {k: v for k, v in env.items() if k not in ("TYPE", "CUSTOM_SERVER", "FAMILY")} == {
        k: v for k, v in base.items() if k != "TYPE"
    }


@pytest.mark.parametrize(
    ("server_type", "family"),
    [
        (ServerType.VANILLA, "VANILLA"),
        (ServerType.PAPER, "SPIGOT"),
        (ServerType.FABRIC, "FABRIC"),
        (ServerType.FORGE, "FORGE"),
    ],
)
def test_minecraft_runs_as_its_family(tmp_path, server_type, family):
    # TYPE=CUSTOM is HYBRID otherwise, and the image would treat Fabric as Bukkit.
    spec = _mc(server_type)
    _leave_install(tmp_path, server_type)
    MC.finish_install(spec, str(tmp_path))
    assert MC.run_environment(spec, str(tmp_path))["FAMILY"] == family


@pytest.mark.parametrize(
    ("server_type", "expected"),
    [
        (ServerType.VANILLA, {"SETUP_ONLY": "true"}),
        (ServerType.FORGE, {"SETUP_ONLY": "true"}),
        # SETUP_ONLY stops before their first launch, which downloads the rest.
        (ServerType.PAPER, {"JVM_DD_OPTS": "paperclip.patchonly:true"}),
        (ServerType.FABRIC, {"EXTRA_ARGS": "--help"}),
    ],
)
def test_minecraft_install_modes(server_type, expected):
    spec = _mc(server_type)
    env = MC.install_environment(spec)
    assert {k: env[k] for k in expected} == expected
    assert env["TYPE"] == server_type.value
    for other in ("SETUP_ONLY", "JVM_DD_OPTS", "EXTRA_ARGS"):
        if other not in expected:
            assert other not in env


def test_minecraft_hides_the_worlds_code_from_the_install():
    assert set(MC.install_hidden_paths) == {"mods", "plugins"}


@pytest.mark.parametrize(
    "change",
    [
        {"version": "1.21.5"},
        {"server_type": ServerType.FABRIC.value},
    ],
)
def test_a_changed_version_or_loader_installs_again(tmp_path, change):
    spec = _mc(ServerType.PAPER)
    _leave_install(tmp_path, ServerType.PAPER)
    MC.finish_install(spec, str(tmp_path))

    changed = _spec("MINECRAFT", **{**spec.settings, **change})

    assert MC.install_needed(changed, str(tmp_path))


def test_a_new_image_installs_again(tmp_path, monkeypatch):
    spec = _mc(ServerType.PAPER)
    _leave_install(tmp_path, ServerType.PAPER)
    MC.finish_install(spec, str(tmp_path))

    monkeypatch.setattr(type(MC), "image", "itzg/minecraft-server@sha256:" + "0" * 64)

    assert MC.install_needed(spec, str(tmp_path))


def test_an_old_install_is_refreshed(tmp_path):
    # Behind the play list a server cannot update itself.
    spec = _mc(ServerType.PAPER)
    _leave_install(tmp_path, ServerType.PAPER)
    MC.finish_install(spec, str(tmp_path))
    marker = tmp_path / INSTALL_MARKER
    data = json.loads(marker.read_text())
    data["installed_at"] = time.time() - INSTALL_MAX_AGE_SECONDS - 1
    marker.write_text(json.dumps(data))

    assert MC.install_needed(spec, str(tmp_path))


@pytest.mark.parametrize("lost", ["server", "libraries"])
def test_an_install_missing_files_installs_again(tmp_path, lost):
    # A restore or an owner's delete: the server would try to download them.
    import shutil

    spec = _mc(ServerType.PAPER)
    server = _leave_install(tmp_path, ServerType.PAPER)
    MC.finish_install(spec, str(tmp_path))

    target = tmp_path / (server if lost == "server" else "libraries")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()

    assert MC.install_needed(spec, str(tmp_path))


@pytest.mark.parametrize(
    "marker", ["not json", "[]", '{"server": "../../etc/x.jar"}', '{"server": "a b.jar"}']
)
def test_a_garbled_marker_means_installing(tmp_path, marker):
    (tmp_path / INSTALL_MARKER).write_text(marker)
    assert MC.install_needed(_mc(ServerType.PAPER), str(tmp_path))
    with pytest.raises(InstallIncomplete):
        MC.run_environment(_mc(ServerType.PAPER), str(tmp_path))


@pytest.mark.parametrize(
    "named",
    [
        "",  # nothing recorded
        "missing.jar",  # recorded, not there
        "evil;touch x.jar",  # not a file name the image writes
        "server.txt",
    ],
)
def test_an_install_without_a_runnable_server_is_incomplete(tmp_path, named):
    (tmp_path / ".paper.env").write_text(f'SERVER="/data/{named}"\n')
    if named and named != "missing.jar":
        (tmp_path / named).write_text("x")

    with pytest.raises(InstallIncomplete):
        MC.finish_install(_mc(ServerType.PAPER), str(tmp_path))
    assert not (tmp_path / INSTALL_MARKER).exists()


def test_only_the_file_name_is_kept(tmp_path):
    # The results file is the world's to write; a path must not escape /data.
    (tmp_path / "paper.jar").write_text("x")
    (tmp_path / ".paper.env").write_text('SERVER="/elsewhere/../../paper.jar"\n')
    spec = _mc(ServerType.PAPER)

    MC.finish_install(spec, str(tmp_path))

    assert MC.run_environment(spec, str(tmp_path))["CUSTOM_SERVER"] == "/data/paper.jar"


# --- Palworld -----------------------------------------------------------------


def test_palworld_installs_until_the_server_and_its_manifest_are_there(tmp_path):
    spec = _spec("PALWORLD")
    assert PAL.install_needed(spec, str(tmp_path))
    (tmp_path / "PalServer.sh").write_text("")
    # A half-finished SteamCMD run leaves the script before the manifest.
    assert PAL.install_needed(spec, str(tmp_path))
    (tmp_path / "steamapps").mkdir()
    (tmp_path / "steamapps" / "appmanifest_2394010.acf").write_text("")

    assert not PAL.install_needed(spec, str(tmp_path))
    PAL.finish_install(spec, str(tmp_path))


def test_palworld_install_only_sets_up(tmp_path):
    spec = _spec("PALWORLD")
    env = PAL.install_environment(spec)
    assert env["SETUP_ONLY"] == "true"
    assert {k: v for k, v in env.items() if k != "SETUP_ONLY"} == PAL.environment(spec)
    # The server never updates itself: it cannot reach Steam.
    assert PAL.run_environment(spec, str(tmp_path))["UPDATE_ON_BOOT"] == "false"


def test_palworld_finish_refuses_an_incomplete_install(tmp_path):
    with pytest.raises(InstallIncomplete):
        PAL.finish_install(_spec("PALWORLD"), str(tmp_path))
