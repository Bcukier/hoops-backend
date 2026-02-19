"""
Security utilities: rate limiting, account lockout, password validation,
secure HTTP headers, and input sanitization.
"""
import re
import time
import logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import aiosqlite
from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger("hoops.security")

# ══════════════════════════════════════════════════════════════
# PASSWORD VALIDATION
# ══════════════════════════════════════════════════════════════

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


def validate_password(password: str) -> tuple[bool, str]:
    """
    Enforce password complexity requirements.
    Returns (is_valid, error_message).
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return False, f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
    if len(password) > MAX_PASSWORD_LENGTH:
        return False, f"Password must not exceed {MAX_PASSWORD_LENGTH} characters"
    if not re.search(r"[A-Z]", password):
        return False, "Password must contain at least one uppercase letter"
    if not re.search(r"[a-z]", password):
        return False, "Password must contain at least one lowercase letter"
    if not re.search(r"\d", password):
        return False, "Password must contain at least one digit"
    return True, ""


def validate_password_for_demo(password: str) -> tuple[bool, str]:
    """
    Relaxed validation for demo/dev mode. Still enforces minimum length.
    In production, use validate_password() instead.
    """
    if len(password) < 4:
        return False, "Password must be at least 4 characters"
    if len(password) > MAX_PASSWORD_LENGTH:
        return False, f"Password must not exceed {MAX_PASSWORD_LENGTH} characters"
    return True, ""


# ══════════════════════════════════════════════════════════════
# ACCOUNT LOCKOUT
# ══════════════════════════════════════════════════════════════

LOCKOUT_THRESHOLD = 5           # Failed attempts before lockout
LOCKOUT_WINDOW_MINUTES = 15     # Window for counting failures
LOCKOUT_DURATION_MINUTES = 30   # How long lockout lasts


async def record_login_attempt(
    db: aiosqlite.Connection,
    email: str,
    ip_address: str,
    success: bool,
):
    """Record a login attempt for lockout tracking."""
    await db.execute(
        """INSERT INTO login_attempts (email, ip_address, success, attempted_at)
           VALUES (?, ?, ?, ?)""",
        (email.lower().strip(), ip_address, 1 if success else 0,
         datetime.now(timezone.utc).isoformat()),
    )
    await db.commit()


async def is_account_locked(db: aiosqlite.Connection, email: str) -> bool:
    """Check if an account is locked out due to too many failed attempts."""
    window_start = (
        datetime.now(timezone.utc) - timedelta(minutes=LOCKOUT_WINDOW_MINUTES)
    ).isoformat()

    cursor = await db.execute(
        """SELECT COUNT(*) as c FROM login_attempts
           WHERE email = ? AND success = 0 AND attempted_at > ?""",
        (email.lower().strip(), window_start),
    )
    row = await cursor.fetchone()
    failed_count = row["c"]

    if failed_count < LOCKOUT_THRESHOLD:
        return False

    # Check if lockout has expired (based on most recent failure)
    cursor = await db.execute(
        """SELECT MAX(attempted_at) as last_fail FROM login_attempts
           WHERE email = ? AND success = 0""",
        (email.lower().strip(),),
    )
    row = await cursor.fetchone()
    if row and row["last_fail"]:
        last_fail = datetime.fromisoformat(row["last_fail"])
        if last_fail.tzinfo is None:
            last_fail = last_fail.replace(tzinfo=timezone.utc)
        lockout_until = last_fail + timedelta(minutes=LOCKOUT_DURATION_MINUTES)
        if datetime.now(timezone.utc) < lockout_until:
            remaining = (lockout_until - datetime.now(timezone.utc)).seconds // 60
            logger.warning(f"Account locked: {email} ({remaining}m remaining)")
            return True

    return False


# ══════════════════════════════════════════════════════════════
# IN-MEMORY RATE LIMITING
# ══════════════════════════════════════════════════════════════

class RateLimiter:
    """
    Sliding-window rate limiter using in-memory storage.
    For production with multiple workers, use Redis instead.
    """

    def __init__(self):
        # {key: [timestamp, timestamp, ...]}
        self._requests: dict[str, list[float]] = defaultdict(list)

    def _cleanup(self, key: str, window: float):
        """Remove timestamps outside the current window."""
        cutoff = time.monotonic() - window
        self._requests[key] = [
            t for t in self._requests[key] if t > cutoff
        ]

    def is_rate_limited(
        self, key: str, max_requests: int, window_seconds: float
    ) -> bool:
        """Check if a key has exceeded the rate limit."""
        self._cleanup(key, window_seconds)
        if len(self._requests[key]) >= max_requests:
            return True
        self._requests[key].append(time.monotonic())
        return False

    def get_retry_after(self, key: str, window_seconds: float) -> int:
        """Get seconds until the rate limit window resets."""
        if not self._requests[key]:
            return 0
        oldest = min(self._requests[key])
        return max(0, int(window_seconds - (time.monotonic() - oldest)))


# Global rate limiter instance
rate_limiter = RateLimiter()

# Rate limit configurations: (max_requests, window_seconds)
RATE_LIMITS = {
    "login":     (10, 300),    # 10 attempts per 5 minutes per IP
    "register":  (3,  3600),   # 3 registrations per hour per IP
    "signup":    (30, 60),     # 30 game signups per minute per user
    "api":       (120, 60),    # 120 API calls per minute per user
}


def check_rate_limit(category: str, key: str):
    """Raise 429 if rate limit exceeded."""
    if category not in RATE_LIMITS:
        return
    max_req, window = RATE_LIMITS[category]
    limiter_key = f"{category}:{key}"
    if rate_limiter.is_rate_limited(limiter_key, max_req, window):
        retry_after = rate_limiter.get_retry_after(limiter_key, window)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )


# ══════════════════════════════════════════════════════════════
# SECURE HEADERS MIDDLEWARE
# ══════════════════════════════════════════════════════════════

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # Prevent MIME type sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Prevent clickjacking
        response.headers["X-Frame-Options"] = "DENY"
        # XSS protection (legacy browsers)
        response.headers["X-XSS-Protection"] = "1; mode=block"
        # Referrer policy
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Permissions policy — disable unnecessary browser features
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        # Content Security Policy (API only — tighten for frontend)
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        # Strict Transport Security (enable behind TLS terminator)
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
        # Prevent caching of authenticated responses
        if request.headers.get("Authorization"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"

        return response


# ══════════════════════════════════════════════════════════════
# GLOBAL RATE LIMIT MIDDLEWARE
# ══════════════════════════════════════════════════════════════

class RateLimitMiddleware(BaseHTTPMiddleware):
    """Apply global API rate limiting per IP address."""

    async def dispatch(self, request: Request, call_next):
        # Skip health check
        if request.url.path == "/api/health":
            return await call_next(request)

        client_ip = get_client_ip(request)

        # Global per-IP rate limit
        max_req, window = RATE_LIMITS["api"]
        limiter_key = f"api:{client_ip}"
        if rate_limiter.is_rate_limited(limiter_key, max_req, window):
            retry_after = rate_limiter.get_retry_after(limiter_key, window)
            return JSONResponse(
                status_code=429,
                content={"detail": f"Rate limit exceeded. Retry in {retry_after}s."},
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)


# ══════════════════════════════════════════════════════════════
# INPUT SANITIZATION
# ══════════════════════════════════════════════════════════════

def sanitize_string(value: str, max_length: int = 200) -> str:
    """Basic input sanitization: strip, truncate, remove control characters, escape HTML."""
    if not value:
        return ""
    import html
    # Remove null bytes and control characters (except newline/tab)
    cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', value)
    # HTML-escape to prevent XSS
    cleaned = html.escape(cleaned, quote=True)
    return cleaned.strip()[:max_length]


def validate_email_format(email: str) -> bool:
    """Basic email format validation."""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email)) and len(email) <= 254


def validate_phone(phone: str) -> bool:
    """Basic phone number validation (digits, dashes, spaces, parens, plus)."""
    if not phone:
        return True  # Phone is optional
    cleaned = re.sub(r'[\s\-\(\)\+]', '', phone)
    return cleaned.isdigit() and 7 <= len(cleaned) <= 15


# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

def get_client_ip(request: Request) -> str:
    """
    Extract client IP, checking X-Forwarded-For for reverse proxies.
    Only trust the first hop (leftmost) in X-Forwarded-For.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # Take the first IP (client IP from the proxy perspective)
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
