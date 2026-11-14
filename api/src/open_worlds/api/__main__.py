"""Console entrypoint for running the API with uvicorn."""

from __future__ import annotations

import uvicorn

from ..common.config import get_settings


def main() -> None:
    s = get_settings()
    # Dev login trusts "the peer is loopback", so in that mode listen only on
    # loopback and take the peer from the socket, never from X-Forwarded-For.
    dev = s.dev_login
    uvicorn.run(
        "open_worlds.api.main:app",
        host="127.0.0.1" if dev else "0.0.0.0",
        port=8000,
        log_config=None,
        proxy_headers=not dev,
        # Only trust forwarded headers from the edge proxy (not any peer that can
        # reach uvicorn directly on the internal networks).
        forwarded_allow_ips=s.forwarded_allow_ips,
    )


if __name__ == "__main__":
    main()
