"""
Authentication utilities: hardened password hashing, JWT tokens with
revocation support, and database-verified auth dependencies.
"""
import os
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from jose import jwt, JWTError
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.database import get_db

SECRET_KEY = os.environ.get(
    "HOOPS_SECRET_KEY",
    "hoops-dev-secret-key-CHANGE-IN-PRODUCTION-" + secrets.token_hex(16),
)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 30  # Long session per spec

# Password hashing parameters
HASH_ALGORITHM = "sha512"
HASH_ITERATIONS = 600_000  # OWASP 2023 recommendation for PBKDF2-SHA512
SALT_LENGTH = 32           # 256 bits

security = HTTPBearer(auto_error=False)


# ══════════════════════════════════════════════════════════════
# PASSWORD HASHING (PBKDF2-SHA512)
# ══════════════════════════════════════════════════════════════

def hash_password(password: str) -> str:
    """
    Hash a password using PBKDF2-HMAC-SHA512 with a random salt.
    Format: $pbkdf2-sha512$iterations$salt_hex$hash_hex
    """
    salt = secrets.token_bytes(SALT_LENGTH)
    h = hashlib.pbkdf2_hmac(
        HASH_ALGORITHM,
        password.encode("utf-8"),
        salt,
        HASH_ITERATIONS,
    )
    return f"$pbkdf2-sha512${HASH_ITERATIONS}${salt.hex()}${h.hex()}"


def verify_password(plain: str, stored_hash: str) -> bool:
    """
    Verify a password against its stored hash using timing-safe comparison.
    Supports both new format ($pbkdf2-sha512$...) and legacy format (salt$hash).
    """
    try:
        if stored_hash.startswith("$pbkdf2-sha512$"):
            # New format: $pbkdf2-sha512$iterations$salt_hex$hash_hex
            parts = stored_hash.split("$")
            # parts = ['', 'pbkdf2-sha512', iterations, salt_hex, hash_hex]
            iterations = int(parts[2])
            salt = bytes.fromhex(parts[3])
            expected = bytes.fromhex(parts[4])
            computed = hashlib.pbkdf2_hmac(
                HASH_ALGORITHM,
                plain.encode("utf-8"),
                salt,
                iterations,
            )
        else:
            # Legacy format: salt_hex$hash_hex (sha256, 100k iterations)
            salt_hex, expected_hex = stored_hash.split("$", 1)
            expected = bytes.fromhex(expected_hex)
            computed = hashlib.pbkdf2_hmac(
                "sha256",
                plain.encode("utf-8"),
                salt_hex.encode("utf-8"),
                100_000,
            )

        # Timing-safe comparison to prevent timing attacks
        return hmac.compare_digest(computed, expected)
    except (ValueError, IndexError, KeyError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """Check if a password hash uses the legacy format and needs upgrading."""
    return not stored_hash.startswith("$pbkdf2-sha512$")


# ══════════════════════════════════════════════════════════════
# JWT TOKENS
# ══════════════════════════════════════════════════════════════

def create_access_token(player_id: int, role: str) -> tuple[str, str]:
    """
    Create a JWT access token with a unique JTI for revocation support.
    Returns (token_string, jti).
    """
    jti = secrets.token_hex(16)
    expire = datetime.now(timezone.utc) + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": str(player_id),
        "role": role,
        "jti": jti,
        "iat": datetime.now(timezone.utc),
        "exp": expire,
    }
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    return token, jti


def decode_token(token: str) -> dict:
    """Decode and validate a JWT token."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if "sub" not in payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token payload",
            )
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )


# ══════════════════════════════════════════════════════════════
# TOKEN BLACKLIST
# ══════════════════════════════════════════════════════════════

async def blacklist_token(jti: str, player_id: int, expires_at: str):
    """Add a token to the blacklist (for logout / forced revocation)."""
    db = await get_db()
    try:
        await db.execute(
            """INSERT OR IGNORE INTO token_blacklist (jti, player_id, expires_at)
               VALUES (?, ?, ?)""",
            (jti, player_id, expires_at),
        )
        await db.commit()
    finally:
        await db.close()


async def is_token_blacklisted(db, jti: str) -> bool:
    """Check if a token has been revoked."""
    cursor = await db.execute(
        "SELECT jti FROM token_blacklist WHERE jti = ?", (jti,)
    )
    return (await cursor.fetchone()) is not None


# ══════════════════════════════════════════════════════════════
# AUTH DEPENDENCIES (DB-VERIFIED)
# ══════════════════════════════════════════════════════════════

async def get_current_player_id(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> int:
    """
    Extract, validate, and DB-verify the current player from the JWT.
    Checks: token validity, blacklist, player exists, player is approved.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    payload = decode_token(credentials.credentials)
    player_id = int(payload["sub"])
    jti = payload.get("jti", "")

    # Verify against database
    db = await get_db()
    try:
        # Check token blacklist
        if jti and await is_token_blacklisted(db, jti):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has been revoked",
            )

        # Verify player still exists and is active
        cursor = await db.execute(
            "SELECT id, role, status FROM players WHERE id = ?", (player_id,)
        )
        player = await cursor.fetchone()
        if not player:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Player account not found",
            )
        if player["status"] != "approved":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account is not active",
            )

        # Verify role hasn't been downgraded since token was issued
        token_role = payload.get("role")
        if token_role != player["role"]:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Permissions changed — please log in again",
            )

        return player_id
    finally:
        await db.close()


async def require_owner(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> int:
    """Require the current user to be a verified owner."""
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    payload = decode_token(credentials.credentials)
    if payload.get("role") != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner privileges required",
        )

    player_id = int(payload["sub"])
    jti = payload.get("jti", "")

    # DB verification
    db = await get_db()
    try:
        if jti and await is_token_blacklisted(db, jti):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has been revoked",
            )

        cursor = await db.execute(
            "SELECT role, status FROM players WHERE id = ?", (player_id,)
        )
        player = await cursor.fetchone()
        if not player or player["role"] != "owner" or player["status"] != "approved":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Owner privileges required",
            )

        return player_id
    finally:
        await db.close()
