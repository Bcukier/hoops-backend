"""
Notification service.
In production, integrate with SendGrid (email), Twilio (SMS), FCM (push).
For now it logs notifications and stores them in the database.
"""
import logging
from datetime import datetime, timezone
import aiosqlite

logger = logging.getLogger("hoops.notifications")


async def send_notification(
    db: aiosqlite.Connection,
    player_id: int,
    channel: str,
    subject: str,
    body: str,
):
    """
    Send a notification to a player via their preferred channel.
    In production, this dispatches to email/SMS/push services.
    """
    await db.execute(
        """INSERT INTO notification_log (recipient_id, channel, subject, body)
           VALUES (?, ?, ?, ?)""",
        (player_id, channel, subject, body),
    )
    await db.commit()

    if channel == "email":
        logger.info(f"📧 EMAIL to player {player_id}: {subject} — {body[:80]}")
    elif channel == "sms":
        logger.info(f"📱 SMS to player {player_id}: {body[:120]}")
    elif channel == "push":
        logger.info(f"🔔 PUSH to player {player_id}: {subject} — {body[:80]}")
    else:
        logger.warning(f"Unknown channel '{channel}' for player {player_id}")


async def notify_game_signup_open(
    db: aiosqlite.Connection,
    game_id: int,
    player_ids: list[int],
    game_date: str,
    game_location: str,
):
    """Notify a batch of players that signup is open for a game."""
    for pid in player_ids:
        cursor = await db.execute(
            "SELECT notif_pref FROM players WHERE id = ?", (pid,)
        )
        row = await cursor.fetchone()
        if not row:
            continue
        channel = row["notif_pref"]
        subject = f"🏀 Pickup Game — {game_date}"
        body = (
            f"Signup is open for the game at {game_location} on {game_date}. "
            f"Open the app to sign up!"
        )
        await send_notification(db, pid, channel, subject, body)
        await db.execute(
            """INSERT INTO game_notifications
               (game_id, player_id, notification_type, channel, message, delivered)
               VALUES (?, ?, 'signup_open', ?, ?, 1)""",
            (game_id, pid, channel, body),
        )
    await db.commit()


async def notify_selection_results(
    db: aiosqlite.Connection,
    game_id: int,
    in_players: list[int],
    waitlist_players: list[dict],
):
    """Notify players of their selection result."""
    for pid in in_players:
        cursor = await db.execute(
            "SELECT notif_pref FROM players WHERE id = ?", (pid,)
        )
        row = await cursor.fetchone()
        if not row:
            continue
        await send_notification(
            db, pid, row["notif_pref"],
            "🏀 You're in the game!",
            "You've been selected for the game. See you on the court!",
        )

    for entry in waitlist_players:
        pid = entry["player_id"]
        pos = entry["position"]
        cursor = await db.execute(
            "SELECT notif_pref FROM players WHERE id = ?", (pid,)
        )
        row = await cursor.fetchone()
        if not row:
            continue
        await send_notification(
            db, pid, row["notif_pref"],
            "🏀 Waitlisted",
            f"You're #{pos} on the waitlist. We'll notify you if a spot opens up.",
        )


async def notify_waitlist_promotion(
    db: aiosqlite.Connection,
    game_id: int,
    player_id: int,
):
    """Notify a player they've been promoted from waitlist to the game."""
    cursor = await db.execute(
        "SELECT notif_pref FROM players WHERE id = ?", (player_id,)
    )
    row = await cursor.fetchone()
    if not row:
        return
    await send_notification(
        db, player_id, row["notif_pref"],
        "🏀 You're in!",
        "A spot opened up and you've been moved into the game!",
    )
    await db.execute(
        """INSERT INTO game_notifications
           (game_id, player_id, notification_type, channel, message, delivered)
           VALUES (?, ?, 'waitlist_promotion', ?, ?, 1)""",
        (game_id, player_id, row["notif_pref"], "Promoted from waitlist"),
    )
    await db.commit()


async def notify_owner_player_drop(
    db: aiosqlite.Connection,
    game_id: int,
    player_name: str,
    drop_time: str,
):
    """Notify all owners that a player dropped from a game."""
    cursor = await db.execute(
        "SELECT id, notif_pref FROM players WHERE role = 'owner' AND status = 'approved'"
    )
    owners = await cursor.fetchall()
    for owner in owners:
        await send_notification(
            db, owner["id"], owner["notif_pref"],
            "⚠️ Player Drop",
            f"{player_name} dropped from game #{game_id} at {drop_time}",
        )


async def notify_owners_new_signup(
    db: aiosqlite.Connection,
    player_name: str,
    player_email: str,
):
    """Notify owners about a new player registration."""
    cursor = await db.execute(
        "SELECT id, notif_pref FROM players WHERE role = 'owner' AND status = 'approved'"
    )
    owners = await cursor.fetchall()
    for owner in owners:
        await send_notification(
            db, owner["id"], owner["notif_pref"],
            "👤 New Player Signup",
            f"{player_name} ({player_email}) has registered and needs approval.",
        )
