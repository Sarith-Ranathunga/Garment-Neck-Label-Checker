"""
Password hashing for operator accounts.

bcrypt rather than a bare digest: a password store is only as good as the
work factor in front of it, and SHA over a four-digit floor password is not
a password store. Cost 12 takes roughly a quarter of a second on the
supervisor workstation, which is invisible at a shift change and expensive
in bulk.

One caveat that belongs in the record rather than in a comment nobody reads:
the phones reach the host over plain HTTP, so a password posted to /api/login
crosses the factory LAN in the clear. Hashing protects the stored password if
the database is read; it does nothing against someone on the same network.
TLS on the host is the fix, and it is not done here.
"""
from __future__ import annotations

import bcrypt

# bcrypt truncates silently at 72 bytes, so the limit is stated and enforced
# rather than discovered later by a user whose long password half works.
MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_CHARS = 4


class PasswordError(ValueError):
    """A password that cannot be stored as given."""


def hash_password(raw: str) -> str:
    """Return a bcrypt hash, or raise PasswordError if the input is unusable."""
    if raw is None:
        raise PasswordError("a password is required")
    if len(raw) < MIN_PASSWORD_CHARS:
        raise PasswordError(
            f"password must be at least {MIN_PASSWORD_CHARS} characters")
    encoded = raw.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise PasswordError(
            f"password must be at most {MAX_PASSWORD_BYTES} bytes")
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(raw: str, stored: str | None) -> bool:
    """
    True when `raw` matches `stored`.

    Returns False rather than raising on a malformed or placeholder hash —
    accounts seeded with 'x' predate this module and must simply fail to sign
    in, not crash the login route for everyone else.
    """
    if not raw or not stored:
        return False
    try:
        return bcrypt.checkpw(raw.encode("utf-8"), stored.encode("utf-8"))
    except (ValueError, TypeError):
        return False
