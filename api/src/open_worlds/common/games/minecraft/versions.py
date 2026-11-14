"""Minecraft versions: which a world may pin, and the latest release.

Worlds pin a specific version, never the rolling ``LATEST``/``SNAPSHOT``
aliases, so a world cannot auto-update into a version that breaks its data or
mods. The create-time default is the latest release from Mojang's manifest,
cached, with a configured fallback for when the manifest is unreachable.
"""

from __future__ import annotations

import re
import threading
import time

import httpx2

from ...logging import get_logger
from .config import minecraft_config

log = get_logger(__name__)

_LATEST_LOCK = threading.Lock()
# (fetched at, latest release, every listed version id or None); None until
# the first lookup.
_latest_cache: tuple[float, str, frozenset[str] | None] | None = None
# So concurrent requests on an expired cache make one call to Mojang.
_refresh_in_flight = False

# Short: the first caller in a process waits on it, and there is a fallback.
_MANIFEST_TIMEOUT_SECONDS = 5.0

# How soon to retry after a failed refresh: well under the TTL, but enough not
# to hammer Mojang through an outage.
_REFRESH_RETRY_SECONDS = 300.0

# Specific pre-release forms: weekly snapshots (24w14a) and pre-releases or
# release candidates (1.20.2-pre1, 1.21-rc1).
_SNAPSHOT_RE = re.compile(r"^\d{2}w\d{2}[a-z]$")
_PRE_RC_RE = re.compile(r"^\d+(?:\.\d+)*-(?:pre|rc)\d+$")


def release_tuple(v: str) -> tuple[int, ...] | None:
    """Parse a plain release like ``1.20.4`` into a comparable tuple, else None."""
    parts = v.strip().split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def is_concrete_version(version: str) -> bool:
    """True for a pinnable release like ``1.20.4`` (not ``LATEST``/``SNAPSHOT``/weekly)."""
    return release_tuple(version) is not None


def is_pinnable_version(version: str) -> bool:
    """True for any specific, non-rolling version a world may pin to.

    Releases, weekly snapshots and pre-releases. Curated mods are offered only
    for releases (:func:`is_concrete_version`).
    """
    v = version.strip()
    if not v:
        return False
    if is_concrete_version(v):
        return True
    return bool(_SNAPSHOT_RE.match(v) or _PRE_RC_RE.match(v))


def _reset_cache_for_tests() -> None:
    """Clear the memoized latest-release value (used by the test suite)."""
    global _latest_cache, _refresh_in_flight
    with _LATEST_LOCK:
        _latest_cache = None
        _refresh_in_flight = False


def known_versions() -> frozenset[str] | None:
    """Every version id in Mojang's manifest, or None if it was never fetched.

    None rather than empty, so callers can tell "unknown to us" from "not a
    real version".
    """
    latest_release()  # populate or refresh the cache
    with _LATEST_LOCK:
        cached = _latest_cache
    return cached[2] if cached is not None else None


def version_exists(version: str) -> bool | None:
    """Whether Mojang ships ``version``; None when the manifest is unavailable.

    ``1.21.99`` is well-formed but would fail at boot, so it is refused at
    create/edit. Callers let None through rather than depend on Mojang being up.
    """
    known = known_versions()
    if known is None:
        return None
    return version.strip() in known


def latest_release() -> str:
    """The latest stable release per Mojang's manifest, or the configured fallback.

    Stale-while-revalidate: only the first caller in a process waits on the
    network. An expired value is returned at once and refreshed in the
    background, because the create-world form loads from this.
    """
    s = minecraft_config()
    now = time.monotonic()
    with _LATEST_LOCK:
        cached = _latest_cache
    if cached is not None:
        if now - cached[0] >= s.version_cache_ttl_seconds:
            _start_background_refresh()
        return cached[1]
    return _fetch_latest_release()


def _start_background_refresh() -> None:
    """Refresh the cache off the request path, at most one refresh at a time."""
    global _refresh_in_flight
    with _LATEST_LOCK:
        if _refresh_in_flight:
            return
        _refresh_in_flight = True

    def _run() -> None:
        global _refresh_in_flight
        try:
            _fetch_latest_release()
        finally:
            with _LATEST_LOCK:
                _refresh_in_flight = False

    # Daemon: a refresh in flight must never hold up process shutdown.
    threading.Thread(target=_run, name="mcversion-refresh", daemon=True).start()


def _fetch_latest_release() -> str:
    """Fetch the manifest, update the cache, and return the resolved version.

    A failed fetch never replaces a version already resolved with the
    fallback, which would be a downgrade.
    """
    global _latest_cache
    s = minecraft_config()

    resolved = False
    version = s.default_world_version_fallback
    versions: frozenset[str] | None = None
    try:
        with httpx2.Client(timeout=_MANIFEST_TIMEOUT_SECONDS, follow_redirects=True) as client:
            resp = client.get(s.mojang_version_manifest_url)
        resp.raise_for_status()
        data = resp.json()
        latest = data.get("latest", {}).get("release") if isinstance(data, dict) else None
        if isinstance(latest, str) and is_concrete_version(latest):
            version = latest
            resolved = True
        else:
            log.warning("Mojang manifest missing a usable latest.release; using fallback")
        listed = data.get("versions") if isinstance(data, dict) else None
        if isinstance(listed, list):
            ids = frozenset(str(v["id"]) for v in listed if isinstance(v, dict) and v.get("id"))
            versions = ids or None
    except (httpx2.HTTPError, ValueError) as exc:
        log.warning(
            "could not fetch Mojang version manifest; using fallback",
            extra={"error": str(exc)},
        )

    now = time.monotonic()
    with _LATEST_LOCK:
        if not resolved and _latest_cache is not None:
            # Keep it, and backdate the stamp so the next call retries in
            # _REFRESH_RETRY_SECONDS rather than a whole TTL.
            previous = _latest_cache[1]
            _latest_cache = (
                now - minecraft_config().version_cache_ttl_seconds + _REFRESH_RETRY_SECONDS,
                previous,
                _latest_cache[2],
            )
            return previous
        _latest_cache = (now, version, versions)
    return version
