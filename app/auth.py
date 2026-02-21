"""
Authentication: PBKDF2-SHA512 passwords, JWT tokens with revocation, DB-verified auth.
"""
import os, hashlib, hmac, secrets
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
ACCESS_TOKEN_EXPIRE_DAYS = 30
HASH_ALGORITHM = "sha512"
HASH_ITERATIONS = 600_000
SALT_LENGTH = 32
security = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_LENGTH)
    h = hashlib.pbkdf2_hmac(HASH_ALGORITHM, password.encode("utf-8"), salt, HASH_ITERATIONS)
    return f"$pbkdf2-sha512${HASH_ITERATIONS}${salt.hex()}${h.hex()}"


def verify_password(plain: str, stored_hash: str) -> bool:
    try:
        if stored_hash.startswith("$pbkdf2-sha512$"):
            parts = stored_hash.split("$")
            iterations = int(parts[2])
            salt = bytes.fromhex(parts[3])
            expected = bytes.fromhex(parts[4])
            computed = hashlib.pbkdf2_hmac(HASH_ALGORITHM, plain.encode("utf-8"), salt, iterations)
        else:
            salt_hex, expected_hex = stored_hash.split("$", 1)
            expected = bytes.fromhex(expected_hex)
            computed = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt_hex.encode("utf-8"), 100_000)
        return hmac.compare_digest(computed, expected)
    except (ValueError, IndexError, KeyError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    return not stored_hash.startswith("$pbkdf2-sha512$")


def create_access_token(player_id: int, role: str) -> tuple[str, str]:
    jti = secrets.token_hex(16)
    expire = datetime.now(timezone.utc) + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    payload = {"sub": str(player_id), "role": role, "jti": jti,
               "iat": datetime.now(timezone.utc), "exp": expire}
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    return token, jti


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if "sub" not in payload:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")
        return payload
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")


async def blacklist_token(jti: str, player_id: int, expires_at: str):
    db = await get_db()
    try:
        await db.execute("INSERT OR IGNORE INTO token_blacklist (jti,player_id,expires_at) VALUES (?,?,?)",
                         (jti, player_id, expires_at))
        await db.commit()
    finally:
        await db.close()


async def is_token_blacklisted(db, jti: str) -> bool:
    cursor = await db.execute("SELECT jti FROM token_blacklist WHERE jti=?", (jti,))
    return (await cursor.fetchone()) is not None


async def get_current_player_id(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> int:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    payload = decode_token(credentials.credentials)
    player_id = int(payload["sub"])
    jti = payload.get("jti", "")
    db = await get_db()
    try:
        if jti and await is_token_blacklisted(db, jti):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has been revoked")
        cursor = await db.execute("SELECT id, status FROM players WHERE id=?", (player_id,))
        player = await cursor.fetchone()
        if not player:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Player account not found")
        if player["status"] != "approved":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is not active")
        return player_id
    finally:
        await db.close()


async def require_owner(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> int:
    """Require the current user to be an organizer of at least one group."""
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    payload = decode_token(credentials.credentials)
    player_id = int(payload["sub"])
    jti = payload.get("jti", "")
    db = await get_db()
    try:
        if jti and await is_token_blacklisted(db, jti):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has been revoked")
        cursor = await db.execute("SELECT status FROM players WHERE id=?", (player_id,))
        player = await cursor.fetchone()
        if not player or player["status"] != "approved":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Organizer privileges required")
        # Check group_members for organizer role
        cursor = await db.execute(
            "SELECT 1 FROM group_members WHERE player_id=? AND role='organizer' AND status='active' LIMIT 1",
            (player_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Organizer privileges required")
        return player_id
    finally:
        await db.close()


async def get_player_role(db, player_id: int) -> str:
    """Determine global role: 'owner' if organizer of any group, else 'player'."""
    cursor = await db.execute(
        "SELECT 1 FROM group_members WHERE player_id=? AND role='organizer' AND status='active' LIMIT 1",
        (player_id,))
    return "owner" if await cursor.fetchone() else "player"
