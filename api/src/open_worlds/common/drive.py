"""Per-user Google Drive access for backups.

Uses the narrow ``drive.file`` scope: the app sees only files it created.
Credentials are rebuilt on demand from the user's encrypted refresh token.
"""

from __future__ import annotations

from typing import Any

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from .config import get_settings

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
_APP_FOLDER_NAME = "Open Worlds Backups"
# Names from before the platform was renamed; app_root renames one in place.
_LEGACY_APP_FOLDER_NAMES = ("Game Platform Backups",)


def _credentials(refresh_token: str) -> Credentials:
    s = get_settings()
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=s.google_client_id,
        client_secret=s.google_client_secret,
        scopes=[DRIVE_SCOPE],
    )


def is_revoked_grant(exc: BaseException) -> bool:
    """Whether a Drive failure means the user's refresh token is dead.

    Google answers a revoked or expired token with ``invalid_grant``; only
    re-linking fixes it, so callers unlink and the dashboard shows it.
    """
    return isinstance(exc, RefreshError) and "invalid_grant" in str(exc)


def unlink_if_revoked(exc: BaseException, owner: object) -> bool:
    """Drop a dead Drive link on ``owner``; returns True if it did."""
    if not is_revoked_grant(exc):
        return False
    owner.drive_linked = False  # type: ignore[attr-defined]
    owner.drive_refresh_token_enc = None  # type: ignore[attr-defined]
    return True


def is_not_found(exc: BaseException) -> bool:
    """Whether Drive answered "no such file" for this app's credentials.

    Under ``drive.file`` another app's or user's file is also a 404, which is
    what makes a raw file id from a client safe to accept.
    """
    return isinstance(exc, HttpError) and exc.resp.status == 404


DRIVE_REVOKED_MESSAGE = (
    "Google Drive access was revoked or expired; re-link Drive to resume backups."
)


class DriveFileTooLarge(Exception):
    """A download passed its byte cap; the partial file is left for the caller."""


class DriveClient:
    def __init__(self, refresh_token: str) -> None:
        self._service = build(
            "drive", "v3", credentials=_credentials(refresh_token), cache_discovery=False
        )

    def ensure_folder(self, name: str = _APP_FOLDER_NAME, parent: str | None = None) -> str:
        query = f"mimeType='application/vnd.google-apps.folder' and trashed=false and name='{name}'"
        if parent:
            query += f" and '{parent}' in parents"
        existing = (
            self._service.files().list(q=query, fields="files(id,name)", spaces="drive").execute()
        )
        files = existing.get("files", [])
        if files:
            return files[0]["id"]
        metadata: dict[str, Any] = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent:
            metadata["parents"] = [parent]
        created = self._service.files().create(body=metadata, fields="id").execute()
        return created["id"]

    def upload(self, local_path: str, filename: str, parent_folder_id: str) -> str:
        """Resumable upload of a local file; returns the Drive file id."""
        media = MediaFileUpload(local_path, resumable=True)
        metadata = {"name": filename, "parents": [parent_folder_id]}
        request = self._service.files().create(body=metadata, media_body=media, fields="id")
        response = None
        while response is None:
            _status, response = request.next_chunk()
        return response["id"]

    def download(self, file_id: str, local_path: str, max_bytes: int | None = None) -> None:
        """Stream a Drive file we previously created down to a local path.

        A file in the user's Drive is theirs to replace, so it is no more
        trusted than an upload: past ``max_bytes`` the download stops.
        """
        request = self._service.files().get_media(fileId=file_id)
        with open(local_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _status, done = downloader.next_chunk()
                if max_bytes is not None and fh.tell() > max_bytes:
                    raise DriveFileTooLarge(f"Drive file is over {max_bytes} bytes")

    def delete(self, file_id: str) -> None:
        self._service.files().delete(fileId=file_id).execute()

    def world_folder(self, game: str, subdomain: str) -> str:
        """The folder a world's backups belong in, created if missing.

        ``<app>/<GAME>/<subdomain>/``. The game level is the only record of an
        archive's game once its world and backup rows are deleted.
        """
        root = self.app_root(create=True)
        assert root is not None
        return self.ensure_folder(subdomain, parent=self.ensure_folder(game, parent=root))

    def app_root(self, *, create: bool) -> str | None:
        """The app's top-level backup folder, renaming a legacy one in place.

        A rename keeps the file ids that backup rows point at. Returns None
        when there is none and ``create`` is False.
        """
        found = self._find_folder(_APP_FOLDER_NAME)
        if found is not None:
            return found
        for legacy in _LEGACY_APP_FOLDER_NAMES:
            old = self._find_folder(legacy)
            if old is not None:
                self.rename(old, _APP_FOLDER_NAME)
                return old
        return self.ensure_folder() if create else None

    def rename(self, file_id: str, name: str) -> None:
        self._service.files().update(fileId=file_id, body={"name": name}, fields="id").execute()

    def list_backups(self, games: set[str]) -> list[dict[str, Any]]:
        """Every archive this app has stored in the user's Drive.

        Backup rows are deleted with their world but the files stay, using the
        user's quota; listing is the only way to find them. The folder layout
        recovers each file's world and game. ``drive.file`` keeps the user's
        other content out of the result.
        """
        root = self.app_root(create=False)
        if root is None:
            return []
        out: list[dict[str, Any]] = []

        def collect(parent: str, subdomain: str, game: str | None) -> None:
            for f in self._children(parent, folders=False):
                out.append(
                    {
                        "drive_file_id": f["id"],
                        "filename": f["name"],
                        "subdomain": subdomain,
                        "game": game,
                        "size_bytes": int(f["size"]) if f.get("size") else None,
                        "modified_at": f.get("modifiedTime"),
                    }
                )

        # Both layouts, permanently: a folder named for a game holds subdomain
        # folders; anything else is a subdomain folder from before the game
        # level. Archives of deleted worlds cannot be moved, since nothing
        # records their game.
        for child in self._children(root, folders=True):
            if child["name"] in games:
                for world in self._children(child["id"], folders=True):
                    collect(world["id"], world["name"], child["name"])
            else:
                collect(child["id"], child["name"], None)
        return out

    def _find_folder(self, name: str, parent: str | None = None) -> str | None:
        query = f"mimeType='application/vnd.google-apps.folder' and trashed=false and name='{name}'"
        if parent:
            query += f" and '{parent}' in parents"
        res = self._service.files().list(q=query, fields="files(id)", spaces="drive").execute()
        files = res.get("files", [])
        return files[0]["id"] if files else None

    def _children(self, parent: str, *, folders: bool) -> list[dict[str, Any]]:
        op = "=" if folders else "!="
        query = (
            f"'{parent}' in parents and trashed=false "
            f"and mimeType {op} 'application/vnd.google-apps.folder'"
        )
        out: list[dict[str, Any]] = []
        page: str | None = None
        while True:
            res = (
                self._service.files()
                .list(
                    q=query,
                    fields="nextPageToken, files(id,name,size,modifiedTime)",
                    spaces="drive",
                    pageSize=100,
                    pageToken=page,
                )
                .execute()
            )
            out.extend(res.get("files", []))
            page = res.get("nextPageToken")
            if not page:
                return out

    def get_file(self, file_id: str) -> dict[str, Any]:
        """Name and size for one file, or raise HttpError 404 if it isn't ours."""
        return (
            self._service.files().get(fileId=file_id, fields="id,name,size,modifiedTime").execute()
        )
