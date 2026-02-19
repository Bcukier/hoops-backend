"""
Hoops — Pickup Basketball Game Manager
FastAPI backend with SQLite, background scheduler, and security hardening.
"""
import logging
import csv
import io
import os
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Request, status, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.database import get_db, init_db, get_setting, set_setting
from app.auth import (
    hash_password,
    verify_password,
    needs_rehash,
    create_access_token,
    decode_token,
    blacklist_token,
    get_current_player_id,
    require_owner,
)
from app.models import (
    LoginRequest,
    TokenResponse,
    PlayerCreate,
    PlayerOut,
    PlayerOutPublic,
    PlayerUpdate,
    PlayerAdminUpdate,
    GameCreate,
    GameOut,
    SignupOut,
    SettingsOut,
    SettingsUpdate,
    LocationCreate,
)
from app.algorithms import run_random_selection
from app.notifications import (
    notify_game_signup_open,
    notify_waitlist_promotion,
    notify_owner_player_drop,
    notify_owners_new_signup,
)
from app.security import (
    SecurityHeadersMiddleware,
    RateLimitMiddleware,
    check_rate_limit,
    record_login_attempt,
    is_account_locked,
    validate_password_for_demo as validate_pw,
    validate_email_format,
    validate_phone,
    sanitize_string,
    get_client_ip,
)
from app.scheduler import scheduler, schedule_game_notifications

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hoops")

# Environment
DEMO_MODE = os.environ.get("HOOPS_DEMO_MODE", "1") == "1"
ALLOWED_ORIGINS = os.environ.get(
    "HOOPS_ALLOWED_ORIGINS",
    "http://localhost:3000,http://localhost:5173,http://localhost:8000"
).split(",")


# ── Lifespan ──────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await seed_demo_data()
    await scheduler.start()
    logger.info("🏀 Hoops backend ready (demo=%s)", DEMO_MODE)
    yield
    await scheduler.stop()
    logger.info("🏀 Hoops backend shut down")


app = FastAPI(
    title="Hoops — Pickup Basketball Manager",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs" if DEMO_MODE else None,     # Disable Swagger in production
    redoc_url="/redoc" if DEMO_MODE else None,
)

# ── Middleware (applied bottom-to-top) ────────────────────────
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if not DEMO_MODE else ["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)


# ── Helpers ───────────────────────────────────────────────────
def row_to_dict(row) -> dict:
    if row is None:
        return {}
    return dict(row)


def player_from_row(row) -> PlayerOut:
    d = row_to_dict(row)
    return PlayerOut(
        id=d["id"], name=d["name"], email=d["email"], mobile=d.get("mobile", ""),
        role=d["role"], priority=d["priority"], status=d["status"],
        notif_pref=d["notif_pref"], created_at=str(d["created_at"]),
    )


async def get_player_or_404(db, player_id: int):
    cursor = await db.execute("SELECT * FROM players WHERE id = ?", (player_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Player not found")
    return row


# ── Demo Data Seed ────────────────────────────────────────────
async def seed_demo_data():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) as c FROM players")
        row = await cursor.fetchone()
        if row["c"] > 0:
            return

        demo_players = [
            ("Ben Carter", "ben@example.com", "555-0101", "owner", "high"),
            ("Mason Levy", "mason@example.com", "555-0102", "owner", "high"),
            ("Jordan Smith", "jordan@example.com", "555-0103", "player", "high"),
            ("Alex Rivera", "alex@example.com", "555-0104", "player", "standard"),
            ("Chris Park", "chris@example.com", "555-0105", "player", "standard"),
            ("Taylor Kim", "taylor@example.com", "555-0106", "player", "standard"),
            ("Sam Washington", "sam@example.com", "555-0107", "player", "standard"),
            ("Morgan Lee", "morgan@example.com", "555-0108", "player", "low"),
            ("Jamie Chen", "jamie@example.com", "555-0109", "player", "standard"),
            ("Drew Thompson", "drew@example.com", "555-0110", "player", "standard"),
        ]
        pw_hash = hash_password("pass123")
        for name, email, mobile, role, priority in demo_players:
            await db.execute(
                """INSERT INTO players (name, email, mobile, password_hash, role, priority, status, notif_pref)
                   VALUES (?, ?, ?, ?, ?, ?, 'approved', 'email')""",
                (name, email, mobile, pw_hash, role, priority),
            )
        for name, email, mobile in [
            ("Casey Williams", "casey@example.com", "555-0111"),
            ("Riley Davis", "riley@example.com", "555-0112"),
        ]:
            await db.execute(
                """INSERT INTO players (name, email, mobile, password_hash, role, priority, status, notif_pref)
                   VALUES (?, ?, ?, ?, 'player', 'standard', 'pending', 'email')""",
                (name, email, mobile, pw_hash),
            )

        now = datetime.now(timezone.utc)
        tomorrow = (now + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
        game2 = (now + timedelta(days=3)).replace(hour=19, minute=0, second=0, microsecond=0)

        await db.execute(
            """INSERT INTO games (date, location, algorithm, cap, cap_enabled, created_by,
                                  notified_at, phase, selection_done)
               VALUES (?, 'Central Park Courts', 'first_come', 12, 1, 1, ?, 'active', 1)""",
            (tomorrow.isoformat(), now.isoformat()),
        )
        await db.execute(
            """INSERT INTO games (date, location, algorithm, cap, cap_enabled, created_by,
                                  notified_at, phase, selection_done)
               VALUES (?, 'YMCA Gym', 'random', 10, 1, 1, ?, 'signup', 0)""",
            (game2.isoformat(), now.isoformat()),
        )

        for pid in [1, 3, 4, 5, 6, 7]:
            owner_added = 1 if pid in [1, 3] else 0
            await db.execute(
                "INSERT INTO game_signups (game_id, player_id, status, owner_added) VALUES (1,?,?,?)",
                (pid, 'in', owner_added),
            )
        for pid in [2, 4, 5, 6, 7, 9]:
            owner_added = 1 if pid == 2 else 0
            await db.execute(
                "INSERT INTO game_signups (game_id, player_id, status, owner_added) VALUES (2,?,?,?)",
                (pid, 'pending', owner_added),
            )

        await db.commit()
        logger.info("Demo data seeded")
    finally:
        await db.close()


# ══════════════════════════════════════════════════════════════
# AUTH ROUTES
# ══════════════════════════════════════════════════════════════

@app.post("/api/auth/login", response_model=TokenResponse)
async def login(req: LoginRequest, request: Request):
    client_ip = get_client_ip(request)

    # Rate limit login attempts per IP
    check_rate_limit("login", client_ip)

    email = sanitize_string(req.email.lower().strip(), 254)

    db = await get_db()
    try:
        # Check account lockout
        if await is_account_locked(db, email):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Account temporarily locked due to too many failed attempts. Try again later.",
            )

        cursor = await db.execute(
            "SELECT * FROM players WHERE email = ?", (email,)
        )
        player = await cursor.fetchone()

        if not player or not verify_password(req.password, player["password_hash"]):
            # Record failed attempt
            await record_login_attempt(db, email, client_ip, success=False)
            # Generic error to avoid user enumeration
            raise HTTPException(
                status_code=401,
                detail="Invalid email or password",
            )

        if player["status"] == "pending":
            raise HTTPException(status_code=403, detail="Account pending approval")
        if player["status"] == "denied":
            raise HTTPException(status_code=403, detail="Account denied")

        # Record successful attempt (resets lockout window)
        await record_login_attempt(db, email, client_ip, success=True)

        # Transparent password rehash if using legacy format
        if needs_rehash(player["password_hash"]):
            new_hash = hash_password(req.password)
            await db.execute(
                "UPDATE players SET password_hash = ? WHERE id = ?",
                (new_hash, player["id"]),
            )
            await db.commit()
            logger.info(f"Rehashed password for player {player['id']}")

        token, jti = create_access_token(player["id"], player["role"])
        return TokenResponse(
            access_token=token,
            player=player_from_row(player),
        )
    finally:
        await db.close()


@app.post("/api/auth/register", response_model=PlayerOut)
async def register(req: PlayerCreate, request: Request):
    client_ip = get_client_ip(request)
    check_rate_limit("register", client_ip)

    # Sanitize inputs
    name = sanitize_string(req.name, 100)
    email = sanitize_string(req.email.lower().strip(), 254)
    mobile = sanitize_string(req.mobile, 20)

    if not name:
        raise HTTPException(400, "Name is required")
    if not validate_email_format(email):
        raise HTTPException(400, "Invalid email format")
    if mobile and not validate_phone(mobile):
        raise HTTPException(400, "Invalid phone number format")

    # Validate password
    valid, msg = validate_pw(req.password)
    if not valid:
        raise HTTPException(400, msg)

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM players WHERE email = ?", (email,)
        )
        if await cursor.fetchone():
            raise HTTPException(status_code=400, detail="Email already registered")

        pw_hash = hash_password(req.password)
        notif_pref = req.notif_pref if req.notif_pref in ("email", "sms", "push") else "email"

        cursor = await db.execute(
            """INSERT INTO players (name, email, mobile, password_hash, notif_pref)
               VALUES (?, ?, ?, ?, ?)""",
            (name, email, mobile, pw_hash, notif_pref),
        )
        await db.commit()
        player_id = cursor.lastrowid

        notify_setting = await get_setting(db, "notify_owner_new_signup")
        if notify_setting == "1":
            await notify_owners_new_signup(db, name, email)

        cursor = await db.execute("SELECT * FROM players WHERE id = ?", (player_id,))
        return player_from_row(await cursor.fetchone())
    finally:
        await db.close()


@app.post("/api/auth/logout")
async def logout(request: Request):
    """Revoke the current token by adding it to the blacklist."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return {"message": "Logged out"}

    token = auth_header.split(" ", 1)[1]
    try:
        payload = decode_token(token)
        jti = payload.get("jti")
        player_id = int(payload["sub"])
        exp = payload.get("exp", 0)
        # Convert exp to ISO string
        expires_at = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
        if jti:
            await blacklist_token(jti, player_id, expires_at)
    except Exception:
        pass  # Token invalid/expired — already effectively logged out

    return {"message": "Logged out successfully"}


@app.post("/api/auth/reset-password")
async def request_password_reset(email: str = Query(...)):
    """In production, sends a password reset email. For now, just acknowledges."""
    return {"message": "If an account with that email exists, a reset link has been sent."}


# ══════════════════════════════════════════════════════════════
# PLAYER ROUTES
# ══════════════════════════════════════════════════════════════

@app.get("/api/players/me", response_model=PlayerOut)
async def get_me(player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        return player_from_row(await get_player_or_404(db, player_id))
    finally:
        await db.close()


@app.patch("/api/players/me", response_model=PlayerOut)
async def update_me(req: PlayerUpdate, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        updates, params = [], []
        if req.name is not None:
            name = sanitize_string(req.name, 100)
            if not name:
                raise HTTPException(400, "Name cannot be empty")
            updates.append("name = ?")
            params.append(name)
        if req.email is not None:
            email = sanitize_string(req.email.lower().strip(), 254)
            if not validate_email_format(email):
                raise HTTPException(400, "Invalid email format")
            cursor = await db.execute(
                "SELECT id FROM players WHERE email = ? AND id != ?", (email, player_id)
            )
            if await cursor.fetchone():
                raise HTTPException(400, "Email already in use")
            updates.append("email = ?")
            params.append(email)
        if req.mobile is not None:
            mobile = sanitize_string(req.mobile, 20)
            if mobile and not validate_phone(mobile):
                raise HTTPException(400, "Invalid phone number")
            updates.append("mobile = ?")
            params.append(mobile)
        if req.password is not None:
            valid, msg = validate_pw(req.password)
            if not valid:
                raise HTTPException(400, msg)
            updates.append("password_hash = ?")
            params.append(hash_password(req.password))
        if req.notif_pref is not None:
            if req.notif_pref not in ("email", "sms", "push"):
                raise HTTPException(400, "Invalid notification preference")
            updates.append("notif_pref = ?")
            params.append(req.notif_pref)

        if updates:
            updates.append("updated_at = ?")
            params.append(datetime.now(timezone.utc).isoformat())
            params.append(player_id)
            await db.execute(
                f"UPDATE players SET {', '.join(updates)} WHERE id = ?", params
            )
            await db.commit()

        return player_from_row(await get_player_or_404(db, player_id))
    finally:
        await db.close()


# ══════════════════════════════════════════════════════════════
# PLAYER MANAGEMENT (Owner only)
# ══════════════════════════════════════════════════════════════

@app.get("/api/admin/players", response_model=list[PlayerOut])
async def list_players(
    status_filter: str = Query(None, alias="status"),
    owner_id: int = Depends(require_owner),
):
    db = await get_db()
    try:
        if status_filter:
            if status_filter not in ("pending", "approved", "denied"):
                raise HTTPException(400, "Invalid status filter")
            cursor = await db.execute(
                "SELECT * FROM players WHERE status = ? ORDER BY created_at DESC",
                (status_filter,),
            )
        else:
            cursor = await db.execute("SELECT * FROM players ORDER BY created_at DESC")
        return [player_from_row(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


@app.get("/api/admin/players/pending-count")
async def pending_count(owner_id: int = Depends(require_owner)):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) as c FROM players WHERE status = 'pending'"
        )
        return {"count": (await cursor.fetchone())["c"]}
    finally:
        await db.close()


@app.patch("/api/admin/players/{pid}", response_model=PlayerOut)
async def admin_update_player(
    pid: int, req: PlayerAdminUpdate, owner_id: int = Depends(require_owner),
):
    db = await get_db()
    try:
        await get_player_or_404(db, pid)
        updates, params = [], []
        if req.priority is not None:
            if req.priority not in ("high", "standard", "low"):
                raise HTTPException(400, "Invalid priority")
            updates.append("priority = ?")
            params.append(req.priority)
        if req.role is not None:
            if req.role not in ("owner", "player"):
                raise HTTPException(400, "Invalid role")
            updates.append("role = ?")
            params.append(req.role)
        if req.status is not None:
            if req.status not in ("approved", "denied", "pending"):
                raise HTTPException(400, "Invalid status")
            updates.append("status = ?")
            params.append(req.status)
        if updates:
            params.append(pid)
            await db.execute(
                f"UPDATE players SET {', '.join(updates)} WHERE id = ?", params
            )
            await db.commit()
        return player_from_row(await get_player_or_404(db, pid))
    finally:
        await db.close()


@app.post("/api/admin/players/{pid}/approve", response_model=PlayerOut)
async def approve_player(
    pid: int, priority: str = Query("standard"), owner_id: int = Depends(require_owner),
):
    if priority not in ("high", "standard", "low"):
        raise HTTPException(400, "Invalid priority")
    db = await get_db()
    try:
        await get_player_or_404(db, pid)
        await db.execute(
            "UPDATE players SET status = 'approved', priority = ? WHERE id = ?",
            (priority, pid),
        )
        await db.commit()
        return player_from_row(await get_player_or_404(db, pid))
    finally:
        await db.close()


@app.post("/api/admin/players/{pid}/deny")
async def deny_player(pid: int, owner_id: int = Depends(require_owner)):
    db = await get_db()
    try:
        await get_player_or_404(db, pid)
        await db.execute("UPDATE players SET status = 'denied' WHERE id = ?", (pid,))
        await db.commit()
        return {"message": "Player denied"}
    finally:
        await db.close()


@app.delete("/api/admin/players/{pid}")
async def delete_player(pid: int, owner_id: int = Depends(require_owner)):
    db = await get_db()
    try:
        if pid == owner_id:
            raise HTTPException(400, "Cannot delete yourself")
        await get_player_or_404(db, pid)
        await db.execute("DELETE FROM game_signups WHERE player_id = ?", (pid,))
        await db.execute("DELETE FROM players WHERE id = ?", (pid,))
        await db.commit()
        return {"message": "Player deleted"}
    finally:
        await db.close()


@app.post("/api/admin/players/{pid}/reset-password")
async def admin_reset_password(pid: int, owner_id: int = Depends(require_owner)):
    db = await get_db()
    try:
        await get_player_or_404(db, pid)
        temp_pw = "reset123"
        await db.execute(
            "UPDATE players SET password_hash = ? WHERE id = ?",
            (hash_password(temp_pw), pid),
        )
        await db.commit()
        return {"message": f"Password reset. Temporary password: {temp_pw}"}
    finally:
        await db.close()


@app.post("/api/admin/players/add", response_model=PlayerOut)
async def admin_add_player(
    req: PlayerCreate,
    priority: str = Query("standard"),
    owner_id: int = Depends(require_owner),
):
    if priority not in ("high", "standard", "low"):
        raise HTTPException(400, "Invalid priority")
    name = sanitize_string(req.name, 100)
    email = sanitize_string(req.email.lower().strip(), 254)
    if not validate_email_format(email):
        raise HTTPException(400, "Invalid email format")

    db = await get_db()
    try:
        cursor = await db.execute("SELECT id FROM players WHERE email = ?", (email,))
        if await cursor.fetchone():
            raise HTTPException(400, "Email already registered")
        pw_hash = hash_password(req.password if req.password else "welcome123")
        cursor = await db.execute(
            """INSERT INTO players (name, email, mobile, password_hash, priority, status, notif_pref)
               VALUES (?, ?, ?, ?, ?, 'approved', ?)""",
            (name, email, sanitize_string(req.mobile, 20), pw_hash, priority, req.notif_pref or "email"),
        )
        await db.commit()
        return player_from_row(await get_player_or_404(db, cursor.lastrowid))
    finally:
        await db.close()


@app.post("/api/admin/players/import")
async def import_players(
    file: UploadFile = File(...), owner_id: int = Depends(require_owner),
):
    """Import players from CSV. Format: name,email,mobile per line."""
    # Limit file size (1MB)
    content = await file.read()
    if len(content) > 1_048_576:
        raise HTTPException(400, "File too large (max 1MB)")

    db = await get_db()
    try:
        text = content.decode("utf-8", errors="ignore")
        reader = csv.reader(io.StringIO(text))
        added, errors = 0, []
        pw_hash = hash_password("welcome123")
        for i, row in enumerate(reader):
            if i > 500:  # Max 500 rows
                errors.append("Stopped at 500 rows")
                break
            if len(row) < 2:
                errors.append(f"Row {i+1}: insufficient columns")
                continue
            name = sanitize_string(row[0], 100)
            email = sanitize_string(row[1].lower().strip(), 254)
            mobile = sanitize_string(row[2] if len(row) > 2 else "", 20)
            if not name or not validate_email_format(email):
                errors.append(f"Row {i+1}: invalid name or email")
                continue
            cursor = await db.execute("SELECT id FROM players WHERE email = ?", (email,))
            if await cursor.fetchone():
                errors.append(f"Row {i+1}: {email} already exists")
                continue
            await db.execute(
                """INSERT INTO players (name, email, mobile, password_hash, status, notif_pref)
                   VALUES (?, ?, ?, ?, 'approved', 'email')""",
                (name, email, mobile, pw_hash),
            )
            added += 1
        await db.commit()
        return {"added": added, "errors": errors}
    finally:
        await db.close()


# ══════════════════════════════════════════════════════════════
# GAME ROUTES
# ══════════════════════════════════════════════════════════════

@app.get("/api/games", response_model=list[GameOut])
async def list_games(player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        player = await get_player_or_404(db, player_id)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

        cursor = await db.execute(
            "SELECT * FROM games WHERE date > ? AND closed = 0 ORDER BY date ASC",
            (cutoff,),
        )
        games = await cursor.fetchall()

        hp_delay = int(await get_setting(db, "high_priority_delay_minutes") or 60)
        alt_delay = int(await get_setting(db, "alternative_delay_minutes") or 1440)

        result = []
        for g in games:
            g_dict = row_to_dict(g)
            notified_at = g_dict.get("notified_at")
            if notified_at:
                notif_time = datetime.fromisoformat(notified_at)
                if notif_time.tzinfo is None:
                    notif_time = notif_time.replace(tzinfo=timezone.utc)
                elapsed_min = (datetime.now(timezone.utc) - notif_time).total_seconds() / 60
            else:
                elapsed_min = 0

            priority = player["priority"]
            role = player["role"]
            visible = (
                role == "owner"
                or priority == "high"
                or (priority == "standard" and elapsed_min >= hp_delay)
                or (priority == "low" and elapsed_min >= hp_delay + alt_delay)
            )
            if not visible:
                continue

            cursor2 = await db.execute(
                """SELECT gs.*, p.name as player_name
                   FROM game_signups gs JOIN players p ON p.id = gs.player_id
                   WHERE gs.game_id = ? ORDER BY gs.signed_up_at ASC""",
                (g_dict["id"],),
            )
            signups = [
                SignupOut(
                    id=s["id"], player_id=s["player_id"], player_name=s["player_name"],
                    signed_up_at=str(s["signed_up_at"]), status=s["status"],
                    owner_added=bool(s["owner_added"]),
                )
                for s in await cursor2.fetchall()
            ]
            result.append(GameOut(
                id=g_dict["id"], date=g_dict["date"], location=g_dict["location"],
                algorithm=g_dict["algorithm"], cap=g_dict["cap"],
                cap_enabled=bool(g_dict["cap_enabled"]),
                created_by=g_dict["created_by"], created_at=str(g_dict["created_at"]),
                notified_at=g_dict.get("notified_at"), phase=g_dict["phase"],
                selection_done=bool(g_dict["selection_done"]),
                closed=bool(g_dict["closed"]), signups=signups,
            ))
        return result
    finally:
        await db.close()


@app.post("/api/games", response_model=GameOut)
async def create_game(req: GameCreate, owner_id: int = Depends(require_owner)):
    # Validate algorithm
    if req.algorithm not in ("first_come", "random", "weighted"):
        raise HTTPException(400, "Invalid algorithm")

    # Validate date is in the future
    try:
        game_date = datetime.fromisoformat(req.date)
    except ValueError:
        raise HTTPException(400, "Invalid date format")

    location = sanitize_string(req.location, 200)

    db = await get_db()
    try:
        notified_at = None
        if not req.notify_future_at:
            notified_at = datetime.now(timezone.utc).isoformat()
            phase = "active" if req.algorithm == "first_come" else "signup"
        else:
            phase = "created"

        cursor = await db.execute(
            """INSERT INTO games (date, location, algorithm, cap, cap_enabled, created_by,
                                  notified_at, notify_future_at, phase, selection_done)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                req.date, location, req.algorithm, req.cap,
                1 if req.cap_enabled else 0, owner_id,
                notified_at, req.notify_future_at, phase,
                1 if req.algorithm == "first_come" else 0,
            ),
        )
        game_id = cursor.lastrowid

        for pid in req.owner_added_player_ids:
            # Verify player exists and is approved
            cursor2 = await db.execute(
                "SELECT id FROM players WHERE id = ? AND status = 'approved'", (pid,)
            )
            if not await cursor2.fetchone():
                continue
            initial_status = "in" if req.algorithm == "first_come" else "pending"
            await db.execute(
                """INSERT OR IGNORE INTO game_signups (game_id, player_id, status, owner_added)
                   VALUES (?, ?, ?, 1)""",
                (game_id, pid, initial_status),
            )
        await db.commit()

        # Schedule notifications via the background worker
        await schedule_game_notifications(game_id, req.notify_future_at)

        return await _get_game_out(db, game_id)
    finally:
        await db.close()


@app.post("/api/games/{game_id}/signup")
async def signup_for_game(
    game_id: int, request: Request,
    player_id: int = Depends(get_current_player_id),
):
    check_rate_limit("signup", str(player_id))

    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM games WHERE id = ?", (game_id,))
        game = await cursor.fetchone()
        if not game:
            raise HTTPException(404, "Game not found")
        if game["closed"]:
            raise HTTPException(400, "Game is closed")

        game_time = datetime.fromisoformat(game["date"])
        if game_time.tzinfo is None:
            game_time = game_time.replace(tzinfo=timezone.utc)
        if game_time < datetime.now(timezone.utc):
            raise HTTPException(400, "Cannot sign up for games that have started")

        cursor = await db.execute(
            "SELECT id FROM game_signups WHERE game_id = ? AND player_id = ?",
            (game_id, player_id),
        )
        if await cursor.fetchone():
            raise HTTPException(400, "Already signed up")

        algorithm = game["algorithm"]
        selection_done = game["selection_done"]
        cap = game["cap"] if game["cap_enabled"] else 999999

        if algorithm == "random" and not selection_done:
            signup_status = "pending"
        else:
            cursor = await db.execute(
                "SELECT COUNT(*) as c FROM game_signups WHERE game_id = ? AND status = 'in'",
                (game_id,),
            )
            in_count = (await cursor.fetchone())["c"]
            signup_status = "in" if in_count < cap else "waitlist"

        await db.execute(
            "INSERT INTO game_signups (game_id, player_id, status, owner_added) VALUES (?,?,?,0)",
            (game_id, player_id, signup_status),
        )
        await db.commit()

        return {
            "status": signup_status,
            "message": {"in": "You're in the game!", "waitlist": "Added to the waitlist.",
                        "pending": "Signed up — selection pending."}.get(signup_status, "Signed up."),
        }
    finally:
        await db.close()


@app.post("/api/games/{game_id}/drop")
async def drop_from_game(
    game_id: int, player_id: int = Depends(get_current_player_id),
):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM games WHERE id = ?", (game_id,))
        game = await cursor.fetchone()
        if not game:
            raise HTTPException(404, "Game not found")

        cursor = await db.execute(
            "SELECT * FROM game_signups WHERE game_id = ? AND player_id = ?",
            (game_id, player_id),
        )
        signup = await cursor.fetchone()
        if not signup:
            raise HTTPException(400, "Not signed up")

        was_in = signup["status"] == "in"
        signed_up_at = signup["signed_up_at"]

        await db.execute(
            "DELETE FROM game_signups WHERE game_id = ? AND player_id = ?",
            (game_id, player_id),
        )

        promoted_player = None
        if was_in:
            cursor = await db.execute(
                """SELECT gs.*, p.name as player_name FROM game_signups gs
                   JOIN players p ON p.id = gs.player_id
                   WHERE gs.game_id = ? AND gs.status = 'waitlist'
                   ORDER BY gs.signed_up_at ASC LIMIT 1""",
                (game_id,),
            )
            waitlisted = await cursor.fetchone()
            if waitlisted:
                await db.execute(
                    "UPDATE game_signups SET status = 'in' WHERE id = ?",
                    (waitlisted["id"],),
                )
                promoted_player = waitlisted["player_name"]
                await notify_waitlist_promotion(db, game_id, waitlisted["player_id"])

        await db.commit()

        # Notify owners about late drops
        try:
            signup_time = datetime.fromisoformat(str(signed_up_at))
            if signup_time.tzinfo is None:
                signup_time = signup_time.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - signup_time).total_seconds()
        except Exception:
            elapsed = 0

        if elapsed > 60 and (game["algorithm"] == "first_come" or game["selection_done"]):
            player = await get_player_or_404(db, player_id)
            drop_time = datetime.now(timezone.utc).strftime("%I:%M %p")
            await notify_owner_player_drop(db, game_id, player["name"], drop_time)

        result = {"message": "Removed from the game."}
        if promoted_player:
            result["promoted"] = promoted_player
        return result
    finally:
        await db.close()


@app.post("/api/games/{game_id}/run-selection")
async def run_selection(game_id: int, owner_id: int = Depends(require_owner)):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM games WHERE id = ?", (game_id,))
        game = await cursor.fetchone()
        if not game:
            raise HTTPException(404, "Game not found")
        if game["algorithm"] != "random":
            raise HTTPException(400, "Selection only applies to random algorithm")
        if game["selection_done"]:
            raise HTTPException(400, "Selection already done")

        result = await run_random_selection(db, game_id)
        return {
            "message": "Selection complete",
            "in_count": result["in_count"],
            "waitlist_count": result["waitlist_count"],
        }
    finally:
        await db.close()


@app.post("/api/games/{game_id}/close")
async def close_game(game_id: int, owner_id: int = Depends(require_owner)):
    db = await get_db()
    try:
        await db.execute(
            "UPDATE games SET closed = 1, phase = 'closed' WHERE id = ?", (game_id,)
        )
        await db.commit()
        return {"message": "Game closed"}
    finally:
        await db.close()


async def _get_game_out(db, game_id: int) -> GameOut:
    cursor = await db.execute("SELECT * FROM games WHERE id = ?", (game_id,))
    g = row_to_dict(await cursor.fetchone())
    cursor2 = await db.execute(
        """SELECT gs.*, p.name as player_name FROM game_signups gs
           JOIN players p ON p.id = gs.player_id
           WHERE gs.game_id = ? ORDER BY gs.signed_up_at ASC""",
        (game_id,),
    )
    signups = await cursor2.fetchall()
    return GameOut(
        id=g["id"], date=g["date"], location=g["location"],
        algorithm=g["algorithm"], cap=g["cap"], cap_enabled=bool(g["cap_enabled"]),
        created_by=g["created_by"], created_at=str(g["created_at"]),
        notified_at=g.get("notified_at"), phase=g["phase"],
        selection_done=bool(g["selection_done"]), closed=bool(g["closed"]),
        signups=[
            SignupOut(
                id=s["id"], player_id=s["player_id"], player_name=s["player_name"],
                signed_up_at=str(s["signed_up_at"]), status=s["status"],
                owner_added=bool(s["owner_added"]),
            )
            for s in signups
        ],
    )


# ══════════════════════════════════════════════════════════════
# SETTINGS ROUTES
# ══════════════════════════════════════════════════════════════

@app.get("/api/settings", response_model=SettingsOut)
async def get_settings(owner_id: int = Depends(require_owner)):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM settings")
        s = {r["key"]: r["value"] for r in await cursor.fetchall()}
        cursor = await db.execute("SELECT name FROM locations ORDER BY name")
        locs = [r["name"] for r in await cursor.fetchall()]
        return SettingsOut(
            default_cap=int(s.get("default_cap", 12)),
            cap_enabled=s.get("cap_enabled", "1") == "1",
            default_algorithm=s.get("default_algorithm", "first_come"),
            high_priority_delay_minutes=int(s.get("high_priority_delay_minutes", 60)),
            alternative_delay_minutes=int(s.get("alternative_delay_minutes", 1440)),
            random_wait_period_minutes=int(s.get("random_wait_period_minutes", 60)),
            notify_owner_new_signup=s.get("notify_owner_new_signup", "1") == "1",
            locations=locs,
        )
    finally:
        await db.close()


@app.patch("/api/settings")
async def update_settings(req: SettingsUpdate, owner_id: int = Depends(require_owner)):
    db = await get_db()
    try:
        mapping = {
            "default_cap": str(req.default_cap) if req.default_cap is not None else None,
            "cap_enabled": ("1" if req.cap_enabled else "0") if req.cap_enabled is not None else None,
            "default_algorithm": req.default_algorithm if req.default_algorithm in ("first_come", "random", "weighted") else None,
            "high_priority_delay_minutes": str(req.high_priority_delay_minutes) if req.high_priority_delay_minutes is not None else None,
            "alternative_delay_minutes": str(req.alternative_delay_minutes) if req.alternative_delay_minutes is not None else None,
            "random_wait_period_minutes": str(req.random_wait_period_minutes) if req.random_wait_period_minutes is not None else None,
            "notify_owner_new_signup": ("1" if req.notify_owner_new_signup else "0") if req.notify_owner_new_signup is not None else None,
        }
        for key, value in mapping.items():
            if value is not None:
                await set_setting(db, key, value)
        return {"message": "Settings updated"}
    finally:
        await db.close()


@app.post("/api/settings/locations")
async def add_location(req: LocationCreate, owner_id: int = Depends(require_owner)):
    db = await get_db()
    try:
        await db.execute("INSERT OR IGNORE INTO locations (name) VALUES (?)", (sanitize_string(req.name, 200),))
        await db.commit()
        return {"message": "Location added"}
    finally:
        await db.close()


@app.delete("/api/settings/locations/{name}")
async def remove_location(name: str, owner_id: int = Depends(require_owner)):
    db = await get_db()
    try:
        await db.execute("DELETE FROM locations WHERE name = ?", (name,))
        await db.commit()
        return {"message": "Location removed"}
    finally:
        await db.close()


@app.get("/api/settings/locations")
async def list_locations(player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT name FROM locations ORDER BY name")
        return [r["name"] for r in await cursor.fetchall()]
    finally:
        await db.close()


# ══════════════════════════════════════════════════════════════
# SCHEDULER STATUS & NOTIFICATION LOG (Owner only)
# ══════════════════════════════════════════════════════════════

@app.get("/api/admin/scheduler/jobs")
async def get_scheduler_jobs(
    game_id: int = Query(None),
    status_filter: str = Query(None, alias="status"),
    limit: int = Query(50, le=200),
    owner_id: int = Depends(require_owner),
):
    """View scheduler job status."""
    db = await get_db()
    try:
        query = "SELECT * FROM scheduler_jobs WHERE 1=1"
        params = []
        if game_id:
            query += " AND game_id = ?"
            params.append(game_id)
        if status_filter:
            query += " AND status = ?"
            params.append(status_filter)
        query += " ORDER BY scheduled_at DESC LIMIT ?"
        params.append(limit)
        cursor = await db.execute(query, params)
        return [row_to_dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


@app.get("/api/admin/notifications")
async def get_notification_log(
    limit: int = Query(50, le=200),
    owner_id: int = Depends(require_owner),
):
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT nl.*, p.name as recipient_name
               FROM notification_log nl
               JOIN players p ON p.id = nl.recipient_id
               ORDER BY nl.sent_at DESC LIMIT ?""",
            (limit,),
        )
        return [row_to_dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


# ══════════════════════════════════════════════════════════════
# HEALTH CHECK
# ══════════════════════════════════════════════════════════════

@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "hoops-backend", "version": "2.0.0"}


# ══════════════════════════════════════════════════════════════
# STATIC FILE SERVING
# ══════════════════════════════════════════════════════════════

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

if STATIC_DIR.exists():
    # Serve the SPA for any non-API route
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        # Try to serve a static file first
        file_path = STATIC_DIR / full_path
        if full_path and file_path.exists() and file_path.is_file():
            return FileResponse(file_path)
        # Fall back to index.html for SPA routing
        return FileResponse(STATIC_DIR / "index.html")
