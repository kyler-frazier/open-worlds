"""Tests for reaching archives whose backup row is gone.

A backup row is deleted with its world, so the archive it points at outlives
every record of it: still in the owner's Drive, still using their quota, and
invisible to the app. These cover the two halves of getting it back -- listing
Drive itself, and restoring from a file id rather than a row.
"""

from __future__ import annotations

import types

import pytest
from googleapiclient.errors import HttpError
from pydantic import ValidationError

from open_worlds.api.schemas import ImportFromDrive
from open_worlds.common.drive import _APP_FOLDER_NAME, DriveClient, is_not_found


class _Resp:
    """Minimal stand-in for the httplib2 response HttpError carries."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "test"


def test_import_requires_exactly_one_source():
    """A row id or a raw file id, never both and never neither.

    Both would leave which one wins to reading order; neither would reach the
    download with nothing to fetch.
    """
    with pytest.raises(ValidationError):
        ImportFromDrive()
    with pytest.raises(ValidationError):
        ImportFromDrive(backup_id="0f7d1c3e-3a3f-4a1e-9f6a-2f1c8b0d5e11", drive_file_id="abc")

    assert ImportFromDrive(drive_file_id="abc").backup_id is None
    assert ImportFromDrive(backup_id="0f7d1c3e-3a3f-4a1e-9f6a-2f1c8b0d5e11").drive_file_id is None


def test_an_import_carries_no_merge_option():
    """Imports always replace, so there is no flag to get wrong.

    Merging left the previous world's files interleaved with the new one's --
    and for Palworld a second save directory beside the first, which the server
    then had to be told which of to load.
    """
    assert not hasattr(ImportFromDrive(drive_file_id="abc"), "replace")


def test_a_drive_404_is_recognised():
    """Ownership of an untracked file rests on this.

    Under drive.file a file this app did not create is a 404, indistinguishable
    from one that does not exist -- which is what makes accepting a raw file id
    from a client safe. Mistaking that 404 for a transport error would turn a
    clean "not found" into a 502 and hide the real answer.
    """
    assert is_not_found(_http_error(404))
    assert not is_not_found(_http_error(500))
    assert not is_not_found(_http_error(403))
    assert not is_not_found(ValueError("unrelated"))


def _http_error(status: int) -> HttpError:
    return HttpError(_Resp(status), b"")


class _FakeFiles:
    """Just enough of Drive's files() resource for app_root: renames by id."""

    def __init__(self, client: _FolderClient) -> None:
        self._client = client

    def update(self, fileId: str, body: dict, fields: str):  # noqa: N803 -- Drive's name
        self._client.folders[fileId] = body["name"]
        return types.SimpleNamespace(execute=lambda: {"id": fileId})


class _FolderClient(DriveClient):
    """A DriveClient over an in-memory set of top-level folders (id -> name)."""

    def __init__(self, folders: dict[str, str]) -> None:
        self.folders = dict(folders)
        self.created: list[str] = []
        self._service = types.SimpleNamespace(files=lambda: _FakeFiles(self))

    def _find_folder(self, name: str, parent: str | None = None) -> str | None:
        return next((i for i, n in self.folders.items() if n == name), None)

    def ensure_folder(self, name: str = _APP_FOLDER_NAME, parent: str | None = None) -> str:
        found = self._find_folder(name)
        if found is not None:
            return found
        self.folders[f"new-{name}"] = name
        self.created.append(name)
        return f"new-{name}"


def test_the_legacy_backup_folder_is_renamed_in_place():
    """Same folder id, new name: backup rows point at files inside it."""
    drive = _FolderClient({"f1": "Game Platform Backups"})
    assert drive.app_root(create=True) == "f1"
    assert drive.folders == {"f1": _APP_FOLDER_NAME}
    assert drive.created == []


def test_the_current_folder_is_used_when_present():
    drive = _FolderClient({"f1": _APP_FOLDER_NAME, "f0": "Game Platform Backups"})
    assert drive.app_root(create=False) == "f1"
    assert drive.folders["f0"] == "Game Platform Backups"


def test_no_folder_is_created_unless_asked():
    drive = _FolderClient({})
    assert drive.app_root(create=False) is None
    assert drive.created == []
    assert drive.app_root(create=True) == f"new-{_APP_FOLDER_NAME}"
