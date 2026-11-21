"""Password hashing for the admin web credential.

PBKDF2-HMAC-SHA256 is stdlib-only, so the production image needs no new
dependency. Encoded form:
``pbkdf2_sha256$<iterations>$<salt_b64url>$<digest_b64url>``.
"""

import base64
import hashlib
import hmac
import secrets
from typing import Optional

ALGORITHM = "pbkdf2_sha256"
DEFAULT_ITERATIONS = 600_000
MIN_PRODUCTION_ITERATIONS = 100_000
MAX_ITERATIONS = 2_000_000
SALT_BYTES = 16
DIGEST_BYTES = 32


def _b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _parse(encoded: object):
    try:
        algorithm, iterations_text, salt_text, digest_text = str(encoded).split("$", 3)
        if algorithm != ALGORITHM:
            return None
        iterations = int(iterations_text)
        if not 1 <= iterations <= MAX_ITERATIONS:
            return None
        salt = _b64_decode(salt_text)
        digest = _b64_decode(digest_text)
    except (TypeError, ValueError):
        return None
    if len(salt) < 8 or len(digest) != DIGEST_BYTES:
        return None
    return iterations, salt, digest


def hash_password(
    password: str,
    *,
    iterations: int = DEFAULT_ITERATIONS,
    salt: Optional[bytes] = None,
) -> str:
    """Create a PBKDF2-SHA256 hash. Never store the password itself."""
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    iterations = int(iterations)
    if not 1 <= iterations <= MAX_ITERATIONS:
        raise ValueError("invalid iteration count")
    salt = secrets.token_bytes(SALT_BYTES) if salt is None else bytes(salt)
    if len(salt) < 8:
        raise ValueError("salt must be at least 8 bytes")
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return (
        f"{ALGORITHM}${iterations}${_b64_encode(salt)}${_b64_encode(digest)}"
    )


def verify_password(password: str, encoded: object) -> bool:
    """Constant-time verification; malformed or unsupported hashes fail closed."""
    parsed = _parse(encoded)
    if parsed is None or not isinstance(password, str):
        return False
    iterations, salt, expected = parsed
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(actual, expected)


def is_supported_password_hash(encoded: object, *, min_iterations: int = 1) -> bool:
    parsed = _parse(encoded)
    return parsed is not None and parsed[0] >= max(1, int(min_iterations))
