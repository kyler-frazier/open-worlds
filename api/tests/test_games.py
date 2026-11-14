"""Tests for the game seam itself.

These are contract tests: they assert things that must hold for *every*
registered adapter, so adding a third game either satisfies them or fails loudly
here rather than halfway through a container start.
"""

from __future__ import annotations

import pytest

from open_worlds.common.config import get_settings
from open_worlds.common.games import adapter_for, all_adapters
from open_worlds.common.games.minecraft.adapter import JVM_HEAP_FRACTION, jvm_heap_mb
from open_worlds.common.orchestrator import WorldSpec


def _spec(adapter, **kw):
    base = dict(
        game=adapter.game,
        world_id="w1",
        owner_id="o1",
        subdomain="demo",
        display_name="Demo",
        memory_mb=4096,
        max_players=16,
        admin_password="secret",
        server_password="joinpw" if adapter.supports_server_password else "",
        game_port=8213 if adapter.allocates_port else None,
        settings=(
            {"version": "1.21.4", "server_type": "VANILLA"}
            if adapter.supports_version_pinning
            else {}
        ),
    )
    base.update(kw)
    return WorldSpec(**base)


def test_built_in_games_register_through_entry_points():
    """Built-in games register exactly the way an outside package would."""
    from importlib.metadata import entry_points

    from open_worlds.common.games import ENTRY_POINT_GROUP

    registered = {ep.load()().game for ep in entry_points(group=ENTRY_POINT_GROUP)}
    assert registered == {a.game for a in all_adapters()} >= {"MINECRAFT", "PALWORLD"}


@pytest.mark.parametrize("adapter", all_adapters(), ids=lambda a: a.game)
def test_game_ids_are_upper_case(adapter):
    # The id is the stored value and the label on the game's containers.
    assert adapter.game == adapter.game.upper()


def test_some_game_is_offered_on_a_fresh_platform():
    # The offered list must never be empty, including before an admin saves it.
    assert any(a.enabled_by_default for a in all_adapters())


@pytest.mark.parametrize("adapter", all_adapters(), ids=lambda a: a.game)
def test_settings_model_forbids_unknown_keys(adapter):
    # A key for another game must be refused, not silently kept.
    from open_worlds.common.games import GameSettingsError

    with pytest.raises(GameSettingsError):
        adapter.validate_settings({"no_such_setting": 1})


@pytest.mark.parametrize("adapter", all_adapters(), ids=lambda a: a.game)
def test_settings_schema_describes_the_model(adapter):
    schema = adapter.settings_schema()
    assert schema["type"] == "object"
    assert set(schema.get("properties", {})) == set(adapter.settings_model.model_fields)


@pytest.mark.parametrize("adapter", all_adapters(), ids=lambda a: a.game)
def test_spec_fields_name_real_fields(adapter):
    # Every spec field is a world column or one of the game's settings, or the
    # "turn it off first" rule silently never fires for it.
    world_fields = {"memory_mb", "max_players", "server_password", "display_name"}
    assert set(adapter.spec_fields) <= world_fields | set(adapter.settings_model.model_fields)


@pytest.mark.parametrize("adapter", all_adapters(), ids=lambda a: a.game)
def test_adapter_declares_a_usable_container_spec(adapter):
    env = adapter.environment(_spec(adapter))
    assert env, "an adapter must supply some environment"
    assert all(isinstance(v, str) for v in env.values()), "docker env must be strings"
    assert adapter.data_mount.startswith("/")
    assert adapter.container_caps, "dropping ALL caps needs a minimal add-back set"
    assert adapter.image


@pytest.mark.parametrize("adapter", all_adapters(), ids=lambda a: a.game)
def test_admin_password_is_never_echoed_into_a_label(adapter):
    # Labels are readable by anything that can list containers; the password
    # belongs in the environment only.
    labels = adapter.labels(_spec(adapter))
    assert "secret" not in " ".join(labels.values())


@pytest.mark.parametrize("adapter", all_adapters(), ids=lambda a: a.game)
def test_import_policy_never_keeps_executable_code(adapter):
    policy = adapter.import_policy
    assert policy.executable_suffixes, "every game must strip something"
    for suffix in policy.executable_suffixes:
        assert policy.is_prunable_nested(f"anything/whatever{suffix}")
        # Including at the top of a kept directory, not just deep inside one.
        assert policy.is_prunable_nested(f"kept{suffix}")


@pytest.mark.parametrize("adapter", all_adapters(), ids=lambda a: a.game)
def test_connect_address_is_complete_enough_to_join(adapter):
    spec = _spec(adapter)
    address = adapter.connect_address("demo", "servers.example.com", spec.game_port)
    assert address.startswith("demo.servers.example.com")
    # A game that allocates a port must surface it: without one the address is
    # not enough to reach the world, since the port is what identifies it.
    assert (":" in address) is adapter.allocates_port


def test_port_allocation_and_hostname_routing_are_mutually_exclusive():
    """A game routes by hostname or by port, never both.

    This is the whole reason the seam exists, so it is worth stating outright:
    Minecraft's handshake names the host and mc-router picks a backend from it;
    Palworld's does not, so a port stands in for the name.
    """
    mc = adapter_for("MINECRAFT")
    pal = adapter_for("PALWORLD")
    assert not mc.allocates_port
    assert "mc-router.host" in mc.labels(_spec(mc))
    assert pal.allocates_port
    assert "mc-router.host" not in pal.labels(_spec(pal))


def test_capabilities_match_what_each_game_actually_has():
    mc = adapter_for("MINECRAFT")
    pal = adapter_for("PALWORLD")
    # Minecraft pins versions (Mojang publishes a manifest) and has a curated
    # catalog (Modrinth is a keyless, trusted registry with per-file hashes).
    assert mc.supports_version_pinning
    assert mc.supports_content_catalog
    # Palworld has neither: the server is a Steam app updated in place, and no
    # equivalent registry exists to curate from.
    assert not pal.supports_version_pinning
    assert not pal.supports_content_catalog


@pytest.mark.parametrize("adapter", all_adapters(), ids=lambda a: a.game)
def test_adapter_declares_a_bootable_default_memory(adapter):
    """A default below the floor either game needs would break every create.

    The upper bound is deliberately loose: what a host can afford is the
    operator's business (PLATFORM_MAX_MEMORY_MB), and a default above it is
    clamped rather than refused. This only catches an adapter that forgot the
    attribute or typed MB where GB was meant.
    """
    assert isinstance(adapter.default_memory_mb, int)
    assert 1024 <= adapter.default_memory_mb <= 65536


def test_default_memory_reflects_whether_it_is_reserved_or_merely_capped():
    """Minecraft's memory is reserved at boot (-Xms/-Xmx); Palworld's is only a
    cgroup ceiling, so Palworld's default should sit above Minecraft's."""
    mc = adapter_for("MINECRAFT")
    pal = adapter_for("PALWORLD")
    heap = mc.environment(_spec(mc, memory_mb=mc.default_memory_mb))["MEMORY"]
    assert heap == f"{jvm_heap_mb(mc.default_memory_mb)}M", (
        "Minecraft's default must be what actually reaches the JVM"
    )
    assert "MEMORY" not in pal.environment(_spec(pal)), (
        "nothing should hand Palworld a memory figure; its limit is the cgroup's"
    )
    assert pal.default_memory_mb > mc.default_memory_mb


def test_palworld_runs_multithreaded_without_a_host_sized_thread_count():
    """The image leaves Pocketpair's performance flags off, so a world ran its
    simulation single-threaded. The thread count must stay unset: the image's
    fallback counts host cores, which ignores any per-world CPU cap.
    """
    env = adapter_for("PALWORLD").environment(_spec(adapter_for("PALWORLD")))
    assert env["ENABLE_PERF_THREADING_ARGS"] == "true"
    # The deprecated switch would bring the host-sized fallback back with it.
    assert "MULTITHREADING" not in env
    assert "WORKER_THREADS_SERVER" not in env


def test_palworld_negative_delta_recovery_is_off_unless_the_operator_opts_in(monkeypatch):
    """Off by default: it masks a host clock problem, so it is the operator's call."""
    pal = adapter_for("PALWORLD")
    assert pal.environment(_spec(pal))["PALWORLD_ALLOW_NEGATIVE_DELTA_TIME"] == "false"

    monkeypatch.setenv("PLATFORM_PALWORLD_ALLOW_NEGATIVE_DELTA_TIME", "true")
    get_settings.cache_clear()
    try:
        assert pal.environment(_spec(pal))["PALWORLD_ALLOW_NEGATIVE_DELTA_TIME"] == "true"
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


@pytest.mark.parametrize("adapter", all_adapters(), ids=lambda a: a.game)
def test_renaming_a_world_never_changes_its_container(adapter):
    """Display name is platform metadata and must not reach the container spec.

    An adapter that derives any container input from ``display_name`` silently
    makes renaming a spec change, which forces the world OFF for a cosmetic edit.
    Anything identifying the world inside its container should come from the
    subdomain, which cannot change for the life of the world.
    """
    before = adapter.environment(_spec(adapter, display_name="Before"))
    after = adapter.environment(_spec(adapter, display_name="After Renaming"))
    assert before == after
    assert adapter.labels(_spec(adapter, display_name="Before")) == adapter.labels(
        _spec(adapter, display_name="After Renaming")
    )


def _delivered(adapter, spec, data_dir) -> str:
    """Everything the server will see: the environment plus any file we render.

    Deliberately agnostic about *which* of the two carries a given setting.
    Palworld's moved from the environment into its settings file once the
    platform took ownership of that file, and a test pinned to one mechanism
    would have called that a failure while the behaviour was unchanged.
    """
    blob = " ".join(adapter.environment(spec).values())
    adapter.prepare_data_dir(spec, str(data_dir))
    for path in sorted(data_dir.rglob("*")):
        if path.is_file():
            blob += " " + path.read_text()
    return blob


@pytest.mark.parametrize("adapter", all_adapters(), ids=lambda a: a.game)
def test_join_password_reaches_the_container_only_where_it_is_supported(adapter, tmp_path):
    """A password the game cannot enforce must never look like it was applied."""
    spec = _spec(adapter, server_password="joinpw")
    delivered = _delivered(adapter, spec, tmp_path)
    if adapter.supports_server_password:
        assert "joinpw" in delivered
    else:
        assert "joinpw" not in delivered
    # Whatever the game supports, a join password is no more label-safe than the
    # admin one: labels are readable by anything that can list containers.
    assert "joinpw" not in " ".join(adapter.labels(_spec(adapter)).values())


@pytest.mark.parametrize("adapter", all_adapters(), ids=lambda a: a.game)
def test_an_open_world_is_the_default(adapter, tmp_path):
    """No password configured must mean no password, never a blank-but-set one."""
    assert "joinpw" not in _delivered(adapter, _spec(adapter, server_password=""), tmp_path)


def test_only_palworld_has_a_join_password():
    # Minecraft has no shared server password at all: its access control is
    # whitelist.json, an allowlist of Mojang-authenticated usernames, which is a
    # different mechanism rather than the same one under another name.
    assert adapter_for("PALWORLD").supports_server_password
    assert not adapter_for("MINECRAFT").supports_server_password


@pytest.mark.parametrize("adapter", all_adapters(), ids=lambda a: a.game)
def test_every_admin_client_can_be_flushed_and_resumed(adapter):
    """``save_resume`` is contract, not an optional extra the caller must probe for."""
    client = adapter.admin_client("host.invalid", "pw")
    assert callable(client.save_flush)
    assert callable(client.save_resume)
    assert callable(client.players)
    assert callable(client.close)
    # A resume with nothing flushed must be harmless: the backup path calls it in
    # a finally block that also runs when the flush itself failed.
    client.save_resume()
    client.close()


def test_adapter_lookup_accepts_the_stored_string_value():
    # World.game round-trips through the database as a string.
    assert adapter_for("MINECRAFT") is adapter_for("MINECRAFT")


def test_minecraft_heap_leaves_headroom_under_the_container_limit():
    """The JVM heap must sit below the cgroup limit, never equal to it.

    Heap == limit left nothing for metaspace, GC structures and thread stacks, so
    a busy modded world was OOM-killed and reported as crashed. The world's
    figure is the container limit; the heap gets a fixed share of it.
    """
    mc = adapter_for("MINECRAFT")
    for memory_mb in (1024, 4096, 12288):
        heap = int(mc.environment(_spec(mc, memory_mb=memory_mb))["MEMORY"].rstrip("M"))
        assert heap < memory_mb
        assert heap == int(memory_mb * JVM_HEAP_FRACTION)
    # A tiny world still gets enough heap for the JVM to start at all.
    assert jvm_heap_mb(600) == 512


def test_palworld_keeps_mods_off():
    """v2.8.0 made mod support the image default, which downloads and loads
    UE4SS on every boot. The platform offers no mods, so it must opt out.
    """
    pal = adapter_for("PALWORLD")
    assert pal.environment(_spec(pal))["MOD_ENABLED"] == "false"


def test_dir_may_hold_allowed_prunes_only_unreachable_subtrees():
    policy = adapter_for("PALWORLD").import_policy
    for reachable in (
        "Pal",
        "Pal/Saved",
        "pal/saved/savegames/0/ABC",
        "Pal/Saved/SaveGames/0/ABC/Players",
    ):
        assert policy.dir_may_hold_allowed(reachable), reachable
    for unreachable in ("Pal/Content", "Pal/Saved/Crashes", "Pal/Saved/SaveGames/0/ABC/backup"):
        assert not policy.dir_may_hold_allowed(unreachable), unreachable
    # No allow-list, no opinion.
    assert adapter_for("MINECRAFT").import_policy.dir_may_hold_allowed("anything/at/all")


# --- Card artwork -----------------------------------------------------------------


@pytest.fixture
def art_dir(tmp_path, monkeypatch):
    from open_worlds.common import config

    monkeypatch.setenv("PLATFORM_GAME_ART_DIR", str(tmp_path))
    config.get_settings.cache_clear()
    yield tmp_path
    config.get_settings.cache_clear()


def test_no_art_unless_the_operator_supplies_it(monkeypatch):
    from open_worlds.common import config

    monkeypatch.delenv("PLATFORM_GAME_ART_DIR", raising=False)
    config.get_settings.cache_clear()
    assert all(a.art_path() is None for a in all_adapters())


def test_art_is_found_by_game_name_in_the_directory(art_dir):
    (art_dir / "minecraft.png").write_bytes(b"png")
    assert adapter_for("MINECRAFT").art_path() == str(art_dir / "minecraft.png")
    assert adapter_for("PALWORLD").art_path() is None


def test_one_game_can_point_elsewhere(art_dir, tmp_path_factory, monkeypatch):
    elsewhere = tmp_path_factory.mktemp("other") / "pal.webp"
    elsewhere.write_bytes(b"webp")
    (art_dir / "custom.jpg").write_bytes(b"jpg")
    monkeypatch.setenv("PLATFORM_GAME_ART_PALWORLD", str(elsewhere))
    monkeypatch.setenv("PLATFORM_GAME_ART_MINECRAFT", "custom.jpg")  # relative to the dir
    assert adapter_for("PALWORLD").art_path() == str(elsewhere)
    assert adapter_for("MINECRAFT").art_path() == str(art_dir / "custom.jpg")


def test_only_images_are_ever_served(art_dir, monkeypatch):
    secret = art_dir / "settings.env"
    secret.write_text("PLATFORM_SESSION_SECRET=x")
    monkeypatch.setenv("PLATFORM_GAME_ART_MINECRAFT", str(secret))
    assert adapter_for("MINECRAFT").art_path() is None
