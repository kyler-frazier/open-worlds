"""Tests for the file-backed, TTL-cached admin runtime settings.

We swap in a fake Settings (pointing at a tmp file) and a fresh cache per test so
the module-level cache never leaks between cases.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from open_worlds.common import runtime_config as rc
from open_worlds.common.runtime_config import _Cache

_GIB = 1024**3


def _use(monkeypatch, path, ttl=30):
    fake = SimpleNamespace(runtime_config_path=str(path), runtime_config_ttl_seconds=ttl)
    monkeypatch.setattr(rc, "get_settings", lambda: fake)
    monkeypatch.setattr(rc, "_cache", _Cache())


def test_defaults_when_file_missing(tmp_path, monkeypatch):
    _use(monkeypatch, tmp_path / "settings.json")
    cfg = rc.get_runtime_config()
    assert cfg.max_upload_bytes == 5 * _GIB
    assert cfg.max_upload_file_count == 100_000
    assert not hasattr(cfg, "prune_garbage_enabled")
    assert cfg.extra_allowed_root_names == []


def test_update_persists_and_roundtrips(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    _use(monkeypatch, path)
    rc.update_runtime_config(max_upload_bytes=_GIB, extra_allowed_root_names=["x"])

    on_disk = json.loads(path.read_text())
    assert on_disk["max_upload_bytes"] == _GIB
    assert on_disk["extra_allowed_root_names"] == ["x"]

    # A fresh cache must read the same values back from disk.
    monkeypatch.setattr(rc, "_cache", _Cache())
    cfg = rc.get_runtime_config()
    assert cfg.max_upload_bytes == _GIB
    assert cfg.extra_allowed_root_names == ["x"]


def test_ttl_caches_until_expiry(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    _use(monkeypatch, path, ttl=9999)
    first = rc.get_runtime_config().max_upload_bytes
    # An external edit within the TTL window is intentionally not observed.
    path.write_text(json.dumps({"max_upload_bytes": 2 * _GIB}))
    assert rc.get_runtime_config().max_upload_bytes == first


def test_zero_ttl_reloads_every_read(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    _use(monkeypatch, path, ttl=0)
    rc.get_runtime_config()
    path.write_text(json.dumps({"max_upload_bytes": 2 * _GIB}))
    assert rc.get_runtime_config().max_upload_bytes == 2 * _GIB


def test_malformed_file_falls_back_to_defaults(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text("{ not valid json")
    _use(monkeypatch, path)
    assert rc.get_runtime_config().max_upload_bytes == 5 * _GIB
