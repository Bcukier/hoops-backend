"""
Database setup and initialization for the Hoops pickup basketball app.
Uses aiosqlite for async SQLite access.
"""
import aiosqlite
import os
from datetime import datetime, timezone, timedelta

DB_PATH = os.environ.get("HOOPS_DB_PATH", "hoops.db")

SCHEMA = """
-- ── Core Tables ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL COLLATE NOCASE,
    mobile TEXT DEFAULT '',
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'player' CHECK(role IN ('owner','player')),
    priority TEXT NOT NULL DEFAULT 'standard' CHECK(priority IN ('high','standard','low')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','denied')),
    notif_pref TEXT NOT NULL DEFAULT 'email',
    force_password_change INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    address TEXT DEFAULT '',
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    FOREIGN KEY (created_by) REFERENCES players(id)
);

CREATE TABLE IF NOT EXISTS game_signups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    signed_up_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('in','waitlist','pending')),
    owner_added INTEGER DEFAULT 0,
    UNIQUE(game_id, player_id)
);

CREATE TABLE IF NOT EXISTS game_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    notification_type TEXT NOT NULL,
    channel TEXT NOT NULL,
    message TEXT NOT NULL,
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    delivered INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS notification_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_id INTEGER NOT NULL,
    channel TEXT NOT NULL,
    subject TEXT,
    body TEXT NOT NULL,
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── Security Tables ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL COLLATE NOCASE,
    ip_address TEXT NOT NULL DEFAULT '',
    success INTEGER NOT NULL DEFAULT 0,
    attempted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_email_time
    ON login_attempts(email, attempted_at);

CREATE TABLE IF NOT EXISTS token_blacklist (
    jti TEXT PRIMARY KEY,
    player_id INTEGER NOT NULL,
    blacklisted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_token_blacklist_expires
    ON token_blacklist(expires_at);

-- ── Scheduler State ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS scheduler_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    job_type TEXT NOT NULL
        CHECK(job_type IN ('notify_high','notify_standard','notify_low','run_selection')),
    scheduled_at TEXT NOT NULL,       -- ISO datetime when job should execute
    executed_at TEXT,                  -- ISO datetime when job actually ran
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','running','completed','failed')),
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(game_id, job_type)
);
CREATE INDEX IF NOT EXISTS idx_scheduler_pending
    ON scheduler_jobs(status, scheduled_at);

-- ── Password Reset Tokens ───────────────────────────────────

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    token TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    used INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

DEFAULT_SETTINGS = {
    "default_cap": "12",
    "cap_enabled": "1",
    "default_algorithm": "first_come",
    "default_location": "",
    "high_priority_delay_minutes": "60",
    "alternative_delay_minutes": "1440",
    "random_wait_period_minutes": "60",
    "notify_owner_new_signup": "1",
}

DEFAULT_LOCATIONS = [
    "Central Park Courts",
    "YMCA Gym",
    "LA Fitness",
    "Community Center",
    "Outdoor Courts @ 5th",
]


async def get_db() -> aiosqlite.Connection:
    """Get a database connection with row factory and security pragmas."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA trusted_schema=OFF")
    return db


async def init_db():
    """Initialize the database schema and seed data."""
    db = await get_db()
    try:
        await db.executescript(SCHEMA)

        # ── Migrations for existing databases ──
        # Players: force_password_change
        cursor = await db.execute("PRAGMA table_info(players)")
        pcols = {row["name"] for row in await cursor.fetchall()}
        if "force_password_change" not in pcols:
            await db.execute("ALTER TABLE players ADD COLUMN force_password_change INTEGER DEFAULT 0")

        # Locations: address, sort_order
        cursor = await db.execute("PRAGMA table_info(locations)")
        lcols = {row["name"] for row in await cursor.fetchall()}
        if "address" not in lcols:
            await db.execute("ALTER TABLE locations ADD COLUMN address TEXT DEFAULT ''")
        if "sort_order" not in lcols:
            await db.execute("ALTER TABLE locations ADD COLUMN sort_order INTEGER DEFAULT 0")

        # Games: batch_id
        cursor = await db.execute("PRAGMA table_info(games)")
        gcols = {row["name"] for row in await cursor.fetchall()}
        if "batch_id" not in gcols:
            await db.execute("ALTER TABLE games ADD COLUMN batch_id TEXT")

        # Migrate notif_pref 'push' → 'email' for existing players
        await db.execute("UPDATE players SET notif_pref = 'email' WHERE notif_pref = 'push'")

        # Ensure default_location setting exists
        cursor = await db.execute("SELECT 1 FROM settings WHERE key = 'default_location'")
        if not await cursor.fetchone():
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('default_location', '')")

        cursor = await db.execute("SELECT COUNT(*) as c FROM settings")
        row = await cursor.fetchone()
        if row["c"] == 0:
            for key, value in DEFAULT_SETTINGS.items():
                await db.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (key, value),
                )

        cursor = await db.execute("SELECT COUNT(*) as c FROM locations")
        row = await cursor.fetchone()
        if row["c"] == 0:
            for i, loc in enumerate(DEFAULT_LOCATIONS):
                await db.execute(
                    "INSERT OR IGNORE INTO locations (name, sort_order) VALUES (?, ?)",
                    (loc, i),
                )

        await db.commit()
    finally:
        await db.close()


async def get_setting(db: aiosqlite.Connection, key: str) -> str | None:
    cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = await cursor.fetchone()
    return row["value"] if row else None


async def set_setting(db: aiosqlite.Connection, key: str, value: str):
    await db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
    )
    await db.commit()


async def cleanup_expired_tokens(db: aiosqlite.Connection):
    """Remove expired entries from the token blacklist."""
    now = datetime.now(timezone.utc).isoformat()
    await db.execute("DELETE FROM token_blacklist WHERE expires_at < ?", (now,))
    await db.commit()


async def cleanup_old_login_attempts(db: aiosqlite.Connection, days: int = 7):
    """Remove login attempts older than N days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    await db.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff,))
    await db.commit()
