"""Authlib OAuth client registration for Google login and Drive linking.

- ``google``: OpenID Connect login; Authlib handles state, nonce and PKCE.
- Drive linking is a separate, incremental consent for ``drive.file`` with
  offline access, so a refresh token is issued.
"""

from __future__ import annotations

from authlib.integrations.starlette_client import OAuth

from ..common.config import get_settings
from ..common.drive import DRIVE_SCOPE

_GOOGLE_DISCOVERY = "https://accounts.google.com/.well-known/openid-configuration"

oauth = OAuth()


def init_oauth() -> OAuth:
    s = get_settings()
    if "google" not in oauth._clients:
        oauth.register(
            name="google",
            client_id=s.google_client_id,
            client_secret=s.google_client_secret,
            server_metadata_url=_GOOGLE_DISCOVERY,
            client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
        )
    return oauth


def drive_authorize_params() -> dict[str, str]:
    """Extra params so Drive linking returns a durable refresh token."""
    return {
        "scope": f"openid {DRIVE_SCOPE}",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
