"""
Notification service with Gmail SMTP (email) and Twilio (SMS) integration.

Configure via environment variables:
  GMAIL_ADDRESS          - Gmail address to send from
  GMAIL_APP_PASSWORD     - Gmail App Password (NOT your regular password)
                           Generate at: https://myaccount.google.com/apppasswords
  TWILIO_ACCOUNT_SID     - Twilio Account SID
  TWILIO_AUTH_TOKEN      - Twilio Auth Token
  TWILIO_FROM_NUMBER     - Twilio phone number (e.g. +15551234567)

If credentials are missing, notifications are logged but not delivered.
"""
import os
import logging
import asyncio
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
from functools import partial

import aiosqlite

logger = logging.getLogger("hoops.notifications")

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
GMAIL_FROM_NAME = os.environ.get("GMAIL_FROM_NAME", "🏀 Hoops")

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
# EMAIL (Gmail SMTP)
# ═══════════════════════════════════════════════════════════════

def _send_email_sync(to_email: str, subject: str, body: str) -> bool:
    """Send an email via Gmail SMTP (blocking — run in executor)."""
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        logger.warning(f"📧 Gmail not configured — email to {to_email} logged only")
        return False

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{GMAIL_FROM_NAME} <{GMAIL_ADDRESS}>"
    msg["To"] = to_email
    msg["Subject"] = subject

    # Plain text
    msg.attach(MIMEText(body, "plain"))

    # HTML version
    html_body = body.replace("\n", "<br>")
    html = f"""\
    <div style="font-family:sans-serif;max-width:500px;margin:0 auto;padding:20px;">
        <h2 style="color:#ff6a2f;">{subject}</h2>
        <p style="color:#333;line-height:1.6;">{html_body}</p>
        <hr style="border:none;border-top:1px solid #eee;margin:20px 0;">
        <p style="color:#999;font-size:12px;">Sent by Hoops — Pickup Basketball Manager</p>
    </div>"""
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        logger.info(f"📧 Email sent to {to_email}: {subject}")
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error("📧 Gmail auth failed — check GMAIL_ADDRESS and GMAIL_APP_PASSWORD")
        return False
    except Exception as e:
        logger.error(f"📧 Gmail error sending to {to_email}: {e}")
        return False


async def send_email(to_email: str, subject: str, body: str) -> bool:
    """Send email asynchronously via Gmail SMTP."""
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
        # Default to US +1 if no country code
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
# UNIFIED NOTIFICATION DISPATCHER
# ═══════════════════════════════════════════════════════════════

async def send_notification(
    db: aiosqlite.Connection,
    player_id: int,
    channel: str,
    subject: str,
    body: str,
):
    """
    Send a notification to a player via their preferred channel.
    Always logs to the database. Delivers via SendGrid/Twilio if configured.
    """
    # Log to notification_log table (always)
    await db.execute(
        """INSERT INTO notification_log (recipient_id, channel, subject, body)
           VALUES (?, ?, ?, ?)""",
        (player_id, channel, subject, body),
    )
    await db.commit()

    # Look up player contact info
    cursor = await db.execute(
        "SELECT email, mobile FROM players WHERE id = ?", (player_id,)
    )
    player = await cursor.fetchone()
    if not player:
        logger.warning(f"Player {player_id} not found for notification")
        return

    delivered = False

    if channel == "email":
        if player["email"]:
            delivered = await send_email(player["email"], subject, body)
        else:
            logger.warning(f"Player {player_id} has no email address")

    elif channel == "sms":
        if player["mobile"]:
            delivered = await send_sms(player["mobile"], f"{subject}\n\n{body}")
        else:
            logger.warning(f"Player {player_id} has no mobile number")

    elif channel == "push":
        # Push notifications not yet implemented — fall back to email
        logger.info(f"🔔 Push not implemented, falling back to email for player {player_id}")
        if player["email"]:
            delivered = await send_email(player["email"], subject, body)

    else:
        logger.warning(f"Unknown channel '{channel}' for player {player_id}")

    if not delivered:
        logger.info(f"📋 Notification logged (not delivered) — player {player_id}: {subject}")


# ═══════════════════════════════════════════════════════════════
# HIGH-LEVEL NOTIFICATION FUNCTIONS
# (unchanged interface — used by the rest of the app)
# ═══════════════════════════════════════════════════════════════

async def notify_game_signup_open(
    db: aiosqlite.Connection,
    game_id: int,
    player_ids: list[int],
    game_date: str,
    game_location: str,
):
    """Notify a batch of players that signup is open for a game."""
    # Format the date nicely
    try:
        from datetime import datetime as dt
        d = dt.fromisoformat(game_date)
        nice_date = d.strftime("%A, %B %d at %I:%M %p")
    except Exception:
        nice_date = game_date

    for pid in player_ids:
        cursor = await db.execute(
            "SELECT notif_pref FROM players WHERE id = ?", (pid,)
        )
        row = await cursor.fetchone()
        if not row:
            continue
        channel = row["notif_pref"]
        subject = "🏀 Game Signup Open"
        body = (
            f"Signup is open for pickup basketball!\n\n"
            f"📍 {game_location}\n"
            f"🕐 {nice_date}\n\n"
            f"Open the app to sign up before spots fill."
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
            "🏀 You're In!",
            "Great news — you've been selected for the game!\n\nSee you on the court.",
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
            f"You're #{pos} on the waitlist.\n\nWe'll notify you right away if a spot opens up.",
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
        "🏀 You're In!",
        "A spot opened up and you've been moved into the game!\n\nSee you on the court.",
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
            f"{player_name} dropped from game #{game_id} at {drop_time}.",
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
            f"{player_name} ({player_email}) has registered and needs approval.\n\n"
            f"Open the app to review and approve.",
        )


async def notify_game_cancelled(
    db: aiosqlite.Connection,
    game_id: int,
    player_ids: list[int],
    game_date: str,
    game_location: str,
):
    """Notify all signed-up players that a game has been cancelled."""
    try:
        from datetime import datetime as dt
        d = dt.fromisoformat(game_date)
        weekday = d.strftime("%A")
        nice_date = d.strftime("%B %d")
    except Exception:
        weekday = ""
        nice_date = game_date

    subject = f"{weekday} game on {nice_date} at {game_location} has been cancelled"

    for pid in player_ids:
        cursor = await db.execute(
            "SELECT notif_pref FROM players WHERE id = ?", (pid,)
        )
        row = await cursor.fetchone()
        if not row:
            continue
        channel = row["notif_pref"]
        body = (
            f"The {weekday} game has been cancelled.\n\n"
            f"📍 {game_location}\n"
            f"🕐 {weekday}, {nice_date}\n\n"
            f"We'll let you know when the next game is scheduled."
        )
        await send_notification(db, pid, channel, subject, body)


# ═══════════════════════════════════════════════════════════════
# STARTUP DIAGNOSTICS
# ═══════════════════════════════════════════════════════════════

def log_notification_config():
    """Log which notification channels are configured."""
    email_ok = bool(GMAIL_ADDRESS and GMAIL_APP_PASSWORD)
    sms_ok = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER)

    logger.info(
        f"📬 Notification config: "
        f"email={'✅ Gmail' if email_ok else '❌ not configured'}, "
        f"sms={'✅ Twilio' if sms_ok else '❌ not configured'}"
    )
    if not email_ok:
        logger.info("   Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD to enable email")
    if not sms_ok:
        logger.info("   Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER to enable SMS")
