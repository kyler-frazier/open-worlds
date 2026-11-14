"""Admin-tunable runtime settings, stored in a file and cached with a TTL.

Unlike env-driven :class:`Settings`, admins edit these from the UI, so they live
in a JSON file on a volume shared by the api and worker. Changes apply within
one TTL without a restart. A missing file means defaults; an unreadable one
keeps the last good value; writes are atomic.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time

from pydantic import BaseModel, Field

from .config import get_settings
from .logging import get_logger

log = get_logger(__name__)

_GIB = 1024**3


class RuntimeConfig(BaseModel):
    """The admin-editable knobs. Field names are the JSON keys on disk."""

    # Max total *uncompressed* size accepted for a world upload/import.
    max_upload_bytes: int = Field(default=5 * _GIB, ge=1024 * 1024)
    # Max entries an uploaded archive may expand to.
    max_upload_file_count: int = Field(default=100_000, ge=1)
    # Top-level names allowed in addition to the built-in allow-list. The only
    # lever over what an upload may contain: filtering itself cannot be turned
    # off, because it is what stops an upload supplying the server binaries.
    extra_allowed_root_names: list[str] = Field(default_factory=list)
    # Game ids worlds may be created for; None until an admin first saves it.
    # Written only through set_enabled_games (see common/game_availability.py).
    enabled_games: list[str] | None = None


class _Cache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: RuntimeConfig | None = None
        self._loaded_at = 0.0

    def _read_file(self, path: str) -> RuntimeConfig:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            return RuntimeConfig.model_validate(data)
        except FileNotFoundError:
            return RuntimeConfig()
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            # Never raise into a request or worker over a bad file.
            log.warning("runtime config unreadable; using fallback", extra={"error": str(exc)})
            return self._value or RuntimeConfig()

    def get(self) -> RuntimeConfig:
        s = get_settings()
        now = time.monotonic()
        with self._lock:
            if self._value is None or (now - self._loaded_at) >= s.runtime_config_ttl_seconds:
                self._value = self._read_file(s.runtime_config_path)
                self._loaded_at = now
            return self._value

    def write(self, cfg: RuntimeConfig) -> RuntimeConfig:
        s = get_settings()
        path = s.runtime_config_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = json.dumps(cfg.model_dump(), indent=2, sort_keys=True)
        # Atomic replace so readers never see a half-written file.
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        with self._lock:
            self._value = cfg
            self._loaded_at = time.monotonic()
        return cfg


_cache = _Cache()


def get_runtime_config() -> RuntimeConfig:
    """Return the current settings, refreshing from disk at most once per TTL."""
    return _cache.get()


def update_runtime_config(**changes: object) -> RuntimeConfig:
    """Merge ``changes`` onto the current settings, persist, and return the result."""
    current = get_runtime_config()
    merged = current.model_copy(update={k: v for k, v in changes.items() if v is not None})
    return _cache.write(merged)
