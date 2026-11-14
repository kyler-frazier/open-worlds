"""Where a multipart upload is buffered before the handler sees it.

Starlette writes the body to a SpooledTemporaryFile first, so an import is
written twice. The parser's copy went to /tmp, which inside the container is
the overlay filesystem -- the slowest place available and the one meant to stay
small. These pin the two properties that keep it off there.
"""

from __future__ import annotations

import os
import tempfile

from starlette.formparsers import MultiPartParser

from open_worlds.api.main import _configure_upload_spooling
from open_worlds.common.config import get_settings


def test_uploads_spill_onto_the_staging_volume(tmp_path, monkeypatch):
    """Not /tmp. The overlay is not a place to write a multi-gigabyte archive,
    and it is not the filesystem the file is ultimately going to."""
    staging = tmp_path / "staging"
    staging.mkdir()
    settings = get_settings().model_copy(update={"backup_staging_dir": str(staging)})

    monkeypatch.delenv("TMPDIR", raising=False)
    _configure_upload_spooling(settings)

    assert os.environ["TMPDIR"].startswith(str(staging))
    # gettempdir() caches, so the reset matters as much as the env var.
    assert tempfile.gettempdir().startswith(str(staging))


def test_a_small_upload_never_touches_disk(tmp_path, monkeypatch):
    """A curated mod is a few MB. Below the threshold the parser keeps it in
    memory, so the duplicate write disappears for the common upload."""
    staging = tmp_path / "staging"
    staging.mkdir()
    settings = get_settings().model_copy(
        update={"backup_staging_dir": str(staging), "upload_spool_max_bytes": 8 * 1024 * 1024}
    )

    _configure_upload_spooling(settings)
    assert MultiPartParser.spool_max_size == 8 * 1024 * 1024


def test_an_unusable_staging_dir_does_not_stop_the_api(tmp_path, monkeypatch):
    """A missing or read-only staging directory is a deployment problem. It
    should not be the reason the API refuses to start -- the default /tmp is
    worse, not broken."""
    settings = get_settings().model_copy(
        update={"backup_staging_dir": "/proc/nonexistent/cannot-create"}
    )
    monkeypatch.delenv("TMPDIR", raising=False)

    _configure_upload_spooling(settings)  # must not raise

    assert "TMPDIR" not in os.environ
