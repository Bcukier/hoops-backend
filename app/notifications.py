"""
Notification service with SendGrid (email) and Twilio (SMS) integration.

Configure via environment variables:
  SENDGRID_API_KEY       - SendGrid API key
  SENDGRID_FROM_EMAIL    - Verified sender email (e.g. hoops@goatcommish.com)
  SENDGRID_FROM_NAME     - Display name (default: GOATcommish)
  TWILIO_ACCOUNT_SID     - Twilio Account SID
  TWILIO_AUTH_TOKEN      - Twilio Auth Token
  TWILIO_FROM_NUMBER     - Twilio phone number (e.g. +15551234567)

If credentials are missing, notifications are logged but not delivered.

notif_pref is comma-separated: "email", "sms", "email,sms", or "none".
When "none", no notifications are sent. Multi-select sends via ALL selected channels.
"""
import os
import logging
import asyncio
import re
from datetime import datetime, timezone
from functools import partial

import aiosqlite

logger = logging.getLogger("hoops.notifications")

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
SENDGRID_FROM_EMAIL = os.environ.get("SENDGRID_FROM_EMAIL", "")
SENDGRID_FROM_NAME = os.environ.get("SENDGRID_FROM_NAME", "GOATcommish")

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")

# Lazy-initialized client
_twilio_client = None


def _get_twilio_client():
    global _twilio_client
    if _twilio_client is None and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
        from twilio.rest import Client
        _twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        logger.info("📱 Twilio client initialized")
    return _twilio_client


# ═══════════════════════════════════════════════════════════════
# EMAIL (SendGrid API — uses HTTPS, no SMTP ports needed)
# ═══════════════════════════════════════════════════════════════

def _send_email_sync(to_email: str, subject: str, body: str) -> bool:
    """Send an email via SendGrid API (blocking — run in executor)."""
    if not SENDGRID_API_KEY or not SENDGRID_FROM_EMAIL:
        logger.warning(f"📧 SendGrid not configured — email to {to_email} logged only")
        return False

    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail, Email, To, Content, HtmlContent

    # HTML version
    html_body = body.replace("\n", "<br>")
    # Convert "Open the app...:\n<URL>" into a styled hyperlink in HTML
    html_body = re.sub(
        r'Open the app(.*?):<br>https?://[^\s<]+',
        r'<a href="https://www.goatcommish.com" style="color:#ff6a2f;font-weight:bold;text-decoration:underline;">Open the app</a>\1.',
        html_body,
    )
    # Linkify any remaining bare URLs (e.g. password reset links)
    parts = re.split(r'(<a [^>]*>.*?</a>)', html_body)
    for i, part in enumerate(parts):
        if not part.startswith('<a '):
            parts[i] = re.sub(
                r'(https?://[^\s<]+)',
                r'<a href="\1" style="color:#ff6a2f;text-decoration:underline;">\1</a>',
                part,
            )
    html_body = ''.join(parts)

    html = f"""\
    <div style="font-family:sans-serif;max-width:500px;margin:0 auto;padding:20px;">
        <h2 style="color:#ff6a2f;">{subject}</h2>
        <p style="color:#333;line-height:1.6;">{html_body}</p>
        <hr style="border:none;border-top:1px solid #eee;margin:20px 0;">
        <p style="color:#999;font-size:12px;">Sent by GOATcommish — Pickup Basketball</p>
    </div>"""

    message = Mail(
        from_email=Email(SENDGRID_FROM_EMAIL, SENDGRID_FROM_NAME),
        to_emails=To(to_email),
        subject=subject,
        plain_text_content=Content("text/plain", body),
        html_content=HtmlContent(html),
    )

    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)
        logger.info(f"📧 Email sent to {to_email}: {subject} (status={response.status_code})")
        return response.status_code in (200, 201, 202)
    except Exception as e:
        logger.error(f"📧 SendGrid error sending to {to_email}: {e}")
        return False


async def send_email(to_email: str, subject: str, body: str) -> bool:
    """Send email asynchronously via SendGrid API."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(_send_email_sync, to_email, subject, body))


# ═══════════════════════════════════════════════════════════════
# SMS (Twilio)
# ═══════════════════════════════════════════════════════════════

def _send_sms_sync(to_number: str, body: str) -> bool:
    """Send an SMS via Twilio (blocking — run in executor)."""
    client = _get_twilio_client()
    if not client or not TWILIO_FROM_NUMBER:
        logger.warning(f"📱 Twilio not configured — SMS to {to_number} logged only")
        return False

    # Ensure number has country code
    clean_number = to_number.strip()
    if not clean_number.startswith("+"):
        digits = "".join(c for c in clean_number if c.isdigit())
        if len(digits) == 10:
            clean_number = f"+1{digits}"
        elif len(digits) == 11 and digits.startswith("1"):
            clean_number = f"+{digits}"
        else:
            logger.warning(f"📱 Invalid phone number format: {to_number}")
            return False

    try:
        message = client.messages.create(
            body=body,
            from_=TWILIO_FROM_NUMBER,
            to=clean_number,
        )
        logger.info(f"📱 SMS sent to {clean_number}: SID={message.sid}")
        return True
    except Exception as e:
        logger.error(f"📱 Twilio exception sending to {clean_number}: {e}")
        return False


async def send_sms(to_number: str, body: str) -> bool:
    """Send SMS asynchronously via Twilio."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(_send_sms_sync, to_number, body))


# ═══════════════════════════════════════════════════════════════
# UNIFIED NOTIFICATION DISPATCHER — MULTI-CHANNEL SUPPORT
# ═══════════════════════════════════════════════════════════════

def _parse_notif_pref(pref: str) -> list[str]:
    """Parse comma-separated notif_pref into list of channels.
    Returns empty list for 'none' (user opts out of notifications).
    """
    if not pref or pref.strip().lower() == "none":
        return []
    channels = [c.strip().lower() for c in pref.split(",") if c.strip()]
    # Filter to valid channels only
    return [c for c in channels if c in ("email", "sms")]


async def send_notification(
    db: aiosqlite.Connection,
    player_id: int,
    notif_pref: str,
    subject: str,
    body: str,
):
    """
    Send a notification to a player via ALL their preferred channels.
    notif_pref is comma-separated (e.g. "email,sms"). "none" = no delivery.
    Always logs to the database.
    """
    channels = _parse_notif_pref(notif_pref)

    # Log to notification_log table (always, even if none)
    channel_str = notif_pref or "none"
    await db.execute(
        """INSERT INTO notification_log (recipient_id, channel, subject, body)
           VALUES (?, ?, ?, ?)""",
        (player_id, channel_str, subject, body),
    )
    await db.commit()

    if not channels:
        logger.info(f"📋 Player {player_id} has notifications set to 'none' — logged only")
        return

    # Look up player contact info
    cursor = await db.execute(
        "SELECT email, mobile FROM players WHERE id = ?", (player_id,)
    )
    player = await cursor.fetchone()
    if not player:
        logger.warning(f"Player {player_id} not found for notification")
        return

    any_delivered = False

    for channel in channels:
        if channel == "email":
            if player["email"]:
                delivered = await send_email(player["email"], subject, body)
                any_delivered = any_delivered or delivered
            else:
                logger.warning(f"Player {player_id} has no email address")

        elif channel == "sms":
            if player["mobile"]:
                delivered = await send_sms(player["mobile"], f"{subject}\n\n{body}")
                any_delivered = any_delivered or delivered
            else:
                logger.warning(f"Player {player_id} has no mobile number")

    if not any_delivered:
        logger.info(f"📋 Notification logged (not delivered) — player {player_id}: {subject}")


# ═══════════════════════════════════════════════════════════════
# HIGH-LEVEL NOTIFICATION FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def _format_game_date(game_date: str):
    """Format a game date string for display. Returns (nice_date, day, time)."""
    try:
        d = datetime.fromisoformat(game_date)
        nice_date = d.strftime("%A, %B %d at %I:%M %p")
        day = d.strftime("%A")
        time_str = d.strftime("%I:%M %p").lstrip("0")
        return nice_date, day, time_str
    except Exception:
        return game_date, game_date, ""


async def notify_game_signup_open(
    db: aiosqlite.Connection,
    game_id: int,
    player_ids: list[int],
    game_date: str,
    game_location: str,
):
    """Notify a batch of players that signup is open for a game."""
    nice_date, subject_day, subject_time = _format_game_date(game_date)

    for pid in player_ids:
        cursor = await db.execute(
            "SELECT notif_pref FROM players WHERE id = ?", (pid,)
        )
        row = await cursor.fetchone()
        if not row:
            continue
        notif_pref = row["notif_pref"]
        subject = f"GOATcommish - New Game {subject_day} {subject_time}@{game_location}"
        body = (
            f"Signup is open for pickup basketball!\n\n"
            f"📍 {game_location}\n"
            f"🕐 {nice_date}\n\n"
            f"Open the app to sign up before spots fill:\n"
            f"https://www.goatcommish.com"
        )
        await send_notification(db, pid, notif_pref, subject, body)
        await db.execute(
            """INSERT INTO game_notifications
               (game_id, player_id, notification_type, channel, message, delivered)
               VALUES (?, ?, 'signup_open', ?, ?, 1)""",
            (game_id, pid, notif_pref, body),
        )
    await db.commit()


async def notify_batch_games_signup_open(
    db: aiosqlite.Connection,
    game_infos: list[dict],
    player_ids: list[int],
):
    """Notify players about multiple games in a single notification.
    game_infos: list of dicts with keys: id, date, location
    """
    if not game_infos:
        return

    # Build the combined game list
    game_lines = []
    for g in game_infos:
        nice_date, _, _ = _format_game_date(g["date"])
        game_lines.append(f"📍 {g['location']} — 🕐 {nice_date}")

    game_list_text = "\n".join(game_lines)
    n = len(game_infos)
    subject = f"GOATcommish - {n} New Game{'s' if n > 1 else ''} This Week"

    body = (
        f"{n} new game{'s have' if n > 1 else ' has'} been posted!\n\n"
        f"{game_list_text}\n\n"
        f"Open the app to sign up before spots fill:\n"
        f"https://www.goatcommish.com"
    )

    for pid in player_ids:
        cursor = await db.execute(
            "SELECT notif_pref FROM players WHERE id = ?", (pid,)
        )
        row = await cursor.fetchone()
        if not row:
            continue
        notif_pref = row["notif_pref"]
        await send_notification(db, pid, notif_pref, subject, body)
        # Log against first game in batch
        for g in game_infos:
            await db.execute(
                """INSERT INTO game_notifications
                   (game_id, player_id, notification_type, channel, message, delivered)
                   VALUES (?, ?, 'signup_open', ?, ?, 1)""",
                (g["id"], pid, notif_pref, body),
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
            "🏀 You're IN!",
            "You've been selected to play! See you on the court.",
        )

    for wp in waitlist_players:
        pid = wp["player_id"]
        position = wp.get("position", "?")
        cursor = await db.execute(
            "SELECT notif_pref FROM players WHERE id = ?", (pid,)
        )
        row = await cursor.fetchone()
        if not row:
            continue
        await send_notification(
            db, pid, row["notif_pref"],
            "📋 Waitlisted",
            f"You're #{position} on the waitlist. We'll notify you if a spot opens up.",
        )


async def notify_waitlist_promotion(
    db: aiosqlite.Connection,
    game_id: int,
    player_id: int,
):
    """Notify a player they've been promoted from waitlist."""
    cursor = await db.execute(
        "SELECT notif_pref FROM players WHERE id = ?", (player_id,)
    )
    row = await cursor.fetchone()
    if not row:
        return
    await send_notification(
        db, player_id, row["notif_pref"],
        "🎉 Spot Opened — You're IN!",
        "A spot opened up and you've been moved in. See you on the court!",
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
    """Notify organizers that a player dropped from a game."""
    cursor = await db.execute(
        "SELECT id, notif_pref FROM players WHERE role = 'owner' AND status = 'approved'"
    )
    owners = await cursor.fetchall()
    for owner in owners:
        await send_notification(
            db, owner["id"], owner["notif_pref"],
            "⚠️ Player Drop",
            f"{player_name} dropped from game #{game_id} at {drop_time}.",
        )


async def notify_owners_new_signup(
    db: aiosqlite.Connection,
    player_name: str,
    player_email: str,
):
    """Notify organizers about a new player registration."""
    cursor = await db.execute(
        "SELECT id, notif_pref FROM players WHERE role = 'owner' AND status = 'approved'"
    )
    owners = await cursor.fetchall()
    for owner in owners:
        await send_notification(
            db, owner["id"], owner["notif_pref"],
            "👤 New Player Signup",
            f"{player_name} ({player_email}) has registered and needs approval.\n\n"
            f"Open the app to review and approve:\n"
            f"https://www.goatcommish.com",
        )


async def notify_game_cancelled(
    db: aiosqlite.Connection,
    game_id: int,
    player_ids: list[int],
    game_date: str,
    game_location: str,
):
    """Notify players that a game has been cancelled."""
    nice_date, weekday, _ = _format_game_date(game_date)
    subject = f"❌ Game Cancelled — {weekday}"

    for pid in player_ids:
        cursor = await db.execute(
            "SELECT notif_pref FROM players WHERE id = ?", (pid,)
        )
        row = await cursor.fetchone()
        if not row:
            continue
        notif_pref = row["notif_pref"]
        body = (
            f"The {weekday} game has been cancelled.\n\n"
            f"📍 {game_location}\n"
            f"🕐 {weekday}, {nice_date}\n\n"
            f"We'll let you know when the next game is scheduled."
        )
        await send_notification(db, pid, notif_pref, subject, body)


async def notify_game_edited(
    db: aiosqlite.Connection,
    game_id: int,
    player_ids: list[int],
    changes: str,
    new_date: str,
    new_location: str,
):
    """Notify players that a game has been updated."""
    nice_date, weekday, time_str = _format_game_date(new_date)
    subject = f"📝 Game Updated — {weekday}"

    for pid in player_ids:
        cursor = await db.execute(
            "SELECT notif_pref FROM players WHERE id = ?", (pid,)
        )
        row = await cursor.fetchone()
        if not row:
            continue
        notif_pref = row["notif_pref"]
        body = (
            f"A game you signed up for has been updated.\n\n"
            f"{changes}\n\n"
            f"📍 {new_location}\n"
            f"🕐 {nice_date}\n\n"
            f"Open the app to review:\n"
            f"https://www.goatcommish.com"
        )
        await send_notification(db, pid, notif_pref, subject, body)


# ═══════════════════════════════════════════════════════════════
# STARTUP DIAGNOSTICS
# ═══════════════════════════════════════════════════════════════

def log_notification_config():
    """Log which notification channels are configured."""
    email_ok = bool(SENDGRID_API_KEY and SENDGRID_FROM_EMAIL)
    sms_ok = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER)

    logger.info(
        f"📬 Notification config: "
        f"email={'✅ SendGrid (' + SENDGRID_FROM_EMAIL + ')' if email_ok else '❌ not configured'}, "
        f"sms={'✅ Twilio' if sms_ok else '❌ not configured'}"
    )
    if not email_ok:
        logger.info("   Set SENDGRID_API_KEY and SENDGRID_FROM_EMAIL to enable email")
    if not sms_ok:
        logger.info("   Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER to enable SMS")
