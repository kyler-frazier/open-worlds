"""Apply or remove a curated catalog entry on a world's data directory.

- MOD / DATAPACK: download, verify the sha512, write into ``mods/``,
  ``plugins/`` or ``world/datapacks/``.
- RESOURCEPACK: a ``world_content`` row records the URL and sha1, and the
  orchestrator sets the image's ``RESOURCE_PACK`` env on the next start.

The API requires the world OFF. ``managed_hashes`` and
``reinstall_world_content`` let managed content survive an import's prune.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import world_fs
from ...config import get_settings
from ...logging import get_logger
from ...models import (
    ContentCatalog,
    ContentKind,
    ContentSource,
    World,
    WorldContent,
)
from .config import minecraft_config
from .content import (
    ContentError,
    ResolvedFile,
    download_and_verify,
    resolve_catalog_entry,
)
from .settings import ServerType, settings_of

log = get_logger(__name__)


def _data_dir(subdomain: str) -> str:
    return os.path.join(get_settings().worlds_dir, subdomain, "data")


def _mod_subdir(server_type: ServerType) -> str:
    # Paper loads plugins from plugins/; Fabric/Forge load mods from mods/.
    return "plugins" if server_type is ServerType.PAPER else "mods"


def _install_dir(kind: ContentKind, server_type: ServerType) -> str:
    """Where ``kind`` goes, relative to the data dir (see world_fs for why)."""
    if kind is ContentKind.MOD:
        return _mod_subdir(server_type)
    if kind is ContentKind.DATAPACK:
        return "world/datapacks"
    raise ContentError(f"{kind.value} is not installed as a world file.")


def managed_hashes(db: Session, world_id: uuid.UUID) -> set[str]:
    """sha512 digests of a world's tracked file content (mods/datapacks).

    Passed to the import prune so a managed jar in an upload is preserved.
    """
    rows = db.execute(
        select(WorldContent.sha512).where(
            WorldContent.world_id == world_id, WorldContent.sha512.isnot(None)
        )
    ).scalars()
    return {h for h in rows if h}


def run_content(db: Session, world_id: uuid.UUID | None, payload: str) -> None:
    if world_id is None:
        raise ContentError("Content job has no world.")
    world = db.get(World, world_id)
    if world is None:
        log.warning("content job target world missing", extra={"world_id": str(world_id)})
        return
    data = json.loads(payload)
    action = data.get("action")
    catalog_id = uuid.UUID(str(data["catalog_id"]))
    if action == "apply":
        _apply(db, world, catalog_id, bool(data.get("enforce")))
    elif action == "remove":
        _remove(db, world, catalog_id)
    else:
        raise ContentError(f"Unknown content action: {action!r}")


def _existing(db: Session, world_id: uuid.UUID, catalog_id: uuid.UUID) -> WorldContent | None:
    return db.execute(
        select(WorldContent).where(
            WorldContent.world_id == world_id, WorldContent.catalog_id == catalog_id
        )
    ).scalar_one_or_none()


def _check_dependencies(db: Session, resolved: ResolvedFile) -> None:
    """Block the apply unless every required dependency is also in the catalog."""
    required = {
        d.project_id
        for d in resolved.dependencies
        if d.dependency_type == "required" and d.project_id
    }
    if not required:
        return
    have = set(
        db.execute(
            select(ContentCatalog.modrinth_project_id).where(
                ContentCatalog.kind == ContentKind.MOD,
                ContentCatalog.enabled.is_(True),
                ContentCatalog.source == ContentSource.MODRINTH,
            )
        ).scalars()
    )
    have.discard(None)
    missing = required - have
    if missing:
        raise ContentError(
            "This mod requires dependencies that are not in the catalog "
            f"(Modrinth project ids: {', '.join(sorted(missing))}). "
            "An admin must add them before it can be installed."
        )


def _cache_path(sha512: str) -> str | None:
    """Content-addressed cache location for a digest, or None when disabled."""
    root = minecraft_config().content_cache_dir
    sha = (sha512 or "").lower()
    if not root or len(sha) < 3:
        return None
    return os.path.join(root, sha[:2], sha)


def _download_file(
    resolved: ResolvedFile, kind: ContentKind, data_dir: str, dest_dir: str
) -> tuple[str, str]:
    """Download+verify into ``data_dir/dest_dir``; return (filename, sha512).

    Uses the content-addressed cache when configured: a cache hit (by the
    resolved sha512, known before download) is copied straight in; otherwise the
    verified download is stored in the cache for reuse across worlds.
    """
    # Never trust the resolved name to contain a path; keep only the basename.
    safe_name = os.path.basename(resolved.filename) or "download"
    final = f"{dest_dir}/{safe_name}"

    cached = _cache_path(resolved.sha512)
    if cached and os.path.isfile(cached):
        world_fs.copy_in(cached, data_dir, final)
        return safe_name, resolved.sha512.lower()

    staging = get_settings().backup_staging_dir
    os.makedirs(staging, exist_ok=True)
    tmp_path = os.path.join(staging, f"content-{uuid.uuid4().hex}")
    try:
        digest = download_and_verify(resolved.url, tmp_path, resolved.sha512, kind)
        # Populate the cache first (by verified digest), then place into the world.
        cache_target = _cache_path(digest)
        if cache_target and not os.path.isfile(cache_target):
            try:
                os.makedirs(os.path.dirname(cache_target), exist_ok=True)
                shutil.copy2(tmp_path, cache_target)
            except OSError:
                log.warning("could not populate content cache", extra={"sha512": digest[:12]})
        world_fs.copy_in(tmp_path, data_dir, final)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    return safe_name, digest


def _apply(db: Session, world: World, catalog_id: uuid.UUID, enforce: bool) -> None:
    entry = db.get(ContentCatalog, catalog_id)
    if entry is None or not entry.enabled:
        raise ContentError("Catalog entry not found or disabled.")
    settings = settings_of(world)
    resolved = resolve_catalog_entry(entry, settings.server_type, settings.version)
    if entry.kind is ContentKind.MOD:
        _check_dependencies(db, resolved)

    if entry.kind is ContentKind.RESOURCEPACK:
        _apply_resourcepack(db, world, entry, resolved, enforce)
        log.info(
            "resource pack applied",
            extra={"world_id": str(world.id), "catalog_id": str(catalog_id)},
        )
        return

    data_dir = _data_dir(world.subdomain)
    dest_dir = _install_dir(entry.kind, settings_of(world).server_type)
    filename, digest = _download_file(resolved, entry.kind, data_dir, dest_dir)

    wc = _existing(db, world.id, catalog_id)
    if wc is None:
        wc = WorldContent(world_id=world.id, catalog_id=catalog_id, kind=entry.kind)
        db.add(wc)
    wc.version_id = resolved.version_id
    wc.filename = filename
    wc.sha512 = digest
    wc.sha1 = resolved.sha1 or None
    wc.download_url = resolved.url
    db.flush()
    log.info(
        "content applied",
        extra={"world_id": str(world.id), "kind": entry.kind.value, "file": filename},
    )


def _apply_resourcepack(
    db: Session, world: World, entry: ContentCatalog, resolved: ResolvedFile, enforce: bool
) -> None:
    # A server enforces a single resource pack: drop any other one first.
    current = db.execute(
        select(WorldContent).where(
            WorldContent.world_id == world.id, WorldContent.kind == ContentKind.RESOURCEPACK
        )
    ).scalar_one_or_none()
    if current is not None and current.catalog_id != entry.id:
        db.delete(current)
        db.flush()
        current = None
    wc = current or WorldContent(
        world_id=world.id, catalog_id=entry.id, kind=ContentKind.RESOURCEPACK
    )
    if wc.id is None:
        db.add(wc)
    wc.version_id = resolved.version_id
    wc.filename = None
    wc.sha512 = resolved.sha512 or None
    wc.sha1 = resolved.sha1 or None
    wc.download_url = resolved.url
    wc.enforce = enforce
    db.flush()


def _remove(db: Session, world: World, catalog_id: uuid.UUID) -> None:
    wc = _existing(db, world.id, catalog_id)
    if wc is None:
        return
    if wc.kind in (ContentKind.MOD, ContentKind.DATAPACK) and wc.filename:
        dest_dir = _install_dir(wc.kind, settings_of(world).server_type)
        world_fs.remove(_data_dir(world.subdomain), f"{dest_dir}/{os.path.basename(wc.filename)}")
    db.delete(wc)
    db.flush()
    log.info("content removed", extra={"world_id": str(world.id), "catalog_id": str(catalog_id)})


def reinstall_world_content(db: Session, world: World) -> None:
    """Restore tracked file content missing after an import prune.

    Managed jars whose sha512 matched the upload are kept by the prune; anything
    else tracked (renamed, or absent from the upload) is re-downloaded here so a
    re-import can't silently drop a world's managed mods/datapacks.
    """
    rows = list(
        db.execute(
            select(WorldContent).where(
                WorldContent.world_id == world.id,
                WorldContent.kind.in_([ContentKind.MOD, ContentKind.DATAPACK]),
            )
        ).scalars()
    )
    if not rows:
        return
    data_dir = _data_dir(world.subdomain)
    for wc in rows:
        if not wc.filename or not wc.download_url or not wc.sha512:
            continue
        dest_dir = _install_dir(wc.kind, settings_of(world).server_type)
        if world_fs.is_file(data_dir, f"{dest_dir}/{os.path.basename(wc.filename)}"):
            continue  # preserved by the prune's managed-hash allowance
        resolved = ResolvedFile(
            version_id=wc.version_id or "",
            filename=wc.filename,
            url=wc.download_url,
            sha512=wc.sha512,
            sha1=wc.sha1 or "",
            size=0,
        )
        try:
            _download_file(resolved, wc.kind, data_dir, dest_dir)
            log.info(
                "reinstalled managed content after import",
                extra={"world_id": str(world.id), "file": wc.filename},
            )
        except ContentError:
            log.warning(
                "could not reinstall managed content after import",
                extra={"world_id": str(world.id), "file": wc.filename},
            )
