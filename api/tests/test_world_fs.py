"""A world cannot redirect platform writes into another world with a symlink."""

from __future__ import annotations

import os

import pytest

from open_worlds.common import world_fs
from open_worlds.common.games.palworld import adapter as pal_adapter


@pytest.fixture
def worlds(tmp_path):
    a = tmp_path / "a" / "data"
    b = tmp_path / "b" / "data"
    (b / "mods").mkdir(parents=True)
    (b / "mods" / "keep.jar").write_bytes(b"b's")
    a.mkdir(parents=True)
    return str(a), b


def test_writes_and_reads_inside_the_world(worlds):
    a, _ = worlds
    world_fs.write_bytes(a, "mods/x.jar", b"jar")
    assert world_fs.read_bytes(a, "mods/x.jar") == b"jar"
    assert world_fs.is_file(a, "mods/x.jar")
    assert oct(os.stat(os.path.join(a, "mods", "x.jar")).st_mode & 0o777) == "0o644"
    assert world_fs.remove(a, "mods/x.jar")
    assert not world_fs.remove(a, "mods/x.jar")
    assert world_fs.read_bytes(a, "mods/x.jar") is None


def test_a_symlinked_directory_is_refused_not_followed(worlds):
    a, b = worlds
    os.symlink(b / "mods", os.path.join(a, "mods"))
    with pytest.raises(world_fs.UnsafeWorldPath):
        world_fs.write_bytes(a, "mods/x.jar", b"jar")
    with pytest.raises(world_fs.UnsafeWorldPath):
        world_fs.remove(a, "mods/keep.jar")
    with pytest.raises(world_fs.UnsafeWorldPath):
        world_fs.read_bytes(a, "mods/keep.jar")
    assert not world_fs.is_file(a, "mods/keep.jar")
    assert sorted(os.listdir(b / "mods")) == ["keep.jar"]


def test_a_symlink_deeper_in_the_path_is_refused(worlds):
    a, b = worlds
    os.makedirs(os.path.join(a, "world"))
    os.symlink(b / "mods", os.path.join(a, "world", "datapacks"))
    with pytest.raises(world_fs.UnsafeWorldPath):
        world_fs.copy_in(str(b / "mods" / "keep.jar"), a, "world/datapacks/p.zip")
    assert sorted(os.listdir(b / "mods")) == ["keep.jar"]


def test_a_symlinked_file_is_replaced_not_written_through(worlds):
    a, b = worlds
    os.makedirs(os.path.join(a, "mods"))
    os.symlink(b / "mods" / "keep.jar", os.path.join(a, "mods", "x.jar"))
    with pytest.raises(world_fs.UnsafeWorldPath):
        world_fs.read_bytes(a, "mods/x.jar")
    world_fs.write_bytes(a, "mods/x.jar", b"ours")
    assert (b / "mods" / "keep.jar").read_bytes() == b"b's"
    assert not os.path.islink(os.path.join(a, "mods", "x.jar"))


def test_a_path_cannot_climb_out(worlds):
    a, _ = worlds
    with pytest.raises(world_fs.UnsafeWorldPath):
        world_fs.write_bytes(a, "../../b/data/mods/x.jar", b"jar")


def test_palworld_server_name_is_not_written_through_a_symlink(worlds):
    a, b = worlds
    os.makedirs(os.path.join(a, "Pal", "Saved"))
    os.symlink(b, os.path.join(a, "Pal", "Saved", "Config"))
    with pytest.raises(world_fs.UnsafeWorldPath):
        pal_adapter._write_dedicated_server_name(a, "ABC")
    assert sorted(os.listdir(b)) == ["mods"]
