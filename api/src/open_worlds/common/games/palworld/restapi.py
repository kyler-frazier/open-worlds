"""Minimal client for a Palworld server's REST admin API (Palworld's RCON
replacement).

Served inside the world container and never published to the host; the worker
reaches it over the private ``palworld`` network with the world's admin
password (HTTP Basic, user ``admin``). Wraps only ``info``/``players``,
``save`` before a backup, and ``shutdown``.
"""

from __future__ import annotations

import json as jsonlib
from dataclasses import dataclass

import httpx2

from ...config import get_settings
from ...logging import get_logger

# The world answers these, and the world is untrusted. Real replies are a few
# KB; a player list for a full server stays well under this.
MAX_RESPONSE_BYTES = 1024 * 1024


def _read_capped(resp: httpx2.Response) -> bytes:
    body = bytearray()
    for chunk in resp.iter_bytes():
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise httpx2.DecodingError(f"response over {MAX_RESPONSE_BYTES} bytes")
    return bytes(body)


log = get_logger(__name__)


class PalApiError(Exception):
    """The server could not be reached, or answered with an error status."""


class PalApiAuthError(PalApiError):
    """The admin password was rejected (401/403)."""


@dataclass(frozen=True)
class Player:
    name: str
    player_id: str
    user_id: str
    ping: float
    level: int


class PalApiClient:
    """Talks to one world's REST admin API over the private Docker network."""

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        timeout: float | None = None,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self._base = f"http://{host}:{port}/v1/api"
        self._auth = ("admin", password)
        self._timeout = timeout if timeout is not None else get_settings().admin_api_timeout_seconds
        self._transport = transport

    def __enter__(self) -> PalApiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        url = f"{self._base}{path}"
        try:
            with httpx2.Client(timeout=self._timeout, transport=self._transport) as client:
                with client.stream(method, url, auth=self._auth, json=json) as resp:
                    content = _read_capped(resp)
        except httpx2.HTTPError as exc:
            raise PalApiError(f"Could not reach the world's admin API: {exc}") from exc
        if resp.status_code in (401, 403):
            raise PalApiAuthError("The world's admin API rejected our credentials.")
        if resp.status_code >= 400:
            raise PalApiError(f"Admin API returned HTTP {resp.status_code}.")
        if not content:
            return {}
        try:
            body = jsonlib.loads(content)
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    # --- reads ---

    def info(self) -> dict:
        """Server name, version and build id. Doubles as a liveness probe."""
        return self._request("GET", "/info")

    def players(self) -> list[Player]:
        """The currently connected players (empty list when nobody is on)."""
        raw = self._request("GET", "/players").get("players") or []
        out: list[Player] = []
        for p in raw:
            if not isinstance(p, dict):
                continue
            out.append(
                Player(
                    name=str(p.get("name", "")),
                    player_id=str(p.get("playerId", "")),
                    user_id=str(p.get("userId", "")),
                    ping=float(p.get("ping") or 0.0),
                    level=int(p.get("level") or 0),
                )
            )
        return out

    # --- writes ---

    def save(self) -> None:
        """Flush the world to disk. The backup path's equivalent of save-all."""
        self._request("POST", "/save")

    def shutdown(self, seconds: int = 5, message: str = "") -> None:
        """Ask the server to save and exit after warning connected players."""
        self._request("POST", "/shutdown", json={"waittime": seconds, "message": message})
