"""
Database setup for GOATcommish — multi-group pickup basketball.
"""
import aiosqlite, os, logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("hoops.db")
DB_PATH = os.environ.get("HOOPS_DB_PATH", "hoops.db")

SCHEMA = """
-- ── Global player accounts ──────────────────────────────────
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL COLLATE NOCASE,
    mobile TEXT DEFAULT '',
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'player' CHECK(role IN ('owner','player')),
    priority TEXT NOT NULL DEFAULT 'standard',
    status TEXT NOT NULL DEFAULT 'approved' CHECK(status IN ('pending','approved','denied')),
    notif_pref TEXT NOT NULL DEFAULT 'email',
    force_password_change INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── Groups ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL COLLATE NOCASE,
    created_by INTEGER REFERENCES players(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS group_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'player' CHECK(role IN ('organizer','player')),
    priority TEXT NOT NULL DEFAULT 'standard' CHECK(priority IN ('high','standard','low')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('active','pending','invited','declined')),
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(group_id, player_id)
);

CREATE TABLE IF NOT EXISTS group_invitations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    invited_by INTEGER NOT NULL REFERENCES players(id),
    token TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','declined')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── Per-group settings ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS group_settings (
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    UNIQUE(key, group_id)
);

-- ── Per-group locations ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    address TEXT DEFAULT '',
    sort_order INTEGER DEFAULT 0,
    group_id INTEGER NOT NULL DEFAULT 0
);

-- ── Games (per-group) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL DEFAULT 0,
    date TEXT NOT NULL,
    location TEXT NOT NULL,
    algorithm TEXT NOT NULL DEFAULT 'first_come'
        CHECK(algorithm IN ('first_come','random','weighted')),
    cap INTEGER DEFAULT 12,
    cap_enabled INTEGER DEFAULT 1,
    created_by INTEGER NOT NULL REFERENCES players(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notified_at TEXT,
    notify_future_at TEXT,
    phase TEXT NOT NULL DEFAULT 'created'
        CHECK(phase IN ('created','notifying_high','notifying_standard',
                        'notifying_low','signup','active','closed','cancelled')),
    selection_done INTEGER DEFAULT 0,
    closed INTEGER DEFAULT 0,
    batch_id TEXT,
    random_high_auto INTEGER DEFAULT 1,
    FOREIGN KEY (created_by) REFERENCES players(id)
);

CREATE TABLE IF NOT EXISTS game_signups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    signed_up_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('in','waitlist','pending')),
    owner_added INTEGER DEFAULT 0,
    UNIQUE(game_id, player_id)
);

CREATE TABLE IF NOT EXISTS game_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    notification_type TEXT NOT NULL, channel TEXT NOT NULL,
    message TEXT NOT NULL, sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    delivered INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS notification_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_id INTEGER NOT NULL, channel TEXT NOT NULL,
    subject TEXT, body TEXT NOT NULL, sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── Security ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL COLLATE NOCASE, ip_address TEXT NOT NULL DEFAULT '',
    success INTEGER NOT NULL DEFAULT 0, attempted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_email_time ON login_attempts(email, attempted_at);

CREATE TABLE IF NOT EXISTS token_blacklist (
    jti TEXT PRIMARY KEY, player_id INTEGER NOT NULL,
    blacklisted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, expires_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_token_blacklist_expires ON token_blacklist(expires_at);

CREATE TABLE IF NOT EXISTS scheduler_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    job_type TEXT NOT NULL CHECK(job_type IN ('notify_high','notify_standard','notify_low','run_selection')),
    scheduled_at TEXT NOT NULL, executed_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','completed','failed')),
    error_message TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(game_id, job_type)
);
CREATE INDEX IF NOT EXISTS idx_scheduler_pending ON scheduler_jobs(status, scheduled_at);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    token TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL,
    used INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── Legacy settings table (kept for migration, replaced by group_settings) ──
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "default_cap": "12", "cap_enabled": "1", "default_algorithm": "first_come",
    "default_location": "", "high_priority_delay_minutes": "60",
    "alternative_delay_minutes": "1440", "random_wait_period_minutes": "60",
    "notify_owner_new_signup": "1",
}


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA trusted_schema=OFF")
    return db


async def init_db():
    db = await get_db()
    try:
        await db.executescript(SCHEMA)
        await _run_migrations(db)
        await db.commit()
    finally:
        await db.close()


async def _run_migrations(db):
    """Migrate legacy pre-groups DB to groups model."""
    # Add columns that may be missing from older DBs
    for tbl, col, defn in [
        ("players", "force_password_change", "INTEGER DEFAULT 0"),
        ("games", "group_id", "INTEGER NOT NULL DEFAULT 0"),
        ("games", "random_high_auto", "INTEGER DEFAULT 1"),
        ("games", "batch_id", "TEXT"),
        ("locations", "group_id", "INTEGER NOT NULL DEFAULT 0"),
        ("locations", "address", "TEXT DEFAULT ''"),
        ("locations", "sort_order", "INTEGER DEFAULT 0"),
    ]:
        cursor = await db.execute(f"PRAGMA table_info({tbl})")
        cols = {r["name"] for r in await cursor.fetchall()}
        if col not in cols:
            await db.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {defn}")

    # Migrate push -> email
    await db.execute("UPDATE players SET notif_pref='email' WHERE notif_pref='push'")

    # Check if groups exist already
    cursor = await db.execute("SELECT COUNT(*) as c FROM groups")
    if (await cursor.fetchone())["c"] > 0:
        return  # Already migrated

    # Only create friedland group if there are existing players to migrate
    cursor = await db.execute("SELECT COUNT(*) as c FROM players")
    if (await cursor.fetchone())["c"] == 0:
        return  # Fresh install, no data to migrate

    # Create default "friedland" group
    await db.execute("INSERT OR IGNORE INTO groups (name) VALUES ('friedland')")
    cursor = await db.execute("SELECT id FROM groups WHERE name='friedland'")
    gid = (await cursor.fetchone())["id"]

    # Migrate existing players to friedland
    cursor = await db.execute("SELECT id, role, priority, status FROM players")
    for p in await cursor.fetchall():
        gm_role = "organizer" if p["role"] == "owner" else "player"
        gm_status = "active" if p["status"] == "approved" else "pending"
        await db.execute(
            "INSERT OR IGNORE INTO group_members (group_id,player_id,role,priority,status) VALUES (?,?,?,?,?)",
            (gid, p["id"], gm_role, p["priority"] or "standard", gm_status))

    # Migrate games, locations to friedland
    await db.execute("UPDATE games SET group_id=? WHERE group_id=0", (gid,))
    await db.execute("UPDATE locations SET group_id=? WHERE group_id=0", (gid,))

    # Migrate old settings table to group_settings
    cursor = await db.execute("SELECT key, value FROM settings")
    for row in await cursor.fetchall():
        await db.execute(
            "INSERT OR IGNORE INTO group_settings (key,value,group_id) VALUES (?,?,?)",
            (row["key"], row["value"], gid))

    # Ensure defaults
    await ensure_group_settings(db, gid)
    await db.commit()
    logger.info(f"✅ Migrated existing data to 'friedland' group (id={gid})")


async def ensure_group_settings(db, group_id: int):
    for key, value in DEFAULT_SETTINGS.items():
        await db.execute(
            "INSERT OR IGNORE INTO group_settings (key,value,group_id) VALUES (?,?,?)",
            (key, value, group_id))
    await db.commit()


async def get_setting(db, key, group_id):
    cursor = await db.execute(
        "SELECT value FROM group_settings WHERE key=? AND group_id=?", (key, group_id))
    row = await cursor.fetchone()
    return row["value"] if row else DEFAULT_SETTINGS.get(key)


async def set_setting(db, key, value, group_id):
    await db.execute(
        "INSERT OR REPLACE INTO group_settings (key,value,group_id) VALUES (?,?,?)",
        (key, value, group_id))
    await db.commit()


async def cleanup_expired_tokens(db):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute("DELETE FROM token_blacklist WHERE expires_at < ?", (now,))
    await db.commit()


async def cleanup_old_login_attempts(db, days=7):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    await db.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff,))
    await db.commit()
