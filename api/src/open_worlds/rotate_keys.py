"""Re-encrypt every stored secret with the current Fernet key.

Step two of a rotation (deploy/RUNBOOK.md, "Rotating keys"): once the new key
is current and the old one is in PLATFORM_FERNET_OLD_KEYS, this rewrites each
stored value so the old key can then be dropped. Safe to run again.
"""

from __future__ import annotations

from sqlalchemy import select

from .common.crypto import reencrypt
from .common.db import session_scope
from .common.models import User, World

# Every encrypted column, by model.
ENCRYPTED = {
    User: ("drive_refresh_token_enc",),
    World: ("admin_password_enc", "server_password_enc"),
}


def rotate_all() -> int:
    count = 0
    with session_scope() as db:
        for model, columns in ENCRYPTED.items():
            for row in db.scalars(select(model)):
                for column in columns:
                    token = getattr(row, column)
                    if token:
                        setattr(row, column, reencrypt(token))
                        count += 1
    return count


def main() -> None:
    print(f"re-encrypted {rotate_all()} values with the current key")


if __name__ == "__main__":
    main()
