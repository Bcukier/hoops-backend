"""
GOATcommish — Multi-Group Pickup Basketball Manager
"""
import logging, csv, io, os, asyncio, uuid, secrets
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Request, status, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.database import get_db, init_db, get_setting, set_setting, ensure_group_settings
from app.auth import (
    hash_password, verify_password, needs_rehash, create_access_token,
    decode_token, blacklist_token, get_current_player_id, require_owner, get_player_role,
)
from app.models import *
from app.algorithms import run_random_selection
from app.notifications import (
    notify_game_signup_open, notify_batch_games_signup_open,
    notify_waitlist_promotion, notify_owner_player_drop,
    notify_owners_new_signup, notify_game_edited,
    notify_group_invitation, log_notification_config,
)
from app.security import (
    SecurityHeadersMiddleware, RateLimitMiddleware, check_rate_limit,
    record_login_attempt, is_account_locked,
    validate_password_for_demo as validate_pw, validate_email_format,
    validate_phone, sanitize_string, get_client_ip,
)
from app.scheduler import scheduler, schedule_game_notifications, Scheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hoops")

DEMO_MODE = os.environ.get("HOOPS_DEMO_MODE", "1") == "1"
ALLOWED_ORIGINS = os.environ.get(
    "HOOPS_ALLOWED_ORIGINS",
    "http://localhost:3000,http://localhost:5173,http://localhost:8000"
).split(",")


async def _bg_notify(coro_fn, *args):
    try:
        db = await get_db()
        try:
            await coro_fn(db, *args)
        finally:
            await db.close()
    except Exception as e:
        logger.error(f"Background notification error: {e}")

def bg_notify(coro_fn, *args):
    asyncio.create_task(_bg_notify(coro_fn, *args))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    if DEMO_MODE:
        await seed_demo_data()
    # Bootstrap superuser from env var
    su_email = os.environ.get("HOOPS_SUPERUSER_EMAIL", "").strip().lower()
    if su_email:
        db = await get_db()
        try:
            await db.execute("UPDATE players SET is_superuser=1 WHERE email=? COLLATE NOCASE", (su_email,))
            await db.commit()
            logger.info(f"👑 Superuser bootstrapped: {su_email}")
        finally:
            await db.close()
    await scheduler.start()
    log_notification_config()
    logger.info("🏀 GOATcommish ready (demo=%s)", DEMO_MODE)
    yield
    await scheduler.stop()

app = FastAPI(title="GOATcommish — Pickup Basketball", version="3.0.0", lifespan=lifespan,
              docs_url="/docs" if DEMO_MODE else None, redoc_url="/redoc" if DEMO_MODE else None)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS if not DEMO_MODE else ["*"],
                   allow_credentials=True, allow_methods=["GET","POST","PATCH","DELETE","OPTIONS"],
                   allow_headers=["Authorization","Content-Type"], max_age=600)


# ═════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════
def row_to_dict(row):
    return dict(row) if row else {}

def validate_notif_pref(pref: str) -> str:
    if not pref: return "email"
    pref = pref.strip().lower()
    if pref == "none": return "none"
    parts = [p.strip() for p in pref.split(",") if p.strip()]
    valid = [p for p in parts if p in ("email", "sms")]
    return ",".join(sorted(set(valid))) if valid else "email"

def esc(s): return s.replace("'", "''") if s else ""


async def player_out_from_db(db, player_id: int) -> PlayerOut:
    """Build a PlayerOut with group memberships and pending invitations."""
    cursor = await db.execute("SELECT * FROM players WHERE id=?", (player_id,))
    p = await cursor.fetchone()
    if not p: raise HTTPException(404, "Player not found")
    d = dict(p)

    # Get group memberships
    cursor = await db.execute(
        """SELECT gm.group_id, g.name as group_name, gm.role, gm.priority, gm.status
           FROM group_members gm JOIN groups g ON g.id=gm.group_id
           WHERE gm.player_id=? AND gm.status IN ('active','pending')
           ORDER BY g.name""", (player_id,))
    groups = [GroupMembershipOut(group_id=r["group_id"], group_name=r["group_name"],
              role=r["role"], priority=r["priority"], status=r["status"])
              for r in await cursor.fetchall()]

    # Get pending invitations
    cursor = await db.execute(
        """SELECT gi.id, gi.group_id, g.name as group_name, gi.token,
                  p2.name as invited_by_name
           FROM group_invitations gi
           JOIN groups g ON g.id=gi.group_id
           JOIN players p2 ON p2.id=gi.invited_by
           WHERE gi.player_id=? AND gi.status='pending'""", (player_id,))
    invitations = [InvitationOut(id=r["id"], group_id=r["group_id"],
                   group_name=r["group_name"], invited_by_name=r["invited_by_name"],
                   token=r["token"]) for r in await cursor.fetchall()]

    role = await get_player_role(db, player_id)
    return PlayerOut(
        id=d["id"], name=d["name"], email=d["email"], mobile=d.get("mobile",""),
        role=role, priority=d.get("priority","standard"), status=d["status"],
        notif_pref=d["notif_pref"], force_password_change=bool(d.get("force_password_change",0)),
        is_superuser=bool(d.get("is_superuser",0)),
        created_at=str(d["created_at"]), groups=groups, pending_invitations=invitations)


async def require_group_organizer(db, player_id: int, group_id: int):
    """Verify player is an organizer of this specific group."""
    cursor = await db.execute(
        "SELECT 1 FROM group_members WHERE player_id=? AND group_id=? AND role='organizer' AND status='active'",
        (player_id, group_id))
    if not await cursor.fetchone():
        raise HTTPException(403, "You are not an organizer of this group")


async def get_organizer_group_ids(db, player_id: int) -> list[int]:
    cursor = await db.execute(
        "SELECT group_id FROM group_members WHERE player_id=? AND role='organizer' AND status='active'",
        (player_id,))
    return [r["group_id"] for r in await cursor.fetchall()]


async def get_player_group_ids(db, player_id: int) -> list[int]:
    cursor = await db.execute(
        "SELECT group_id FROM group_members WHERE player_id=? AND status='active'",
        (player_id,))
    return [r["group_id"] for r in await cursor.fetchall()]


async def require_superuser(db, player_id: int):
    """Verify player is a superuser."""
    cursor = await db.execute("SELECT is_superuser FROM players WHERE id=?", (player_id,))
    row = await cursor.fetchone()
    if not row or not row["is_superuser"]:
        raise HTTPException(403, "Superuser access required")


# ═════════════════════════════════════════════════════════════════
# DEMO DATA
# ═════════════════════════════════════════════════════════════════
async def seed_demo_data():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) as c FROM players")
        if (await cursor.fetchone())["c"] > 0: return

        # Create friedland group
        await db.execute("INSERT OR IGNORE INTO groups (name) VALUES ('friedland')")
        cursor = await db.execute("SELECT id FROM groups WHERE name='friedland'")
        gid = (await cursor.fetchone())["id"]
        await ensure_group_settings(db, gid)

        pw_hash = hash_password("pass123")
        demo_players = [
            ("Ben Carter","ben@example.com","555-0101","owner","high"),
            ("Mason Levy","mason@example.com","555-0102","owner","high"),
            ("Jordan Smith","jordan@example.com","555-0103","player","high"),
            ("Alex Rivera","alex@example.com","555-0104","player","standard"),
            ("Chris Park","chris@example.com","555-0105","player","standard"),
            ("Taylor Kim","taylor@example.com","555-0106","player","standard"),
            ("Sam Washington","sam@example.com","555-0107","player","standard"),
            ("Morgan Lee","morgan@example.com","555-0108","player","low"),
            ("Jamie Chen","jamie@example.com","555-0109","player","standard"),
            ("Drew Thompson","drew@example.com","555-0110","player","standard"),
        ]
        for name, email, mobile, role, priority in demo_players:
            await db.execute(
                """INSERT INTO players (name,email,mobile,password_hash,role,priority,status,notif_pref)
                   VALUES (?,?,?,?,?,?,'approved','email')""",
                (name, email, mobile, pw_hash, role, priority))
            pid = (await db.execute("SELECT last_insert_rowid()")).fetchone
            cursor2 = await db.execute("SELECT id FROM players WHERE email=?", (email,))
            pid = (await cursor2.fetchone())["id"]
            gm_role = "organizer" if role == "owner" else "player"
            await db.execute(
                "INSERT OR IGNORE INTO group_members (group_id,player_id,role,priority,status) VALUES (?,?,?,?,'active')",
                (gid, pid, gm_role, priority))

        # Pending players (no group yet — they'll need to join)
        for name, email, mobile in [("Casey Williams","casey@example.com","555-0111"),
                                     ("Riley Davis","riley@example.com","555-0112")]:
            await db.execute(
                """INSERT INTO players (name,email,mobile,password_hash,role,priority,status,notif_pref)
                   VALUES (?,?,?,?,'player','standard','approved','email')""",
                (name, email, mobile, pw_hash))

        # Demo locations
        for i, loc in enumerate(["Central Park Courts","YMCA Gym","LA Fitness","Community Center","Outdoor Courts @ 5th"]):
            await db.execute("INSERT OR IGNORE INTO locations (name,sort_order,group_id) VALUES (?,?,?)", (loc, i, gid))

        # Demo games
        now = datetime.now(timezone.utc)
        tomorrow = (now + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
        game2 = (now + timedelta(days=3)).replace(hour=19, minute=0, second=0, microsecond=0)
        await db.execute(
            """INSERT INTO games (group_id,date,location,algorithm,cap,cap_enabled,created_by,
                                  notified_at,phase,selection_done)
               VALUES (?,'%s','Central Park Courts','first_come',12,1,1,?,'active',1)""" % tomorrow.isoformat(),
            (gid, now.isoformat()))
        await db.execute(
            """INSERT INTO games (group_id,date,location,algorithm,cap,cap_enabled,created_by,
                                  notified_at,phase,selection_done)
               VALUES (?,'%s','YMCA Gym','random',10,1,1,?,'signup',0)""" % game2.isoformat(),
            (gid, now.isoformat()))
        for pid in [1,3,4,5,6,7]:
            oa = 1 if pid in [1,3] else 0
            await db.execute("INSERT INTO game_signups (game_id,player_id,status,owner_added) VALUES (1,?,?,?)", (pid,'in',oa))
        for pid in [2,4,5,6,7,9]:
            oa = 1 if pid == 2 else 0
            await db.execute("INSERT INTO game_signups (game_id,player_id,status,owner_added) VALUES (2,?,?,?)", (pid,'pending',oa))

        await db.commit()
        logger.info("Demo data seeded")
    finally:
        await db.close()



# ═════════════════════════════════════════════════════════════════
# AUTH ENDPOINTS
# ═════════════════════════════════════════════════════════════════
@app.post("/api/auth/signup")
async def signup(req: SignupRequest, request: Request):
    """Register a new player and optionally join/create a group."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id FROM players WHERE email=? COLLATE NOCASE", (req.email.strip().lower(),))
        if await cursor.fetchone():
            raise HTTPException(400, "Email already registered")
        name = sanitize_string(req.name)
        email = req.email.strip().lower()
        if not validate_email_format(email):
            raise HTTPException(400, "Invalid email format")
        pw_hash = hash_password(req.password)
        notif_pref = validate_notif_pref(req.notif_pref)
        await db.execute(
            """INSERT INTO players (name,email,mobile,password_hash,role,priority,status,notif_pref)
               VALUES (?,?,?,?,'player','standard','approved',?)""",
            (name, email, sanitize_string(req.mobile), pw_hash, notif_pref))
        await db.commit()
        cursor = await db.execute("SELECT id FROM players WHERE email=?", (email,))
        player = await cursor.fetchone()
        pid = player["id"]

        # Handle group join/create
        group_joined = False
        if req.create_group_name.strip():
            gname = sanitize_string(req.create_group_name.strip())
            cursor = await db.execute("SELECT id FROM groups WHERE name=? COLLATE NOCASE", (gname,))
            if await cursor.fetchone():
                raise HTTPException(400, f"Group '{gname}' already exists")
            await db.execute("INSERT INTO groups (name, created_by) VALUES (?,?)", (gname, pid))
            await db.commit()
            cursor = await db.execute("SELECT id FROM groups WHERE name=? COLLATE NOCASE", (gname,))
            gid = (await cursor.fetchone())["id"]
            await db.execute(
                "INSERT INTO group_members (group_id,player_id,role,priority,status) VALUES (?,?,'organizer','standard','active')",
                (gid, pid))
            await ensure_group_settings(db, gid)
            await db.commit()
            group_joined = True
        elif req.join_group_name.strip():
            gname = req.join_group_name.strip()
            cursor = await db.execute("SELECT id FROM groups WHERE name=? COLLATE NOCASE", (gname,))
            grow = await cursor.fetchone()
            if not grow:
                raise HTTPException(404, f"Group '{gname}' not found")
            await db.execute(
                "INSERT OR IGNORE INTO group_members (group_id,player_id,role,priority,status) VALUES (?,?,'player','standard','active')",
                (grow["id"], pid))
            await db.commit()
            group_joined = True
        elif req.join_organizer_email.strip():
            oemail = req.join_organizer_email.strip().lower()
            cursor = await db.execute(
                """SELECT g.id FROM groups g
                   JOIN group_members gm ON gm.group_id=g.id
                   JOIN players p ON p.id=gm.player_id
                   WHERE p.email=? COLLATE NOCASE AND gm.role='organizer' AND gm.status='active'
                   LIMIT 1""", (oemail,))
            grow = await cursor.fetchone()
            if not grow:
                raise HTTPException(404, "No group found for that organizer email")
            await db.execute(
                "INSERT OR IGNORE INTO group_members (group_id,player_id,role,priority,status) VALUES (?,?,'player','standard','active')",
                (grow["id"], pid))
            await db.commit()
            group_joined = True

        role = await get_player_role(db, pid)
        token, jti = create_access_token(pid, role)
        pout = await player_out_from_db(db, pid)
        return TokenResponse(access_token=token, player=pout)
    finally:
        await db.close()


@app.post("/api/auth/login")
async def login(req: LoginRequest, request: Request):
    ip = get_client_ip(request)
    db = await get_db()
    try:
        if await is_account_locked(db, req.email):
            raise HTTPException(429, "Too many failed attempts — try again later")
        cursor = await db.execute("SELECT * FROM players WHERE email=? COLLATE NOCASE", (req.email.strip().lower(),))
        player = await cursor.fetchone()
        if not player or not verify_password(req.password, player["password_hash"]):
            await record_login_attempt(db, req.email, ip, success=False)
            raise HTTPException(401, "Invalid credentials")
        if player["status"] != "approved":
            raise HTTPException(403, "Account is pending approval")
        await record_login_attempt(db, req.email, ip, success=True)
        if needs_rehash(player["password_hash"]):
            new_hash = hash_password(req.password)
            await db.execute("UPDATE players SET password_hash=? WHERE id=?", (new_hash, player["id"]))
            await db.commit()
        role = await get_player_role(db, player["id"])
        token, jti = create_access_token(player["id"], role)
        pout = await player_out_from_db(db, player["id"])
        return TokenResponse(access_token=token, player=pout)
    finally:
        await db.close()


@app.post("/api/auth/logout")
async def logout(player_id: int = Depends(get_current_player_id)):
    return {"message": "Logged out"}


@app.get("/api/me")
async def get_me(player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        return await player_out_from_db(db, player_id)
    finally:
        await db.close()


@app.patch("/api/me")
async def update_me(req: PlayerUpdate, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        sets, vals = [], []
        if req.name is not None: sets.append("name=?"); vals.append(sanitize_string(req.name))
        if req.email is not None:
            email = req.email.strip().lower()
            if not validate_email_format(email): raise HTTPException(400, "Invalid email")
            cursor = await db.execute("SELECT id FROM players WHERE email=? AND id!=?", (email, player_id))
            if await cursor.fetchone(): raise HTTPException(400, "Email taken")
            sets.append("email=?"); vals.append(email)
        if req.mobile is not None: sets.append("mobile=?"); vals.append(sanitize_string(req.mobile))
        if req.password is not None: sets.append("password_hash=?"); vals.append(hash_password(req.password)); sets.append("force_password_change=0")
        if req.notif_pref is not None: sets.append("notif_pref=?"); vals.append(validate_notif_pref(req.notif_pref))
        if not sets: raise HTTPException(400, "No fields to update")
        sets.append("updated_at=?"); vals.append(datetime.now(timezone.utc).isoformat())
        vals.append(player_id)
        await db.execute(f"UPDATE players SET {','.join(sets)} WHERE id=?", vals)
        await db.commit()
        return await player_out_from_db(db, player_id)
    finally:
        await db.close()


@app.post("/api/auth/change-password")
async def change_password(req: dict, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        new_password = req.get("new_password","")
        if len(new_password) < 4: raise HTTPException(400, "Password must be 4+ characters")
        cursor = await db.execute("SELECT password_hash FROM players WHERE id=?", (player_id,))
        p = await cursor.fetchone()
        current = req.get("current_password","")
        if current and not verify_password(current, p["password_hash"]): raise HTTPException(401, "Wrong current password")
        await db.execute("UPDATE players SET password_hash=?, force_password_change=0 WHERE id=?",
                         (hash_password(new_password), player_id))
        await db.commit()
        return {"message":"Password updated"}
    finally:
        await db.close()


@app.post("/api/auth/reset-password")
async def request_password_reset(email: str = Query(...)):
    """Send a password reset link to the user's email."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id FROM players WHERE email=? COLLATE NOCASE", (email.strip().lower(),))
        player = await cursor.fetchone()
        # Always return success to avoid leaking whether email exists
        if not player:
            return {"message": "If that email is registered, a reset link has been sent."}
        pid = player["id"]
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        await db.execute(
            "INSERT INTO password_reset_tokens (player_id, token, expires_at) VALUES (?,?,?)",
            (pid, token, expires))
        await db.commit()
        base_url = os.environ.get("HOOPS_BASE_URL", "https://www.goatcommish.com")
        reset_url = f"{base_url}?reset={token}"
        from app.notifications import send_email
        await send_email(
            email.strip().lower(),
            "🔑 GOATcommish Password Reset",
            f"Click the link below to reset your password:\n\n{reset_url}\n\nThis link expires in 24 hours. If you didn't request this, you can ignore this email."
        )
        return {"message": "If that email is registered, a reset link has been sent."}
    finally:
        await db.close()


@app.post("/api/auth/reset-password/confirm")
async def confirm_password_reset(token: str = Query(...), new_password: str = Query(...)):
    """Reset password using a token from the email link."""
    if len(new_password) < 4:
        raise HTTPException(400, "Password must be at least 4 characters")
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM password_reset_tokens WHERE token=? AND used=0", (token,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(400, "Invalid or expired reset token")
        if row["expires_at"] < datetime.now(timezone.utc).isoformat():
            raise HTTPException(400, "Reset token has expired")
        await db.execute("UPDATE players SET password_hash=?, force_password_change=0 WHERE id=?",
                         (hash_password(new_password), row["player_id"]))
        await db.execute("UPDATE password_reset_tokens SET used=1 WHERE id=?", (row["id"],))
        await db.commit()
        return {"message": "Password has been reset. You can now log in."}
    finally:
        await db.close()


# ═════════════════════════════════════════════════════════════════
# GROUP ENDPOINTS
# ═════════════════════════════════════════════════════════════════
@app.post("/api/groups")
async def create_group(req: GroupCreateRequest, player_id: int = Depends(get_current_player_id)):
    """Create a new group — caller becomes organizer."""
    db = await get_db()
    try:
        name = sanitize_string(req.name.strip())
        cursor = await db.execute("SELECT id FROM groups WHERE name=? COLLATE NOCASE", (name,))
        if await cursor.fetchone(): raise HTTPException(400, f"Group '{name}' already exists")
        await db.execute("INSERT INTO groups (name,created_by) VALUES (?,?)", (name, player_id))
        await db.commit()
        cursor = await db.execute("SELECT id FROM groups WHERE name=?", (name,))
        gid = (await cursor.fetchone())["id"]
        # Add creator as organizer + active
        await db.execute(
            "INSERT INTO group_members (group_id,player_id,role,priority,status) VALUES (?,?,'organizer','standard','active')",
            (gid, player_id))
        # Update player role to owner if not already
        await db.execute("UPDATE players SET role='owner' WHERE id=?", (player_id,))
        await ensure_group_settings(db, gid)
        await db.commit()
        return {"id": gid, "name": name, "message": f"Group '{name}' created"}
    finally:
        await db.close()


@app.get("/api/groups")
async def list_my_groups(player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT g.id, g.name, gm.role,
                      (SELECT COUNT(*) FROM group_members WHERE group_id=g.id AND status='active') as member_count
               FROM group_members gm JOIN groups g ON g.id=gm.group_id
               WHERE gm.player_id=? AND gm.status='active'
               ORDER BY g.name""", (player_id,))
        return [GroupOut(id=r["id"], name=r["name"], my_role=r["role"], member_count=r["member_count"])
                for r in await cursor.fetchall()]
    finally:
        await db.close()


@app.post("/api/groups/join")
async def join_group(req: GroupJoinRequest, player_id: int = Depends(get_current_player_id)):
    """Join a group by name or organizer email."""
    db = await get_db()
    try:
        gid = None
        if req.group_name:
            cursor = await db.execute("SELECT id FROM groups WHERE name=? COLLATE NOCASE", (req.group_name.strip(),))
            row = await cursor.fetchone()
            if row: gid = row["id"]
        if not gid and req.organizer_email:
            cursor = await db.execute(
                """SELECT gm.group_id FROM group_members gm
                   JOIN players p ON p.id=gm.player_id
                   WHERE p.email=? COLLATE NOCASE AND gm.role='organizer' AND gm.status='active'
                   LIMIT 1""", (req.organizer_email.strip(),))
            row = await cursor.fetchone()
            if row: gid = row["group_id"]
        if not gid:
            raise HTTPException(404, "Group not found. Check the name or organizer email.")
        # Check if already a member
        cursor = await db.execute(
            "SELECT status FROM group_members WHERE group_id=? AND player_id=?", (gid, player_id))
        existing = await cursor.fetchone()
        if existing:
            if existing["status"] == "active": raise HTTPException(400, "Already a member of this group")
            if existing["status"] in ("pending","invited"):
                await db.execute(
                    "UPDATE group_members SET status='active' WHERE group_id=? AND player_id=?", (gid, player_id))
                # Also accept any pending invitations
                await db.execute(
                    "UPDATE group_invitations SET status='accepted' WHERE group_id=? AND player_id=? AND status='pending'",
                    (gid, player_id))
                await db.commit()
                return {"message": "Joined group", "group_id": gid}
            if existing["status"] == "declined":
                await db.execute(
                    "UPDATE group_members SET status='active' WHERE group_id=? AND player_id=?", (gid, player_id))
                await db.commit()
                return {"message": "Re-joined group", "group_id": gid}
        await db.execute(
            "INSERT INTO group_members (group_id,player_id,role,priority,status) VALUES (?,?,'player','standard','active')",
            (gid, player_id))
        await db.commit()
        return {"message": "Joined group", "group_id": gid}
    finally:
        await db.close()


@app.post("/api/groups/{group_id}/leave")
async def leave_group(group_id: int, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        await db.execute(
            "UPDATE group_members SET status='declined' WHERE group_id=? AND player_id=?",
            (group_id, player_id))
        await db.commit()
        return {"message": "Left group"}
    finally:
        await db.close()


@app.get("/api/groups/{group_id}/members")
async def get_group_members(group_id: int, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        await require_group_organizer(db, player_id, group_id)
        cursor = await db.execute(
            """SELECT gm.player_id, p.name, p.email, p.mobile, gm.role, gm.priority, gm.status, p.notif_pref
               FROM group_members gm JOIN players p ON p.id=gm.player_id
               WHERE gm.group_id=? AND gm.status IN ('active','pending','invited')
               ORDER BY CASE gm.role WHEN 'organizer' THEN 0 ELSE 1 END,
                        CASE gm.priority WHEN 'high' THEN 0 WHEN 'standard' THEN 1 ELSE 2 END,
                        p.name""", (group_id,))
        return [GroupMemberOut(player_id=r["player_id"], name=r["name"], email=r["email"],
                mobile=r["mobile"], role=r["role"], priority=r["priority"], status=r["status"])
                for r in await cursor.fetchall()]
    finally:
        await db.close()


@app.patch("/api/groups/{group_id}/members/{target_player_id}")
async def update_group_member(group_id: int, target_player_id: int, req: PlayerAdminUpdate,
                               player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        await require_group_organizer(db, player_id, group_id)
        sets, vals = [], []
        if req.priority: sets.append("priority=?"); vals.append(req.priority)
        if req.role:
            sets.append("role=?"); vals.append(req.role)
            # Also update global role
            if req.role == "organizer":
                await db.execute("UPDATE players SET role='owner' WHERE id=?", (target_player_id,))
        if req.status: sets.append("status=?"); vals.append(req.status)
        if not sets: raise HTTPException(400, "Nothing to update")
        vals.extend([group_id, target_player_id])
        await db.execute(f"UPDATE group_members SET {','.join(sets)} WHERE group_id=? AND player_id=?", vals)
        await db.commit()
        # Recalculate global role
        role = await get_player_role(db, target_player_id)
        await db.execute("UPDATE players SET role=? WHERE id=?", (role, target_player_id))
        await db.commit()
        return {"message": "Member updated"}
    finally:
        await db.close()


@app.delete("/api/groups/{group_id}/members/{target_player_id}")
async def remove_group_member(group_id: int, target_player_id: int,
                               player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        await require_group_organizer(db, player_id, group_id)
        await db.execute("DELETE FROM group_members WHERE group_id=? AND player_id=?", (group_id, target_player_id))
        await db.commit()
        role = await get_player_role(db, target_player_id)
        await db.execute("UPDATE players SET role=? WHERE id=?", (role, target_player_id))
        await db.commit()
        return {"message": "Member removed"}
    finally:
        await db.close()


# ── Invitations ───────────────────────────────────────────────
@app.post("/api/groups/{group_id}/invite")
async def invite_to_group(group_id: int, req: PlayerCreate, player_id: int = Depends(get_current_player_id)):
    """Invite a player to a group. If not registered, create account. If registered, send invitation."""
    db = await get_db()
    try:
        await require_group_organizer(db, player_id, group_id)
        email = req.email.strip().lower()
        name = sanitize_string(req.name)
        cursor = await db.execute("SELECT id FROM players WHERE email=? COLLATE NOCASE", (email,))
        existing = await cursor.fetchone()
        token = secrets.token_urlsafe(32)
        if existing:
            target_pid = existing["id"]
            # Check if already member
            cursor2 = await db.execute(
                "SELECT status FROM group_members WHERE group_id=? AND player_id=?", (group_id, target_pid))
            mem = await cursor2.fetchone()
            if mem and mem["status"] == "active":
                return {"message": f"{name} is already an active member", "player_id": target_pid}
            if not mem:
                await db.execute(
                    "INSERT INTO group_members (group_id,player_id,role,priority,status) VALUES (?,?,'player','standard','invited')",
                    (group_id, target_pid))
            else:
                await db.execute("UPDATE group_members SET status='invited' WHERE group_id=? AND player_id=?",
                                 (group_id, target_pid))
            await db.execute(
                "INSERT INTO group_invitations (group_id,player_id,invited_by,token) VALUES (?,?,?,?)",
                (group_id, target_pid, player_id, token))
            await db.commit()
            # Send invitation notification
            bg_notify(notify_group_invitation, group_id, target_pid, player_id, token)
            return {"message": f"Invitation sent to {name}", "player_id": target_pid}
        else:
            # New player — auto-create account and add to group
            pw = email.strip().lower()
            pw_hash = hash_password(pw)
            mobile = sanitize_string(req.mobile) if req.mobile else ""
            await db.execute(
                """INSERT INTO players (name,email,mobile,password_hash,role,priority,status,notif_pref,force_password_change)
                   VALUES (?,?,?,?,'player','standard','approved','email',1)""",
                (name, email, mobile, pw_hash))
            await db.commit()
            cursor2 = await db.execute("SELECT id FROM players WHERE email=?", (email,))
            target_pid = (await cursor2.fetchone())["id"]
            await db.execute(
                "INSERT INTO group_members (group_id,player_id,role,priority,status) VALUES (?,?,'player','standard','active')",
                (group_id, target_pid))
            await db.commit()
            return {"message": f"{name} added to group (new account created, password is their email)", "player_id": target_pid}
    finally:
        await db.close()


@app.post("/api/invitations/{token}/accept")
async def accept_invitation(token: str, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM group_invitations WHERE token=? AND status='pending'", (token,))
        inv = await cursor.fetchone()
        if not inv: raise HTTPException(404, "Invitation not found or already responded")
        if inv["player_id"] != player_id: raise HTTPException(403, "Not your invitation")
        await db.execute("UPDATE group_invitations SET status='accepted' WHERE id=?", (inv["id"],))
        await db.execute("UPDATE group_members SET status='active' WHERE group_id=? AND player_id=?",
                         (inv["group_id"], player_id))
        await db.commit()
        return {"message": "Invitation accepted"}
    finally:
        await db.close()


@app.post("/api/invitations/{token}/decline")
async def decline_invitation(token: str, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM group_invitations WHERE token=? AND status='pending'", (token,))
        inv = await cursor.fetchone()
        if not inv: raise HTTPException(404, "Invitation not found or already responded")
        if inv["player_id"] != player_id: raise HTTPException(403, "Not your invitation")
        await db.execute("UPDATE group_invitations SET status='declined' WHERE id=?", (inv["id"],))
        await db.execute("UPDATE group_members SET status='declined' WHERE group_id=? AND player_id=?",
                         (inv["group_id"], player_id))
        await db.commit()
        return {"message": "Invitation declined"}
    finally:
        await db.close()


# Public accept/decline (via email link, no auth required)
@app.get("/api/invitations/{token}/accept-public")
async def accept_invitation_public(token: str):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM group_invitations WHERE token=? AND status='pending'", (token,))
        inv = await cursor.fetchone()
        if not inv:
            from fastapi.responses import HTMLResponse
            return HTMLResponse("<html><body><h2>Invitation already responded to or expired.</h2></body></html>")
        await db.execute("UPDATE group_invitations SET status='accepted' WHERE id=?", (inv["id"],))
        await db.execute("UPDATE group_members SET status='active' WHERE group_id=? AND player_id=?",
                         (inv["group_id"], inv["player_id"]))
        await db.commit()
        cursor2 = await db.execute("SELECT name FROM groups WHERE id=?", (inv["group_id"],))
        gname = (await cursor2.fetchone())["name"]
        from fastapi.responses import HTMLResponse
        return HTMLResponse(f"<html><body style='text-align:center;padding:40px;font-family:sans-serif;'>"
                           f"<h2>✅ You've joined <strong>{gname}</strong>!</h2>"
                           f"<p>Open the app to see games.</p></body></html>")
    finally:
        await db.close()


@app.get("/api/invitations/{token}/decline-public")
async def decline_invitation_public(token: str):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM group_invitations WHERE token=? AND status='pending'", (token,))
        inv = await cursor.fetchone()
        if not inv:
            from fastapi.responses import HTMLResponse
            return HTMLResponse("<html><body><h2>Invitation already responded to or expired.</h2></body></html>")
        await db.execute("UPDATE group_invitations SET status='declined' WHERE id=?", (inv["id"],))
        await db.execute("UPDATE group_members SET status='declined' WHERE group_id=? AND player_id=?",
                         (inv["group_id"], inv["player_id"]))
        await db.commit()
        from fastapi.responses import HTMLResponse
        return HTMLResponse(f"<html><body style='text-align:center;padding:40px;font-family:sans-serif;'>"
                           f"<h2>Invitation declined.</h2></body></html>")
    finally:
        await db.close()



# ═════════════════════════════════════════════════════════════════
# PLAYER MANAGEMENT (group-scoped)
# ═════════════════════════════════════════════════════════════════
@app.get("/api/groups/{group_id}/players")
async def get_group_players(group_id: int, player_id: int = Depends(get_current_player_id)):
    """Get players for a group. Organizers see full details; players see public info."""
    db = await get_db()
    try:
        # Verify caller is a member
        cursor = await db.execute(
            "SELECT role FROM group_members WHERE group_id=? AND player_id=? AND status='active'",
            (group_id, player_id))
        mem = await cursor.fetchone()
        if not mem: raise HTTPException(403, "Not a member of this group")

        cursor = await db.execute(
            """SELECT gm.player_id, p.name, p.email, p.mobile, gm.role, gm.priority, gm.status, p.notif_pref
               FROM group_members gm JOIN players p ON p.id=gm.player_id
               WHERE gm.group_id=? AND gm.status='active'
               ORDER BY p.name""", (group_id,))
        return [GroupMemberOut(player_id=r["player_id"], name=r["name"], email=r["email"],
                mobile=r["mobile"], role=r["role"], priority=r["priority"], status=r["status"])
                for r in await cursor.fetchall()]
    finally:
        await db.close()


@app.post("/api/groups/{group_id}/players/import")
async def import_players_csv(group_id: int, file: UploadFile = File(...),
                              player_id: int = Depends(get_current_player_id)):
    """Import players from CSV into a group."""
    db = await get_db()
    try:
        await require_group_organizer(db, player_id, group_id)
        content = (await file.read()).decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        added, skipped, invited = 0, 0, 0
        for row in reader:
            name = sanitize_string(row.get("name","").strip())
            email = row.get("email","").strip().lower()
            mobile = sanitize_string(row.get("mobile","").strip())
            if not name or not email: skipped += 1; continue
            cursor = await db.execute("SELECT id FROM players WHERE email=? COLLATE NOCASE", (email,))
            existing = await cursor.fetchone()
            if existing:
                pid = existing["id"]
                cursor2 = await db.execute(
                    "SELECT status FROM group_members WHERE group_id=? AND player_id=?", (group_id, pid))
                mem = await cursor2.fetchone()
                if mem and mem["status"] == "active": skipped += 1; continue
                if mem:
                    await db.execute("UPDATE group_members SET status='invited' WHERE group_id=? AND player_id=?", (group_id, pid))
                else:
                    await db.execute(
                        "INSERT INTO group_members (group_id,player_id,role,priority,status) VALUES (?,?,'player','standard','invited')",
                        (group_id, pid))
                token = secrets.token_urlsafe(32)
                await db.execute(
                    "INSERT INTO group_invitations (group_id,player_id,invited_by,token) VALUES (?,?,?,?)",
                    (group_id, pid, player_id, token))
                invited += 1
            else:
                pw = email.strip().lower()
                pw_hash = hash_password(pw)
                await db.execute(
                    """INSERT INTO players (name,email,mobile,password_hash,role,priority,status,notif_pref,force_password_change)
                       VALUES (?,?,?,?,'player','standard','approved','email',1)""",
                    (name, email, mobile, pw_hash))
                await db.commit()
                cursor2 = await db.execute("SELECT id FROM players WHERE email=?", (email,))
                pid = (await cursor2.fetchone())["id"]
                await db.execute(
                    "INSERT INTO group_members (group_id,player_id,role,priority,status) VALUES (?,?,'player','standard','active')",
                    (group_id, pid))
                added += 1
        await db.commit()
        return {"added": added, "invited": invited, "skipped": skipped}
    finally:
        await db.close()


# ═════════════════════════════════════════════════════════════════
# SETTINGS (group-scoped)
# ═════════════════════════════════════════════════════════════════
@app.get("/api/groups/{group_id}/settings")
async def get_group_settings(group_id: int, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        await require_group_organizer(db, player_id, group_id)
        await ensure_group_settings(db, group_id)
        s = {}
        cursor = await db.execute("SELECT key,value FROM group_settings WHERE group_id=?", (group_id,))
        for r in await cursor.fetchall(): s[r["key"]] = r["value"]
        cursor = await db.execute(
            "SELECT * FROM locations WHERE group_id=? ORDER BY sort_order, id", (group_id,))
        locs = [LocationOut(id=r["id"], name=r["name"], address=r["address"] or "", sort_order=r["sort_order"] or 0)
                for r in await cursor.fetchall()]
        return SettingsOut(
            default_cap=int(s.get("default_cap","12")),
            cap_enabled=s.get("cap_enabled","1")=="1",
            default_algorithm=s.get("default_algorithm","first_come"),
            default_location=s.get("default_location",""),
            high_priority_delay_minutes=int(s.get("high_priority_delay_minutes","60")),
            alternative_delay_minutes=int(s.get("alternative_delay_minutes","1440")),
            random_wait_period_minutes=int(s.get("random_wait_period_minutes","60")),
            notify_owner_new_signup=s.get("notify_owner_new_signup","1")=="1",
            notify_owner_player_drop=s.get("notify_owner_player_drop","1")=="1",
            locations=locs)
    finally:
        await db.close()


@app.patch("/api/groups/{group_id}/settings")
async def update_group_settings(group_id: int, req: SettingsUpdate,
                                 player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        await require_group_organizer(db, player_id, group_id)
        mapping = {
            "default_cap": str(req.default_cap) if req.default_cap is not None else None,
            "cap_enabled": ("1" if req.cap_enabled else "0") if req.cap_enabled is not None else None,
            "default_algorithm": req.default_algorithm,
            "default_location": req.default_location,
            "high_priority_delay_minutes": str(req.high_priority_delay_minutes) if req.high_priority_delay_minutes is not None else None,
            "alternative_delay_minutes": str(req.alternative_delay_minutes) if req.alternative_delay_minutes is not None else None,
            "random_wait_period_minutes": str(req.random_wait_period_minutes) if req.random_wait_period_minutes is not None else None,
            "notify_owner_new_signup": ("1" if req.notify_owner_new_signup else "0") if req.notify_owner_new_signup is not None else None,
            "notify_owner_player_drop": ("1" if req.notify_owner_player_drop else "0") if req.notify_owner_player_drop is not None else None,
        }
        for key, val in mapping.items():
            if val is not None: await set_setting(db, key, val, group_id)
        return await get_group_settings(group_id, player_id)
    finally:
        await db.close()


# ── Locations (group-scoped) ──────────────────────────────────
@app.get("/api/groups/{group_id}/locations")
async def get_group_locations(group_id: int, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM locations WHERE group_id=? ORDER BY sort_order, id", (group_id,))
        return [LocationOut(id=r["id"], name=r["name"], address=r["address"] or "", sort_order=r["sort_order"] or 0)
                for r in await cursor.fetchall()]
    finally:
        await db.close()


@app.post("/api/groups/{group_id}/locations")
async def add_group_location(group_id: int, req: LocationCreate,
                              player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        await require_group_organizer(db, player_id, group_id)
        cursor = await db.execute("SELECT COALESCE(MAX(sort_order),0)+1 as n FROM locations WHERE group_id=?", (group_id,))
        next_sort = (await cursor.fetchone())["n"]
        await db.execute("INSERT INTO locations (name,address,sort_order,group_id) VALUES (?,?,?,?)",
                         (sanitize_string(req.name), sanitize_string(req.address), next_sort, group_id))
        await db.commit()
        return await get_group_locations(group_id, player_id)
    finally:
        await db.close()


@app.patch("/api/groups/{group_id}/locations/{loc_id}")
async def update_group_location(group_id: int, loc_id: int, req: LocationUpdate,
                                 player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        await require_group_organizer(db, player_id, group_id)
        sets, vals = [], []
        if req.name is not None: sets.append("name=?"); vals.append(sanitize_string(req.name))
        if req.address is not None: sets.append("address=?"); vals.append(sanitize_string(req.address))
        if sets:
            vals.extend([loc_id, group_id])
            await db.execute(f"UPDATE locations SET {','.join(sets)} WHERE id=? AND group_id=?", vals)
            await db.commit()
        return await get_group_locations(group_id, player_id)
    finally:
        await db.close()


@app.delete("/api/groups/{group_id}/locations/{loc_id}")
async def delete_group_location(group_id: int, loc_id: int,
                                 player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        await require_group_organizer(db, player_id, group_id)
        await db.execute("DELETE FROM locations WHERE id=? AND group_id=?", (loc_id, group_id))
        await db.commit()
        return await get_group_locations(group_id, player_id)
    finally:
        await db.close()


@app.post("/api/groups/{group_id}/locations/reorder")
async def reorder_group_locations(group_id: int, req: LocationReorder,
                                   player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        await require_group_organizer(db, player_id, group_id)
        for i, lid in enumerate(req.location_ids):
            await db.execute("UPDATE locations SET sort_order=? WHERE id=? AND group_id=?", (i, lid, group_id))
        await db.commit()
        return await get_group_locations(group_id, player_id)
    finally:
        await db.close()



# ═════════════════════════════════════════════════════════════════
# GAME HELPER
# ═════════════════════════════════════════════════════════════════
async def game_to_out(db, g) -> GameOut:
    gd = dict(g)
    gid = gd["id"]
    # Group name
    cursor = await db.execute("SELECT name FROM groups WHERE id=?", (gd["group_id"],))
    grow = await cursor.fetchone()
    group_name = grow["name"] if grow else ""
    # Signups
    cursor = await db.execute(
        """SELECT gs.id,gs.player_id,p.name as player_name,gs.signed_up_at,gs.status,gs.owner_added
           FROM game_signups gs JOIN players p ON p.id=gs.player_id
           WHERE gs.game_id=? ORDER BY gs.signed_up_at""", (gid,))
    signups = [SignupOut(id=r["id"], player_id=r["player_id"], player_name=r["player_name"],
               signed_up_at=str(r["signed_up_at"]), status=r["status"], owner_added=bool(r["owner_added"]))
               for r in await cursor.fetchall()]
    # Scheduler info
    auto_sel, ns_at, nl_at, ns_st, nl_st = None, None, None, None, None
    cursor = await db.execute("SELECT * FROM scheduler_jobs WHERE game_id=?", (gid,))
    for j in await cursor.fetchall():
        jd = dict(j)
        if jd["job_type"] == "run_selection": auto_sel = jd["scheduled_at"]
        elif jd["job_type"] == "notify_standard": ns_at = jd["scheduled_at"]; ns_st = jd["status"]
        elif jd["job_type"] == "notify_low": nl_at = jd["scheduled_at"]; nl_st = jd["status"]
    return GameOut(
        id=gid, group_id=gd["group_id"], group_name=group_name,
        date=gd["date"], location=gd["location"], algorithm=gd["algorithm"],
        cap=gd["cap"], cap_enabled=bool(gd["cap_enabled"]),
        created_by=gd["created_by"], created_at=str(gd["created_at"]),
        notified_at=gd.get("notified_at"), phase=gd["phase"],
        selection_done=bool(gd["selection_done"]), closed=bool(gd["closed"]),
        auto_selection_at=auto_sel, notify_standard_at=ns_at, notify_low_at=nl_at,
        notify_standard_status=ns_st, notify_low_status=nl_st,
        batch_id=gd.get("batch_id"), random_high_auto=bool(gd.get("random_high_auto",1)),
        signups=signups)


# ═════════════════════════════════════════════════════════════════
# GAME ENDPOINTS
# ═════════════════════════════════════════════════════════════════
@app.get("/api/games")
async def list_games(view: str = Query("player"), player_id: int = Depends(get_current_player_id)):
    """List games. view=organizer shows games I organized. view=player shows games from my groups."""
    db = await get_db()
    try:
        if view == "organizer":
            org_gids = await get_organizer_group_ids(db, player_id)
            if not org_gids: return []
            placeholders = ",".join("?" * len(org_gids))
            cursor = await db.execute(
                f"""SELECT * FROM games WHERE group_id IN ({placeholders})
                    ORDER BY date DESC""", org_gids)
        else:
            my_gids = await get_player_group_ids(db, player_id)
            if not my_gids: return []
            placeholders = ",".join("?" * len(my_gids))
            cursor = await db.execute(
                f"""SELECT * FROM games WHERE group_id IN ({placeholders})
                    AND closed=0 ORDER BY date ASC""", my_gids)
        games = await cursor.fetchall()
        return [await game_to_out(db, g) for g in games]
    finally:
        await db.close()


@app.post("/api/games")
async def create_game(req: GameCreate, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        # Determine group_id
        gid = req.group_id
        if not gid:
            org_gids = await get_organizer_group_ids(db, player_id)
            if not org_gids: raise HTTPException(403, "You're not an organizer of any group")
            gid = org_gids[0]
        await require_group_organizer(db, player_id, gid)
        await db.execute(
            """INSERT INTO games (group_id,date,location,algorithm,cap,cap_enabled,created_by,
                                  notify_future_at, phase, selection_done, random_high_auto)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (gid, req.date, sanitize_string(req.location), req.algorithm, req.cap,
             1 if req.cap_enabled else 0, player_id,
             req.notify_future_at, "created", 0, 1 if req.random_high_auto else 0))
        await db.commit()
        cursor = await db.execute("SELECT last_insert_rowid()")
        game_id = (await cursor.fetchone())[0]
        # Owner-added players
        for pid in req.owner_added_player_ids:
            await db.execute(
                "INSERT OR IGNORE INTO game_signups (game_id,player_id,status,owner_added) VALUES (?,?,'pending',1)",
                (game_id, pid))
        await db.commit()
        asyncio.create_task(schedule_game_notifications(game_id, req.notify_future_at))
        cursor = await db.execute("SELECT * FROM games WHERE id=?", (game_id,))
        return await game_to_out(db, await cursor.fetchone())
    finally:
        await db.close()


@app.post("/api/games/batch")
async def create_games_batch(req: BatchGameCreate, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        batch_id = str(uuid.uuid4())[:8]
        created_games = []
        for game_req in req.games:
            gid = game_req.group_id
            if not gid:
                org_gids = await get_organizer_group_ids(db, player_id)
                if not org_gids: raise HTTPException(403, "Not an organizer")
                gid = org_gids[0]
            await require_group_organizer(db, player_id, gid)
            await db.execute(
                """INSERT INTO games (group_id,date,location,algorithm,cap,cap_enabled,created_by,
                                      phase,selection_done,batch_id,random_high_auto,notify_future_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (gid, game_req.date, sanitize_string(game_req.location), game_req.algorithm,
                 game_req.cap, 1 if game_req.cap_enabled else 0, player_id,
                 "created", 0, batch_id, 1 if game_req.random_high_auto else 0,
                 game_req.notify_future_at))
            await db.commit()
            cursor = await db.execute("SELECT last_insert_rowid()")
            gm_id = (await cursor.fetchone())[0]
            for pid in game_req.owner_added_player_ids:
                await db.execute(
                    "INSERT OR IGNORE INTO game_signups (game_id,player_id,status,owner_added) VALUES (?,?,'pending',1)",
                    (gm_id, pid))
            await db.commit()
            created_games.append(gm_id)
        # Send batch notification
        if created_games:
            first_game_req = req.games[0]
            asyncio.create_task(_schedule_batch(created_games, first_game_req.notify_future_at))
        result = []
        for gm_id in created_games:
            cursor = await db.execute("SELECT * FROM games WHERE id=?", (gm_id,))
            result.append(await game_to_out(db, await cursor.fetchone()))
        return result
    finally:
        await db.close()


async def _schedule_batch(game_ids, notify_at):
    for gm_id in game_ids:
        await schedule_game_notifications(gm_id, notify_at)


@app.get("/api/games/{game_id}")
async def get_game(game_id: int, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM games WHERE id=?", (game_id,))
        g = await cursor.fetchone()
        if not g: raise HTTPException(404, "Game not found")
        my_gids = await get_player_group_ids(db, player_id)
        if g["group_id"] not in my_gids: raise HTTPException(403, "Not a member of this group")
        return await game_to_out(db, g)
    finally:
        await db.close()


@app.patch("/api/games/{game_id}")
async def update_game(game_id: int, req: GameUpdate, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM games WHERE id=?", (game_id,))
        g = await cursor.fetchone()
        if not g: raise HTTPException(404, "Game not found")
        await require_group_organizer(db, player_id, g["group_id"])
        old_date, old_loc = g["date"], g["location"]
        sets, vals = [], []
        if req.date: sets.append("date=?"); vals.append(req.date)
        if req.location: sets.append("location=?"); vals.append(sanitize_string(req.location))
        if not sets: raise HTTPException(400, "Nothing to update")
        vals.append(game_id)
        await db.execute(f"UPDATE games SET {','.join(sets)} WHERE id=?", vals)
        await db.commit()
        new_date = req.date or old_date
        new_loc = req.location or old_loc
        if new_date != old_date or new_loc != old_loc:
            bg_notify(notify_game_edited, game_id, old_date, old_loc, new_date, new_loc)
        cursor = await db.execute("SELECT * FROM games WHERE id=?", (game_id,))
        return await game_to_out(db, await cursor.fetchone())
    finally:
        await db.close()


@app.post("/api/games/{game_id}/close")
async def close_game(game_id: int, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM games WHERE id=?", (game_id,))
        g = await cursor.fetchone()
        if not g: raise HTTPException(404)
        await require_group_organizer(db, player_id, g["group_id"])
        await db.execute("UPDATE games SET closed=1, phase='closed' WHERE id=?", (game_id,))
        await db.execute("UPDATE scheduler_jobs SET status='completed' WHERE game_id=? AND status='pending'", (game_id,))
        await db.commit()
        return {"message": "Game closed"}
    finally:
        await db.close()


@app.post("/api/games/{game_id}/cancel")
async def cancel_game(game_id: int, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM games WHERE id=?", (game_id,))
        g = await cursor.fetchone()
        if not g: raise HTTPException(404)
        await require_group_organizer(db, player_id, g["group_id"])
        await db.execute("UPDATE games SET closed=1, phase='cancelled' WHERE id=?", (game_id,))
        await db.execute("UPDATE scheduler_jobs SET status='completed' WHERE game_id=? AND status='pending'", (game_id,))
        await db.commit()
        return {"message": "Game cancelled"}
    finally:
        await db.close()


@app.post("/api/games/{game_id}/run-selection")
async def run_selection(game_id: int, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM games WHERE id=?", (game_id,))
        g = await cursor.fetchone()
        if not g: raise HTTPException(404)
        await require_group_organizer(db, player_id, g["group_id"])
        if g["algorithm"] != "random": raise HTTPException(400, "Not a random game")
        result = await run_random_selection(db, game_id)
        return result
    finally:
        await db.close()


# ── Signups ───────────────────────────────────────────────────
@app.post("/api/games/{game_id}/signup")
async def signup_for_game(game_id: int, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM games WHERE id=?", (game_id,))
        g = await cursor.fetchone()
        if not g: raise HTTPException(404)
        # Verify player is in this group
        my_gids = await get_player_group_ids(db, player_id)
        if g["group_id"] not in my_gids: raise HTTPException(403, "Not in this group")
        if g["closed"]: raise HTTPException(400, "Game is closed")
        cursor = await db.execute(
            "SELECT id FROM game_signups WHERE game_id=? AND player_id=?", (game_id, player_id))
        if await cursor.fetchone(): raise HTTPException(400, "Already signed up")
        if g["algorithm"] == "first_come" and g["selection_done"]:
            cap = g["cap"] if g["cap_enabled"] else 999999
            cursor2 = await db.execute(
                "SELECT COUNT(*) as c FROM game_signups WHERE game_id=? AND status='in'", (game_id,))
            in_count = (await cursor2.fetchone())["c"]
            signup_status = "in" if in_count < cap else "waitlist"
        else:
            signup_status = "pending"
        await db.execute(
            "INSERT INTO game_signups (game_id,player_id,status) VALUES (?,?,?)",
            (game_id, player_id, signup_status))
        await db.commit()
        # Notify organizers
        notify_setting = await get_setting(db, "notify_owner_new_signup", g["group_id"])
        if notify_setting != "0":
            bg_notify(notify_owners_new_signup, game_id, player_id)
        cursor = await db.execute("SELECT * FROM games WHERE id=?", (game_id,))
        return await game_to_out(db, await cursor.fetchone())
    finally:
        await db.close()


@app.delete("/api/games/{game_id}/signup")
async def drop_from_game(game_id: int, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM game_signups WHERE game_id=? AND player_id=?", (game_id, player_id))
        signup = await cursor.fetchone()
        if not signup: raise HTTPException(404, "Not signed up")
        was_in = signup["status"] == "in"
        await db.execute("DELETE FROM game_signups WHERE game_id=? AND player_id=?", (game_id, player_id))
        await db.commit()
        # Promote from waitlist if FCFS, notify organizers of drop
        if was_in:
            cursor2 = await db.execute("SELECT * FROM games WHERE id=?", (game_id,))
            g = await cursor2.fetchone()
            if g and g["algorithm"] == "first_come" and g["selection_done"]:
                cursor3 = await db.execute(
                    """SELECT id, player_id FROM game_signups
                       WHERE game_id=? AND status='waitlist'
                       ORDER BY signed_up_at LIMIT 1""", (game_id,))
                next_up = await cursor3.fetchone()
                if next_up:
                    await db.execute("UPDATE game_signups SET status='in' WHERE id=?", (next_up["id"],))
                    await db.commit()
                    bg_notify(notify_waitlist_promotion, game_id, next_up["player_id"])
            if g:
                drop_setting = await get_setting(db, "notify_owner_player_drop", g["group_id"])
                if drop_setting != "0":
                    bg_notify(notify_owner_player_drop, game_id, player_id)
        cursor = await db.execute("SELECT * FROM games WHERE id=?", (game_id,))
        return await game_to_out(db, await cursor.fetchone())
    finally:
        await db.close()


@app.post("/api/games/{game_id}/add-player/{target_id}")
async def add_player_to_game(game_id: int, target_id: int,
                              player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM games WHERE id=?", (game_id,))
        g = await cursor.fetchone()
        if not g: raise HTTPException(404)
        await require_group_organizer(db, player_id, g["group_id"])
        await db.execute(
            "INSERT OR IGNORE INTO game_signups (game_id,player_id,status,owner_added) VALUES (?,?,'in',1)",
            (game_id, target_id))
        await db.commit()
        cursor = await db.execute("SELECT * FROM games WHERE id=?", (game_id,))
        return await game_to_out(db, await cursor.fetchone())
    finally:
        await db.close()


@app.delete("/api/games/{game_id}/remove-player/{target_id}")
async def remove_player_from_game(game_id: int, target_id: int,
                                   player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM games WHERE id=?", (game_id,))
        g = await cursor.fetchone()
        if not g: raise HTTPException(404)
        await require_group_organizer(db, player_id, g["group_id"])
        await db.execute("DELETE FROM game_signups WHERE game_id=? AND player_id=?", (game_id, target_id))
        await db.commit()
        cursor = await db.execute("SELECT * FROM games WHERE id=?", (game_id,))
        return await game_to_out(db, await cursor.fetchone())
    finally:
        await db.close()


# ═════════════════════════════════════════════════════════════════
# BACKWARD-COMPAT LEGACY ENDPOINTS (deprecated, redirect to group)
# ═════════════════════════════════════════════════════════════════
@app.get("/api/settings")
async def get_settings_legacy(player_id: int = Depends(get_current_player_id)):
    """Legacy: returns settings for user's first organizer group."""
    db = await get_db()
    try:
        org_gids = await get_organizer_group_ids(db, player_id)
        if not org_gids: raise HTTPException(403, "Not an organizer")
        return await get_group_settings(org_gids[0], player_id)
    finally:
        await db.close()

@app.patch("/api/settings")
async def update_settings_legacy(req: SettingsUpdate, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        org_gids = await get_organizer_group_ids(db, player_id)
        if not org_gids: raise HTTPException(403, "Not an organizer")
        return await update_group_settings(org_gids[0], req, player_id)
    finally:
        await db.close()

@app.get("/api/settings/locations")
async def get_locations_legacy(player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        org_gids = await get_organizer_group_ids(db, player_id)
        if not org_gids: raise HTTPException(403, "Not an organizer")
        return await get_group_locations(org_gids[0], player_id)
    finally:
        await db.close()

@app.get("/api/players")
async def get_players_legacy(player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        org_gids = await get_organizer_group_ids(db, player_id)
        if not org_gids: raise HTTPException(403, "Not an organizer")
        return await get_group_players(org_gids[0], player_id)
    finally:
        await db.close()


# ═════════════════════════════════════════════════════════════════
# ADMIN / SUPERUSER
# ═════════════════════════════════════════════════════════════════

@app.get("/api/admin/overview")
async def admin_overview(player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        await require_superuser(db, player_id)
        stats = {}
        for key, query in [
            ("total_players", "SELECT COUNT(*) as c FROM players"),
            ("total_groups", "SELECT COUNT(*) as c FROM groups"),
            ("total_games", "SELECT COUNT(*) as c FROM games"),
            ("pending_games", "SELECT COUNT(*) as c FROM games WHERE closed=0"),
            ("total_signups", "SELECT COUNT(*) as c FROM game_signups"),
            ("total_group_members", "SELECT COUNT(*) as c FROM group_members WHERE status='active'"),
        ]:
            cursor = await db.execute(query)
            stats[key] = (await cursor.fetchone())["c"]
        return stats
    finally:
        await db.close()


@app.get("/api/admin/players")
async def admin_list_players(player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        await require_superuser(db, player_id)
        cursor = await db.execute("SELECT * FROM players ORDER BY name")
        players = []
        for p in await cursor.fetchall():
            d = dict(p)
            # Get group memberships
            c2 = await db.execute(
                """SELECT gm.group_id, g.name as group_name, gm.role, gm.priority, gm.status
                   FROM group_members gm JOIN groups g ON g.id=gm.group_id
                   WHERE gm.player_id=?""", (d["id"],))
            d["groups"] = [dict(r) for r in await c2.fetchall()]
            d["is_superuser"] = bool(d.get("is_superuser", 0))
            d.pop("password_hash", None)
            players.append(d)
        return players
    finally:
        await db.close()


@app.patch("/api/admin/players/{target_id}")
async def admin_update_player(target_id: int, req: dict, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        await require_superuser(db, player_id)
        sets, vals = [], []
        for field in ["name", "email", "mobile", "notif_pref", "status"]:
            if field in req:
                sets.append(f"{field}=?"); vals.append(req[field])
        if "is_superuser" in req:
            sets.append("is_superuser=?"); vals.append(1 if req["is_superuser"] else 0)
        if "password" in req and req["password"]:
            sets.append("password_hash=?"); vals.append(hash_password(req["password"]))
            sets.append("force_password_change=1")
        if not sets:
            raise HTTPException(400, "No fields to update")
        vals.append(target_id)
        await db.execute(f"UPDATE players SET {','.join(sets)} WHERE id=?", vals)
        await db.commit()
        return {"message": "Player updated"}
    finally:
        await db.close()


@app.delete("/api/admin/players/{target_id}")
async def admin_delete_player(target_id: int, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        await require_superuser(db, player_id)
        if target_id == player_id:
            raise HTTPException(400, "Cannot delete yourself")
        await db.execute("DELETE FROM game_signups WHERE player_id=?", (target_id,))
        await db.execute("DELETE FROM group_members WHERE player_id=?", (target_id,))
        await db.execute("DELETE FROM group_invitations WHERE player_id=?", (target_id,))
        await db.execute("DELETE FROM game_notifications WHERE player_id=?", (target_id,))
        await db.execute("DELETE FROM players WHERE id=?", (target_id,))
        await db.commit()
        return {"message": "Player deleted"}
    finally:
        await db.close()


@app.get("/api/admin/games")
async def admin_list_games(player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        await require_superuser(db, player_id)
        cursor = await db.execute(
            """SELECT g.*, grp.name as group_name, p.name as creator_name
               FROM games g
               LEFT JOIN groups grp ON grp.id=g.group_id
               LEFT JOIN players p ON p.id=g.created_by
               ORDER BY g.date DESC""")
        games = []
        for row in await cursor.fetchall():
            d = dict(row)
            d["group_name"] = d.get("group_name", "")
            d["creator_name"] = d.get("creator_name", "Unknown")
            # Get signup count
            c2 = await db.execute(
                "SELECT COUNT(*) as c FROM game_signups WHERE game_id=? AND status='confirmed'",
                (d["id"],))
            d["signup_count"] = (await c2.fetchone())["c"]
            games.append(d)
        return games
    finally:
        await db.close()


@app.patch("/api/admin/games/{game_id}")
async def admin_update_game(game_id: int, req: dict, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        await require_superuser(db, player_id)
        sets, vals = [], []
        for field in ["date", "location", "algorithm", "cap", "cap_enabled", "closed", "phase"]:
            if field in req:
                sets.append(f"{field}=?"); vals.append(req[field])
        if not sets:
            raise HTTPException(400, "No fields to update")
        vals.append(game_id)
        await db.execute(f"UPDATE games SET {','.join(sets)} WHERE id=?", vals)
        await db.commit()
        return {"message": "Game updated"}
    finally:
        await db.close()


@app.delete("/api/admin/games/{game_id}")
async def admin_delete_game(game_id: int, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        await require_superuser(db, player_id)
        await db.execute("DELETE FROM game_signups WHERE game_id=?", (game_id,))
        await db.execute("DELETE FROM game_notifications WHERE game_id=?", (game_id,))
        await db.execute("DELETE FROM scheduler_jobs WHERE game_id=?", (game_id,))
        await db.execute("DELETE FROM games WHERE id=?", (game_id,))
        await db.commit()
        return {"message": "Game deleted"}
    finally:
        await db.close()


@app.get("/api/admin/groups")
async def admin_list_groups(player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        await require_superuser(db, player_id)
        cursor = await db.execute("SELECT * FROM groups ORDER BY name")
        groups = []
        for g in await cursor.fetchall():
            d = dict(g)
            c2 = await db.execute(
                "SELECT COUNT(*) as c FROM group_members WHERE group_id=? AND status='active'",
                (d["id"],))
            d["member_count"] = (await c2.fetchone())["c"]
            c3 = await db.execute(
                "SELECT COUNT(*) as c FROM games WHERE group_id=? AND closed=0",
                (d["id"],))
            d["game_count"] = (await c3.fetchone())["c"]
            groups.append(d)
        return groups
    finally:
        await db.close()


@app.patch("/api/admin/groups/{group_id}")
async def admin_update_group(group_id: int, req: dict, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        await require_superuser(db, player_id)
        if "name" in req:
            await db.execute("UPDATE groups SET name=? WHERE id=?", (req["name"], group_id))
            await db.commit()
        return {"message": "Group updated"}
    finally:
        await db.close()


@app.delete("/api/admin/groups/{group_id}")
async def admin_delete_group(group_id: int, player_id: int = Depends(get_current_player_id)):
    db = await get_db()
    try:
        await require_superuser(db, player_id)
        await db.execute("DELETE FROM game_signups WHERE game_id IN (SELECT id FROM games WHERE group_id=?)", (group_id,))
        await db.execute("DELETE FROM game_notifications WHERE game_id IN (SELECT id FROM games WHERE group_id=?)", (group_id,))
        await db.execute("DELETE FROM scheduler_jobs WHERE game_id IN (SELECT id FROM games WHERE group_id=?)", (group_id,))
        await db.execute("DELETE FROM games WHERE group_id=?", (group_id,))
        await db.execute("DELETE FROM group_members WHERE group_id=?", (group_id,))
        await db.execute("DELETE FROM group_invitations WHERE group_id=?", (group_id,))
        await db.execute("DELETE FROM group_settings WHERE group_id=?", (group_id,))
        await db.execute("DELETE FROM locations WHERE group_id=?", (group_id,))
        await db.execute("DELETE FROM groups WHERE id=?", (group_id,))
        await db.commit()
        return {"message": "Group and all associated data deleted"}
    finally:
        await db.close()


@app.post("/api/admin/make-superuser")
async def admin_make_superuser(req: dict, player_id: int = Depends(get_current_player_id)):
    """Promote/demote superuser status. Only existing superusers can do this."""
    db = await get_db()
    try:
        await require_superuser(db, player_id)
        target_id = req.get("player_id")
        is_super = 1 if req.get("is_superuser", False) else 0
        if target_id == player_id and not is_super:
            raise HTTPException(400, "Cannot remove your own superuser status")
        await db.execute("UPDATE players SET is_superuser=? WHERE id=?", (is_super, target_id))
        await db.commit()
        return {"message": "Superuser status updated"}
    finally:
        await db.close()


# ═════════════════════════════════════════════════════════════════
# STATIC FILES
# ═════════════════════════════════════════════════════════════════
static_dir = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    if full_path and (static_dir / full_path).exists():
        return FileResponse(str(static_dir / full_path))
    return FileResponse(str(static_dir / "index.html"))
