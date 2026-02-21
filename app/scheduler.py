"""
Background scheduler for the Hoops app.

Runs as an asyncio task alongside the FastAPI server. Polls the database
every POLL_INTERVAL seconds and processes pending jobs:

1. Delayed game notifications (notify_future_at)
2. Priority-cascaded notifications (high → standard → low)
3. Auto-run random selection after wait period expires
4. Periodic cleanup of expired tokens and old login attempts

Job lifecycle:
  Game created → scheduler_jobs rows created (pending)
  Scheduler polls → picks up due jobs → executes → marks completed/failed
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from app.database import (
    get_db,
    get_setting,
    cleanup_expired_tokens,
    cleanup_old_login_attempts,
)
from app.notifications import notify_game_signup_open
from app.algorithms import run_random_selection

logger = logging.getLogger("hoops.scheduler")

POLL_INTERVAL = 30      # seconds between scheduler ticks
CLEANUP_INTERVAL = 3600 # seconds between cleanup runs


class Scheduler:
    """Background job scheduler for the Hoops app."""

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._running = False

    async def start(self):
        """Start the scheduler background tasks."""
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("⏰ Scheduler started (poll=%ds)", POLL_INTERVAL)

    async def stop(self):
        """Gracefully stop the scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        logger.info("⏰ Scheduler stopped")

    # ── Main Loop ─────────────────────────────────────────────

    async def _run_loop(self):
        """Main scheduler polling loop."""
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"Scheduler tick error: {e}", exc_info=True)
            await asyncio.sleep(POLL_INTERVAL)

    async def _tick(self):
        """Single scheduler tick: process all due jobs."""
        db = await get_db()
        try:
            now = datetime.now(timezone.utc).isoformat()

            # 1. Process games waiting for future notification time
            await self._process_future_notifications(db, now)

            # 2. Process pending scheduler jobs
            cursor = await db.execute(
                """SELECT * FROM scheduler_jobs
                   WHERE status = 'pending' AND scheduled_at <= ?
                   ORDER BY scheduled_at ASC""",
                (now,),
            )
            jobs = await cursor.fetchall()

            for job in jobs:
                await self._execute_job(db, dict(job))

        finally:
            await db.close()

    # ── Future Notification Trigger ───────────────────────────

    async def _process_future_notifications(self, db, now: str):
        """
        Check for games in 'created' phase whose notify_future_at has passed.
        Transition them to active notification and schedule cascade jobs.
        """
        cursor = await db.execute(
            """SELECT * FROM games
               WHERE phase = 'created'
                 AND notify_future_at IS NOT NULL
                 AND notify_future_at <= ?
                 AND closed = 0""",
            (now,),
        )
        games = await cursor.fetchall()

        for game in games:
            g = dict(game)
            game_id = g["id"]
            logger.info(f"Game {game_id}: triggering delayed notifications")

            # Update game state
            await db.execute(
                """UPDATE games
                   SET notified_at = ?, phase = 'notifying_high'
                   WHERE id = ?""",
                (now, game_id),
            )
            await db.commit()

            # Notify high-priority players immediately
            await self._notify_priority_tier(db, game_id, "high", g)

            # Schedule cascade jobs
            await self._schedule_cascade_jobs(db, game_id, now)

    # ── Job Execution ─────────────────────────────────────────

    async def _execute_job(self, db, job: dict):
        """Execute a single scheduler job."""
        job_id = job["id"]
        game_id = job["game_id"]
        job_type = job["job_type"]

        # Mark as running
        await db.execute(
            "UPDATE scheduler_jobs SET status = 'running' WHERE id = ?",
            (job_id,),
        )
        await db.commit()

        try:
            # Verify game is still open
            cursor = await db.execute(
                "SELECT * FROM games WHERE id = ? AND closed = 0",
                (game_id,),
            )
            game = await cursor.fetchone()
            if not game:
                logger.info(f"Job {job_id}: game {game_id} closed/missing, skipping")
                await self._complete_job(db, job_id)
                return

            g = dict(game)

            if job_type == "notify_standard":
                await self._notify_priority_tier(db, game_id, "standard", g)
                await db.execute(
                    "UPDATE games SET phase = 'notifying_standard' WHERE id = ?",
                    (game_id,),
                )

            elif job_type == "notify_low":
                # Only notify low-priority if spots still available
                spots_open = await self._check_spots_open(db, game_id, g)
                if spots_open:
                    await self._notify_priority_tier(db, game_id, "low", g)
                    await db.execute(
                        "UPDATE games SET phase = 'notifying_low' WHERE id = ?",
                        (game_id,),
                    )
                else:
                    logger.info(
                        f"Job {job_id}: game {game_id} full, skipping low-priority notification"
                    )

            elif job_type == "run_selection":
                if not g["selection_done"] and g["algorithm"] == "random":
                    logger.info(f"Job {job_id}: auto-running selection for game {game_id}")
                    await run_random_selection(db, game_id)
                else:
                    logger.info(
                        f"Job {job_id}: selection already done for game {game_id}"
                    )

            elif job_type == "notify_high":
                await self._notify_priority_tier(db, game_id, "high", g)

            await self._complete_job(db, job_id)
            await db.commit()

        except Exception as e:
            logger.error(f"Job {job_id} failed: {e}", exc_info=True)
            await db.execute(
                """UPDATE scheduler_jobs
                   SET status = 'failed', error_message = ?,
                       executed_at = ?
                   WHERE id = ?""",
                (str(e), datetime.now(timezone.utc).isoformat(), job_id),
            )
            await db.commit()

    async def _complete_job(self, db, job_id: int):
        """Mark a job as completed."""
        await db.execute(
            """UPDATE scheduler_jobs
               SET status = 'completed', executed_at = ?
               WHERE id = ?""",
            (datetime.now(timezone.utc).isoformat(), job_id),
        )

    # ── Notification Helpers ──────────────────────────────────

    async def _notify_priority_tier(
        self, db, game_id: int, priority: str, game: dict
    ):
        """
        Send signup-open notifications to all approved players of a given
        priority tier who haven't already been notified for this game.
        If random_high_auto is True and this is a random game notifying high
        priority, also auto-sign them up (guaranteed spot).
        """
        # Get players of this priority who haven't been notified
        cursor = await db.execute(
            """SELECT p.id FROM players p
               WHERE p.status = 'approved'
                 AND p.priority = ?
                 AND p.id NOT IN (
                     SELECT gn.player_id FROM game_notifications gn
                     WHERE gn.game_id = ? AND gn.notification_type = 'signup_open'
                 )""",
            (priority, game_id),
        )
        players = [row["id"] for row in await cursor.fetchall()]

        if not players:
            logger.info(f"Game {game_id}: no {priority}-priority players to notify")
            return

        # Auto-sign up high-priority players if random_high_auto is enabled
        if (
            priority == "high"
            and game.get("algorithm") == "random"
            and game.get("random_high_auto", 1)
        ):
            for pid in players:
                await db.execute(
                    """INSERT OR IGNORE INTO game_signups
                       (game_id, player_id, status, owner_added)
                       VALUES (?, ?, 'pending', 1)""",
                    (game_id, pid),
                )
            await db.commit()
            logger.info(
                f"Game {game_id}: auto-signed up {len(players)} high-priority players"
            )

        logger.info(
            f"Game {game_id}: notifying {len(players)} {priority}-priority players"
        )
        await notify_game_signup_open(
            db, game_id, players, game["date"], game["location"]
        )

    async def _check_spots_open(self, db, game_id: int, game: dict) -> bool:
        """Check if a game still has open spots."""
        if not game["cap_enabled"]:
            return True
        cursor = await db.execute(
            """SELECT COUNT(*) as c FROM game_signups
               WHERE game_id = ? AND status = 'in'""",
            (game_id,),
        )
        row = await cursor.fetchone()
        return row["c"] < game["cap"]

    # ── Cascade Job Scheduling ────────────────────────────────

    async def _schedule_cascade_jobs(
        self, db, game_id: int, notification_start: str
    ):
        """
        Schedule the full notification cascade for a game:
          notify_high (immediate) → notify_standard (+hp_delay) → notify_low (+alt_delay)
        For random algorithm, also schedule auto-selection.
        """
        start = datetime.fromisoformat(notification_start)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)

        hp_delay = int(await get_setting(db, "high_priority_delay_minutes") or 60)
        alt_delay = int(await get_setting(db, "alternative_delay_minutes") or 1440)
        rand_wait = int(await get_setting(db, "random_wait_period_minutes") or 60)

        standard_time = start + timedelta(minutes=hp_delay)
        low_time = standard_time + timedelta(minutes=alt_delay)

        # Schedule standard notification
        await db.execute(
            """INSERT OR IGNORE INTO scheduler_jobs
               (game_id, job_type, scheduled_at)
               VALUES (?, 'notify_standard', ?)""",
            (game_id, standard_time.isoformat()),
        )

        # Schedule low notification
        await db.execute(
            """INSERT OR IGNORE INTO scheduler_jobs
               (game_id, job_type, scheduled_at)
               VALUES (?, 'notify_low', ?)""",
            (game_id, low_time.isoformat()),
        )

        # For random algorithm, schedule auto-selection
        cursor = await db.execute(
            "SELECT algorithm FROM games WHERE id = ?", (game_id,)
        )
        game = await cursor.fetchone()
        if game and game["algorithm"] == "random":
            selection_time = standard_time + timedelta(minutes=rand_wait)
            await db.execute(
                """INSERT OR IGNORE INTO scheduler_jobs
                   (game_id, job_type, scheduled_at)
                   VALUES (?, 'run_selection', ?)""",
                (game_id, selection_time.isoformat()),
            )

        await db.commit()
        logger.info(
            f"Game {game_id}: cascade scheduled — "
            f"standard={standard_time.isoformat()}, low={low_time.isoformat()}"
        )

    # ── Cleanup Loop ──────────────────────────────────────────

    async def _cleanup_loop(self):
        """Periodic cleanup of expired tokens and old data."""
        while self._running:
            try:
                db = await get_db()
                try:
                    await cleanup_expired_tokens(db)
                    await cleanup_old_login_attempts(db)
                    logger.debug("Cleanup tick completed")
                finally:
                    await db.close()
            except Exception as e:
                logger.error(f"Cleanup error: {e}", exc_info=True)
            await asyncio.sleep(CLEANUP_INTERVAL)


# ══════════════════════════════════════════════════════════════
# PUBLIC API FOR GAME CREATION
# ══════════════════════════════════════════════════════════════

async def schedule_game_notifications(game_id: int, notify_at: str | None):
    """
    Called when a game is created.
    If notify_at is None → send high-priority notifications immediately
    and schedule cascade. If notify_at is set → schedule everything for later.
    """
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM games WHERE id = ?", (game_id,))
        game = await cursor.fetchone()
        if not game:
            return
        g = dict(game)

        if not notify_at:
            # Immediate: notify high-priority now, schedule the rest
            now = datetime.now(timezone.utc)

            # Update game state
            await db.execute(
                """UPDATE games SET notified_at = ?, phase = 'notifying_high'
                   WHERE id = ?""",
                (now.isoformat(), game_id),
            )
            await db.commit()

            # Send high-priority notifications
            cursor2 = await db.execute(
                """SELECT id FROM players
                   WHERE status = 'approved' AND priority = 'high'"""
            )
            high_players = [r["id"] for r in await cursor2.fetchall()]
            if high_players:
                # Auto-sign up high-priority if random_high_auto is on
                if g["algorithm"] == "random" and g.get("random_high_auto", 1):
                    for pid in high_players:
                        await db.execute(
                            """INSERT OR IGNORE INTO game_signups
                               (game_id, player_id, status, owner_added)
                               VALUES (?, ?, 'pending', 1)""",
                            (game_id, pid),
                        )
                    await db.commit()
                    logger.info(
                        f"Game {game_id}: auto-signed up {len(high_players)} high-priority players"
                    )
                await notify_game_signup_open(
                    db, game_id, high_players, g["date"], g["location"]
                )

            # Schedule cascade jobs
            scheduler = Scheduler()
            await scheduler._schedule_cascade_jobs(
                db, game_id, now.isoformat()
            )

        else:
            # Delayed: game stays in 'created' phase.
            # The scheduler loop in _process_future_notifications will pick it up.
            logger.info(
                f"Game {game_id}: notifications scheduled for {notify_at}"
            )

    finally:
        await db.close()


# Global scheduler instance
scheduler = Scheduler()
