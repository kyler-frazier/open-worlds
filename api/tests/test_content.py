"""Tests for the curated-content resolver (Modrinth lookup + validation)."""

from __future__ import annotations

import pytest

from open_worlds.common.games.minecraft import content
from open_worlds.common.games.minecraft.content import (
    ContentError,
    ModrinthProject,
    is_concrete_version,
    kind_supports_server_type,
    loaders_for,
    resolve_modrinth_file,
    validate_project_kind,
)
from open_worlds.common.games.minecraft.settings import ServerType
from open_worlds.common.models import ContentKind


def test_loaders_for_by_kind_and_server_type():
    assert loaders_for(ContentKind.MOD, ServerType.FABRIC) == ("fabric",)
    assert loaders_for(ContentKind.MOD, ServerType.FORGE) == ("forge", "neoforge")
    assert "paper" in loaders_for(ContentKind.MOD, ServerType.PAPER)
    assert loaders_for(ContentKind.MOD, ServerType.VANILLA) == ()
    # Datapacks/resource packs use fixed Modrinth loader tokens for any server.
    assert loaders_for(ContentKind.DATAPACK, ServerType.VANILLA) == ("datapack",)
    assert loaders_for(ContentKind.RESOURCEPACK, ServerType.PAPER) == ("minecraft",)


def test_kind_applicability_hides_mods_from_vanilla():
    assert not kind_supports_server_type(ContentKind.MOD, ServerType.VANILLA)
    assert kind_supports_server_type(ContentKind.MOD, ServerType.FABRIC)
    assert kind_supports_server_type(ContentKind.DATAPACK, ServerType.VANILLA)
    assert kind_supports_server_type(ContentKind.RESOURCEPACK, ServerType.VANILLA)


def test_is_concrete_version():
    assert is_concrete_version("1.20.4")
    assert not is_concrete_version("LATEST")
    assert not is_concrete_version("24w14a")


def test_validate_project_kind_rejects_mismatch():
    proj = ModrinthProject(
        id="abc",
        slug="cool",
        title="Cool",
        project_type="resourcepack",
        loaders=["minecraft"],
        game_versions=["1.20.4"],
        server_side="unsupported",
    )
    with pytest.raises(ContentError):
        validate_project_kind(proj, ContentKind.MOD)
    # A resourcepack project validates as RESOURCEPACK.
    validate_project_kind(proj, ContentKind.RESOURCEPACK)


class _FakeClient:
    def __init__(self, versions):
        self._versions = versions
        self.calls = []

    def list_versions(self, slug, loaders, game_versions):
        self.calls.append((slug, list(loaders), list(game_versions)))
        return self._versions


def _version(vid, filename, sha512, *, primary=True, loaders=None, deps=None):
    return {
        "id": vid,
        "loaders": loaders or ["fabric"],
        "game_versions": ["1.20.4"],
        "dependencies": deps or [],
        "files": [
            {
                "filename": filename,
                "url": f"https://cdn.example/{filename}",
                "primary": primary,
                "size": 123,
                "hashes": {"sha512": sha512, "sha1": "deadbeef"},
            }
        ],
    }


def test_resolve_picks_primary_and_passes_filters():
    fake = _FakeClient([_version("v1", "cool-1.0.jar", "a" * 128)])
    resolved = resolve_modrinth_file(fake, "cool", ContentKind.MOD, ServerType.FABRIC, "1.20.4")
    assert resolved.filename == "cool-1.0.jar"
    assert resolved.sha512 == "a" * 128
    assert resolved.version_id == "v1"
    # Concrete version is forwarded to Modrinth; loader is the world's loader.
    assert fake.calls == [("cool", ["fabric"], ["1.20.4"])]


def test_resolve_skips_wrong_extension_and_missing_hash():
    versions = [
        _version("bad-ext", "cool.txt", "b" * 128),
        _version("no-hash", "cool.jar", ""),
        _version("good", "cool-2.0.jar", "c" * 128),
    ]
    resolved = resolve_modrinth_file(
        _FakeClient(versions), "cool", ContentKind.MOD, ServerType.FABRIC, "1.20.4"
    )
    assert resolved.version_id == "good"


def test_resolve_non_concrete_version_omits_game_version_filter():
    fake = _FakeClient([_version("v1", "cool.jar", "a" * 128)])
    resolve_modrinth_file(fake, "cool", ContentKind.MOD, ServerType.FABRIC, "LATEST")
    assert fake.calls == [("cool", ["fabric"], [])]


def test_resolve_mods_on_vanilla_is_error():
    with pytest.raises(ContentError):
        resolve_modrinth_file(
            _FakeClient([]), "cool", ContentKind.MOD, ServerType.VANILLA, "1.20.4"
        )


def test_resolve_no_versions_raises():
    with pytest.raises(ContentError):
        resolve_modrinth_file(_FakeClient([]), "cool", ContentKind.MOD, ServerType.FABRIC, "1.20.4")


def test_resolve_extracts_required_dependencies():
    deps = [
        {"project_id": "dep1", "dependency_type": "required"},
        {"project_id": "dep2", "dependency_type": "optional"},
    ]
    resolved = resolve_modrinth_file(
        _FakeClient([_version("v1", "cool.jar", "a" * 128, deps=deps)]),
        "cool",
        ContentKind.MOD,
        ServerType.FABRIC,
        "1.20.4",
    )
    required = [d.project_id for d in resolved.dependencies if d.dependency_type == "required"]
    assert required == ["dep1"]


def test_filename_from_url_is_safe_and_extension_enforced():
    assert content._filename_from_url("https://x/y/../../etc/passwd", ContentKind.MOD).endswith(
        ".jar"
    )
    assert "/" not in content._filename_from_url("https://x/a/b/mod.jar", ContentKind.MOD)


def _project(project_type, loaders):
    return ModrinthProject(
        id="p",
        slug="p",
        title="P",
        project_type=project_type,
        loaders=loaders,
        game_versions=["1.21.1"],
        server_side="required",
    )


def test_a_data_pack_is_recognised_by_its_loader_not_its_type():
    """Modrinth types data packs as "mod". Veinminer's real metadata: one
    project with data pack, mod and plugin builds."""
    veinminer = _project(
        "mod",
        ["bukkit", "datapack", "fabric", "folia", "forge", "neoforge", "paper", "quilt"],
    )
    validate_project_kind(veinminer, ContentKind.DATAPACK)
    validate_project_kind(veinminer, ContentKind.MOD)  # its jar builds are mods


def test_a_mod_with_no_data_pack_build_is_not_a_data_pack():
    with pytest.raises(ContentError, match="no data pack"):
        validate_project_kind(_project("mod", ["fabric", "neoforge"]), ContentKind.DATAPACK)


def test_a_data_pack_only_project_is_not_a_mod():
    """Otherwise it would pass as a mod and fail at install, finding no jar."""
    with pytest.raises(ContentError, match="add it as a data pack"):
        validate_project_kind(_project("mod", ["datapack"]), ContentKind.MOD)
    validate_project_kind(_project("mod", ["datapack"]), ContentKind.DATAPACK)
