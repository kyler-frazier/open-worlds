"""Curated content: Modrinth resolution and verified download.

The catalog stores only project identity (a Modrinth slug, or a ``PINNED``
URL and hash). The file is resolved per world at apply time from its loader
and version, and its ``sha512`` checked before use.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

import httpx2

from ...models import ContentCatalog, ContentKind, ContentSource
from .config import minecraft_config
from .settings import ServerType
from .versions import is_concrete_version

__all__ = [
    "ContentError",
    "ModrinthClient",
    "ModrinthProject",
    "ResolvedFile",
    "download_and_verify",
    "is_concrete_version",
    "kind_supports_server_type",
    "loaders_for",
    "resolve_catalog_entry",
    "resolve_modrinth_file",
    "validate_project_kind",
]

# --- Kind <-> Modrinth mapping -------------------------------------------------

# Modrinth ``project_type`` values accepted per kind. Paper plugins appear as
# ``plugin`` or ``mod``. Data packs are typed "mod" and marked by the
# ``datapack`` loader, so validate_project_kind checks loaders too.
_KIND_PROJECT_TYPES: dict[ContentKind, frozenset[str]] = {
    ContentKind.MOD: frozenset({"mod", "plugin"}),
    ContentKind.DATAPACK: frozenset({"datapack", "mod"}),
    ContentKind.RESOURCEPACK: frozenset({"resourcepack"}),
}
# Loaders that are not a mod loader: a project with only these has no jar.
_NON_MOD_LOADERS = frozenset({"datapack", "minecraft"})

# Modrinth loader tokens to query for a given world server type when kind=MOD.
_SERVER_TYPE_LOADERS: dict[ServerType, tuple[str, ...]] = {
    ServerType.FABRIC: ("fabric",),
    ServerType.FORGE: ("forge", "neoforge"),
    ServerType.PAPER: ("paper", "spigot", "bukkit", "purpur", "folia"),
    ServerType.VANILLA: (),  # vanilla has no mod loader; MODs are hidden for it
}

# Guards against a project serving the wrong kind of artifact.
_KIND_EXTENSIONS: dict[ContentKind, tuple[str, ...]] = {
    ContentKind.MOD: (".jar",),
    ContentKind.DATAPACK: (".zip",),
    ContentKind.RESOURCEPACK: (".zip",),
}


class ContentError(Exception):
    """A recoverable content resolution/download failure (surfaced to the user)."""


@dataclass
class ModrinthProject:
    id: str
    slug: str
    title: str
    project_type: str
    loaders: list[str]
    game_versions: list[str]
    server_side: str
    # Modrinth "required"/"optional"/"unsupported"/"unknown"; used to flag mods
    # that need no client-side install ("server-side only").
    client_side: str = "unknown"
    icon_url: str | None = None


@dataclass
class Dependency:
    project_id: str | None
    dependency_type: str


@dataclass
class ResolvedFile:
    version_id: str
    filename: str
    url: str
    sha512: str
    sha1: str
    size: int
    dependencies: list[Dependency] = field(default_factory=list)


def loaders_for(kind: ContentKind, server_type: ServerType) -> tuple[str, ...]:
    """Modrinth loader tokens to query for a (kind, world server type) pair."""
    if kind is ContentKind.DATAPACK:
        return ("datapack",)
    if kind is ContentKind.RESOURCEPACK:
        return ("minecraft",)
    return _SERVER_TYPE_LOADERS.get(server_type, ())


def kind_supports_server_type(kind: ContentKind, server_type: ServerType) -> bool:
    """Whether a kind is even applicable to a world's server type.

    MODs need a loader (hidden for vanilla); datapacks/resource packs apply to
    every server type.
    """
    if kind is ContentKind.MOD:
        return bool(_SERVER_TYPE_LOADERS.get(server_type))
    return True


def _cached_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def is_compatible(entry: ContentCatalog, server_type: ServerType, version: str) -> bool:
    """Advisory fit check from the entry's cached loaders and versions.

    Decides what the picker offers and what a server-type switch would strand;
    the apply-time resolve is authoritative. Empty cached fields never exclude.
    """
    if not entry.enabled or not kind_supports_server_type(entry.kind, server_type):
        return False
    # Mods resolve only for a concrete release.
    if entry.kind is ContentKind.MOD and not is_concrete_version(version):
        return False
    want = set(loaders_for(entry.kind, server_type))
    have = set(_cached_list(entry.cached_loaders))
    if want and have and not (want & have):
        return False
    if is_concrete_version(version):
        gvs = _cached_list(entry.cached_game_versions)
        if gvs and version not in gvs:
            return False
    return True


def _max_bytes_for(kind: ContentKind) -> int:
    s = minecraft_config()
    return {
        ContentKind.MOD: s.max_mod_file_bytes,
        ContentKind.DATAPACK: s.max_datapack_file_bytes,
        ContentKind.RESOURCEPACK: s.max_resourcepack_file_bytes,
    }[kind]


class ModrinthClient:
    """Thin synchronous Modrinth v2 read client (workers/handlers are sync)."""

    def __init__(self) -> None:
        s = minecraft_config()
        self._base = s.modrinth_api_base.rstrip("/")
        self._headers = {"User-Agent": s.modrinth_user_agent}
        self._timeout = s.content_http_timeout_seconds

    def _get(self, path: str, params: dict[str, str] | None = None) -> object:
        url = f"{self._base}{path}"
        try:
            with httpx2.Client(timeout=self._timeout, follow_redirects=True) as client:
                resp = client.get(url, headers=self._headers, params=params)
        except httpx2.HTTPError as exc:
            raise ContentError(f"Modrinth request failed: {exc}") from exc
        if resp.status_code == 404:
            raise ContentError("Modrinth project not found.")
        if resp.status_code >= 400:
            raise ContentError(f"Modrinth returned HTTP {resp.status_code}.")
        return resp.json()

    def get_project(self, slug: str) -> ModrinthProject:
        data = self._get(f"/project/{slug}")
        if not isinstance(data, dict):
            raise ContentError("Unexpected Modrinth project response.")
        return ModrinthProject(
            id=str(data.get("id", "")),
            slug=str(data.get("slug", slug)),
            title=str(data.get("title", slug)),
            project_type=str(data.get("project_type", "")),
            loaders=[str(x) for x in data.get("loaders", [])],
            game_versions=[str(x) for x in data.get("game_versions", [])],
            server_side=str(data.get("server_side", "unknown")),
            client_side=str(data.get("client_side", "unknown")),
            icon_url=(str(data["icon_url"]) if data.get("icon_url") else None),
        )

    def list_versions(
        self, slug: str, loaders: Iterable[str], game_versions: Iterable[str]
    ) -> list[dict]:
        # Modrinth expects these filters as JSON-encoded array strings.
        params: dict[str, str] = {}
        loaders = list(loaders)
        game_versions = list(game_versions)
        if loaders:
            params["loaders"] = json.dumps(loaders)
        if game_versions:
            params["game_versions"] = json.dumps(game_versions)
        data = self._get(f"/project/{slug}/version", params=params)
        if not isinstance(data, list):
            raise ContentError("Unexpected Modrinth versions response.")
        return [v for v in data if isinstance(v, dict)]


def validate_project_kind(project: ModrinthProject, kind: ContentKind) -> None:
    """Ensure a Modrinth project can be installed as the catalog kind it's added as.

    The type says less than it seems: Modrinth's data packs are typed "mod", so
    whether a project offers a data pack or a mod is read from its loaders. A
    multi-format project (data pack + mod + plugin builds) can be added as
    either, and each kind installs its own build.
    """
    expected = _KIND_PROJECT_TYPES[kind]
    if project.project_type not in expected:
        raise ContentError(
            f"Modrinth project is a '{project.project_type}', "
            f"expected one of {sorted(expected)} for {kind.value}."
        )
    loaders = set(project.loaders)
    if kind is ContentKind.DATAPACK and project.project_type != "datapack":
        if "datapack" not in loaders:
            raise ContentError("This Modrinth project has no data pack version.")
    if kind is ContentKind.MOD and loaders and loaders <= _NON_MOD_LOADERS:
        raise ContentError(
            "This Modrinth project is only a data pack; add it as a data pack instead."
        )


def _pick_primary_file(version: dict) -> dict | None:
    files = [f for f in version.get("files", []) if isinstance(f, dict)]
    if not files:
        return None
    for f in files:
        if f.get("primary"):
            return f
    return files[0]


def resolve_modrinth_file(
    client: ModrinthClient,
    slug: str,
    kind: ContentKind,
    server_type: ServerType,
    game_version: str,
) -> ResolvedFile:
    """Resolve the newest compatible Modrinth file for a world.

    Raises ``ContentError`` if nothing compatible exists (the caller greys/hides
    the entry, or blocks the apply).
    """
    loaders = loaders_for(kind, server_type)
    if kind is ContentKind.MOD and not loaders:
        raise ContentError("Mods are not applicable to a vanilla world.")
    gv = [game_version] if is_concrete_version(game_version) else []
    versions = client.list_versions(slug, loaders, gv)
    if not versions:
        raise ContentError(f"No compatible version for MC {game_version} / {server_type.value}.")
    # Modrinth returns versions newest-first; take the first with a usable file.
    exts = _KIND_EXTENSIONS[kind]
    for version in versions:
        f = _pick_primary_file(version)
        if f is None:
            continue
        filename = str(f.get("filename", ""))
        if exts and not filename.lower().endswith(exts):
            continue
        hashes = f.get("hashes", {}) if isinstance(f.get("hashes"), dict) else {}
        sha512 = str(hashes.get("sha512", ""))
        if not sha512:
            continue
        deps = [
            Dependency(
                project_id=(str(d["project_id"]) if d.get("project_id") else None),
                dependency_type=str(d.get("dependency_type", "")),
            )
            for d in version.get("dependencies", [])
            if isinstance(d, dict)
        ]
        return ResolvedFile(
            version_id=str(version.get("id", "")),
            filename=filename,
            url=str(f.get("url", "")),
            sha512=sha512,
            sha1=str(hashes.get("sha1", "")),
            size=int(f.get("size", 0)),
            dependencies=deps,
        )
    raise ContentError("No downloadable file with a sha512 hash was found.")


def download_and_verify(url: str, dest_path: str, expected_sha512: str, kind: ContentKind) -> str:
    """Stream a download to ``dest_path``, enforcing the size cap and sha512.

    Returns the verified lowercase sha512. Raises ``ContentError`` on oversize
    or hash mismatch (and the partial file is left for the caller to clean up).
    """
    s = minecraft_config()
    max_bytes = _max_bytes_for(kind)
    hasher = hashlib.sha512()
    total = 0
    try:
        with httpx2.Client(timeout=s.content_http_timeout_seconds, follow_redirects=True) as client:
            with client.stream("GET", url, headers={"User-Agent": s.modrinth_user_agent}) as resp:
                if resp.status_code >= 400:
                    raise ContentError(f"Download failed: HTTP {resp.status_code}.")
                with open(dest_path, "wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise ContentError(
                                f"Download exceeds the {max_bytes} byte cap for {kind.value}."
                            )
                        hasher.update(chunk)
                        fh.write(chunk)
    except httpx2.HTTPError as exc:
        raise ContentError(f"Download failed: {exc}") from exc
    digest = hasher.hexdigest()
    if expected_sha512 and digest.lower() != expected_sha512.lower():
        raise ContentError("Downloaded file failed sha512 verification.")
    return digest


def _filename_from_url(url: str, kind: ContentKind) -> str:
    """Derive a safe basename from a URL, enforcing the kind's extension."""
    name = os.path.basename(unquote(urlparse(url).path)) or "download"
    # Strip any path separators a hostile URL might smuggle in.
    name = name.replace("/", "_").replace("\\", "_").lstrip(".")
    exts = _KIND_EXTENSIONS[kind]
    if exts and not name.lower().endswith(exts):
        name = f"{name}{exts[0]}"
    return name


def resolve_catalog_entry(
    entry: ContentCatalog, server_type: ServerType, game_version: str
) -> ResolvedFile:
    """Resolve the concrete file to install for a catalog entry and a world.

    Handles both MODRINTH (dynamic lookup) and PINNED (admin-supplied URL+hash).
    """
    if entry.source is ContentSource.PINNED:
        if not entry.pinned_download_url or not entry.pinned_sha512:
            raise ContentError("Pinned entry is missing its download URL or sha512.")
        return ResolvedFile(
            version_id="pinned",
            filename=_filename_from_url(entry.pinned_download_url, entry.kind),
            url=entry.pinned_download_url,
            sha512=entry.pinned_sha512,
            sha1=entry.pinned_sha1 or "",
            size=0,
            dependencies=[],
        )
    return resolve_modrinth_file(
        ModrinthClient(), entry.project_slug, entry.kind, server_type, game_version
    )
