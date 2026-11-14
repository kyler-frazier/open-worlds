"""Tests for upload size/file-count caps enforced during archive extraction."""

from __future__ import annotations

import tarfile
import zipfile

import pytest

from open_worlds.common.config import get_settings
from open_worlds.worker import backups
from open_worlds.worker.backups import ExtractLimits


@pytest.fixture(autouse=True)
def _no_headroom(monkeypatch):
    # Test temp dirs may sit on a small tmpfs; the headroom is tested below.
    monkeypatch.setattr(
        backups,
        "get_settings",
        lambda: get_settings().model_copy(update={"min_free_bytes_for_backup": 0}),
    )


def _limits(max_bytes=10_000, max_files=1000):
    return ExtractLimits(max_total_bytes=max_bytes, max_files=max_files)


def test_zip_within_limits_extracts(tmp_path):
    archive = tmp_path / "ok.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("world/level.dat", "x" * 100)
    dest = tmp_path / "dest"
    backups._extract_archive(str(archive), str(dest), limits=_limits())
    assert (dest / "world" / "level.dat").read_text() == "x" * 100


def test_zip_rejected_when_too_large(tmp_path):
    archive = tmp_path / "big.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("data.bin", "x" * 500)
    with pytest.raises(backups.BackupError, match="too large"):
        backups._extract_archive(str(archive), str(tmp_path / "d"), limits=_limits(max_bytes=100))


def test_zip_rejected_when_too_many_files(tmp_path):
    archive = tmp_path / "many.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("a.txt", "a")
        z.writestr("b.txt", "b")
        z.writestr("c.txt", "c")
    with pytest.raises(backups.BackupError, match="too many files"):
        backups._extract_archive(str(archive), str(tmp_path / "d"), limits=_limits(max_files=2))


def test_tar_rejected_when_too_large(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"x" * 500)
    archive = tmp_path / "big.tar.gz"
    with tarfile.open(archive, "w:gz") as t:
        t.add(src, arcname="data.bin")
    with pytest.raises(backups.BackupError, match="too large"):
        backups._extract_archive(str(archive), str(tmp_path / "d"), limits=_limits(max_bytes=100))


def test_no_limits_skips_checks(tmp_path):
    """A preflighted upload passes limits=None and is not capped again."""
    archive = tmp_path / "big.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("data.bin", "x" * 5000)
    dest = tmp_path / "dest"
    backups._extract_archive(str(archive), str(dest), limits=None)
    assert (dest / "data.bin").exists()


def test_drive_restores_are_capped():
    """A Drive file is the owner's to replace, so it is capped like an upload."""
    s = get_settings()
    assert backups.restore_limits() == ExtractLimits(s.max_restore_bytes, s.max_restore_file_count)


def test_an_archive_that_would_fill_the_disk_is_refused_before_writing(tmp_path, monkeypatch):
    """The declared expanded size is checked against free disk, with or without limits."""
    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("data.bin", "\0" * 1_000_000)
    free = backups.shutil.disk_usage(tmp_path).free
    monkeypatch.setattr(
        backups,
        "get_settings",
        lambda: get_settings().model_copy(update={"min_free_bytes_for_backup": free - 500_000}),
    )
    dest = tmp_path / "dest"
    with pytest.raises(backups.BackupError, match="free disk"):
        backups._extract_archive(str(archive), str(dest), limits=None)
    assert not (dest / "data.bin").exists()


def test_a_drive_download_stops_at_its_cap(tmp_path, monkeypatch):
    from open_worlds.common import drive

    class Chunks:
        def __init__(self, fh, _request, chunksize):
            self.fh = fh

        def next_chunk(self):
            self.fh.write(b"x" * 1000)
            return None, False  # never done: only the cap ends it

    monkeypatch.setattr(drive, "MediaIoBaseDownload", Chunks)
    client = drive.DriveClient.__new__(drive.DriveClient)
    client._service = type(
        "S", (), {"files": lambda self: type("F", (), {"get_media": lambda self, **_: None})()}
    )()
    with pytest.raises(drive.DriveFileTooLarge):
        client.download("id", str(tmp_path / "f"), max_bytes=5000)
    assert (tmp_path / "f").stat().st_size <= 6000
