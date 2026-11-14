"""Back a world up, restore it, and check the world came back.

Pins what neither half's tests can: a Palworld tree shaped like a real one is
archived, restored into an empty world and compared. Filesystem only.
"""

from __future__ import annotations

import hashlib
import os
import tarfile
import types
from unittest.mock import patch

import pytest

from open_worlds.common.config import get_settings
from open_worlds.common.games import adapter_for
from open_worlds.worker import backups as b

# The identifier Palworld generates per world; the save tree hangs off it.
WORLD_ID = "16540A98B40A49228E3B098EABF2ED78"
SAVE_DIR = f"Pal/Saved/SaveGames/0/{WORLD_ID}"


def _write(root, rel, data: bytes):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def _digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.fixture
def palworld_tree(tmp_path):
    """A data dir shaped like a real Palworld install."""
    data = tmp_path / "src" / "data"
    data.mkdir(parents=True)
    root = str(data)

    # What must survive.
    _write(root, f"{SAVE_DIR}/Level.sav", b"LEVEL" * 200)
    _write(root, f"{SAVE_DIR}/LevelMeta.sav", b"META" * 40)
    _write(root, f"{SAVE_DIR}/Players/8F7A5E0A.sav", b"PLAYER" * 30)
    _write(root, "Pal/Saved/Config/LinuxServer/PalWorldSettings.ini", b"[settings]\n")

    # The reinstallable game, which must not.
    _write(root, "Pal/Content/Paks/Pal-Linux.pak", b"PAK" * 5000)
    _write(root, "Pal/Binaries/Linux/PalServer-Linux-Shipping", b"BIN" * 5000)
    _write(root, "steamapps/appmanifest.acf", b"acf")
    _write(root, "linux64/steamclient.so", b"SO" * 5000)
    _write(root, "DefaultPalWorldSettings.ini", b"template")
    _write(root, "Manifest_UFSFiles_Linux.txt", b"m" * 10000)
    return root


def _round_trip(src_root, tmp_path):
    """Archive src_root the way a backup does, restore the way an import does."""
    policy = adapter_for("PALWORLD").import_policy
    worlds = tmp_path / "worlds"
    staging = tmp_path / "staging"
    (worlds / "rt" / "data").mkdir(parents=True)
    staging.mkdir()

    archive = str(staging / "rt.tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(src_root, arcname="data", filter=lambda ti: b._backup_filter(policy, ti))

    settings = get_settings().model_copy(
        update={
            "worlds_dir": str(worlds),
            "backup_staging_dir": str(staging),
            "min_free_bytes_for_backup": 0,
        }
    )
    runtime = types.SimpleNamespace(extra_allowed_root_names=[])
    world = types.SimpleNamespace(subdomain="rt", game="PALWORLD")
    with (
        patch.object(b, "get_settings", lambda: settings),
        patch.object(b, "get_runtime_config", lambda: runtime),
    ):
        b._restore_into_world(world, archive, prune=True)
    return str(worlds / "rt" / "data"), archive


def test_the_save_comes_back_byte_identical(palworld_tree, tmp_path):
    """The one property that matters, and the one nothing asserted.

    Placement is easy to get subtly wrong -- one directory too deep restores a
    world the server cannot see, and the server then generates a fresh one,
    which looks like a successful restore until someone logs in.
    """
    dest, _ = _round_trip(palworld_tree, tmp_path)

    for rel in (
        f"{SAVE_DIR}/Level.sav",
        f"{SAVE_DIR}/LevelMeta.sav",
        f"{SAVE_DIR}/Players/8F7A5E0A.sav",
    ):
        src = os.path.join(palworld_tree, *rel.split("/"))
        out = os.path.join(dest, *rel.split("/"))
        assert os.path.exists(out), f"{rel} did not survive the round trip"
        assert _digest(src) == _digest(out), f"{rel} came back changed"


def test_the_world_settings_come_back(palworld_tree, tmp_path):
    dest, _ = _round_trip(palworld_tree, tmp_path)
    assert os.path.exists(
        os.path.join(dest, "Pal", "Saved", "Config", "LinuxServer", "PalWorldSettings.ini")
    )


def test_the_reinstallable_game_does_not_come_back(palworld_tree, tmp_path):
    """Restoring must not resurrect the install. It is re-downloaded on boot,
    and carrying it is what made a 548 KB world a 4.55 GB archive."""
    dest, _ = _round_trip(palworld_tree, tmp_path)
    for gone in ("Pal/Content", "Pal/Binaries", "steamapps", "linux64"):
        assert not os.path.exists(os.path.join(dest, *gone.split("/"))), f"{gone} was restored"


def test_the_archive_carries_nothing_the_import_would_reject(palworld_tree, tmp_path):
    """The two halves of the policy must agree.

    They did not: the backup kept linux64 and DefaultPalWorldSettings.ini, and
    the import then reported them as "not part of a normal Palworld world" and
    asked the owner to approve pruning their own backup.
    """
    policy = adapter_for("PALWORLD").import_policy
    archive = str(tmp_path / "check.tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(palworld_tree, arcname="data", filter=lambda ti: b._backup_filter(policy, ti))
    with tarfile.open(archive) as tar:
        roots = {n.split("/")[1] for n in tar.getnames() if n.count("/") >= 1 and n != "data"}

    rejected = [r for r in roots if r.lower() not in policy.known_root_entries]
    assert not rejected, f"backup ships entries the import rejects: {rejected}"


def test_a_restored_world_is_not_called_unresponsive_while_it_reinstalls(tmp_path):
    """A restore removes the game, so the next boot re-downloads it.

    That takes far longer than a Minecraft boot, and the global 300s timeout
    reported a world installing exactly as designed as UNRESPONSIVE -- telling
    the owner their restore had failed when it had worked.
    """
    from datetime import UTC, datetime, timedelta

    from open_worlds.common.models import WorldState
    from open_worlds.common.readiness import Readiness, readiness_for

    adapter = adapter_for("PALWORLD")
    world = types.SimpleNamespace(
        state=WorldState.RUNNING,
        ready_at=None,
        started_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    assert readiness_for(world, adapter.readiness_timeout_seconds) is Readiness.STARTING
    # Still reports a genuine fault, once it is genuinely too long.
    world.started_at = datetime.now(UTC) - timedelta(minutes=45)
    assert readiness_for(world, adapter.readiness_timeout_seconds) is Readiness.UNRESPONSIVE
