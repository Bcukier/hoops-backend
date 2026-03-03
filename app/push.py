"""
Apple Push Notification service (APNs) integration.

Configure via environment variables:
  APNS_KEY_ID        - Key ID from Apple Developer portal
  APNS_TEAM_ID       - Your Apple Developer Team ID
  APNS_KEY_PATH      - Path to .p8 private key file
  APNS_BUNDLE_ID     - App bundle identifier
  APNS_USE_SANDBOX   - Set to 1 for development/sandbox APNs server
"""
import os, time, logging, json, asyncio
from functools import partial

logger = logging.getLogger("hoops.push")

APNS_KEY_ID = os.environ.get("APNS_KEY_ID", "")
APNS_TEAM_ID = os.environ.get("APNS_TEAM_ID", "")
APNS_KEY_PATH = os.environ.get("APNS_KEY_PATH", "")
APNS_BUNDLE_ID = os.environ.get("APNS_BUNDLE_ID", "com.goatcommish.app")
APNS_USE_SANDBOX = os.environ.get("APNS_USE_SANDBOX", "1") == "1"

APNS_HOST_PROD = "https://api.push.apple.com"
APNS_HOST_SANDBOX = "https://api.sandbox.push.apple.com"

_cached_token = None
_cached_token_time = 0


def _get_apns_host():
    return APNS_HOST_SANDBOX if APNS_USE_SANDBOX else APNS_HOST_PROD


def _generate_apns_token():
    global _cached_token, _cached_token_time
    now = int(time.time())
    if _cached_token and (now - _cached_token_time) < 3000:
        return _cached_token
    try:
        import jwt
    except ImportError:
        logger.error("PyJWT not installed")
        return None
    if not APNS_KEY_ID or not APNS_TEAM_ID or not APNS_KEY_PATH:
        logger.warning("APNs not configured")
        return None
    try:
        with open(APNS_KEY_PATH, "r") as f:
            private_key = f.read()
    except FileNotFoundError:
        logger.error(f"APNs key file not found: {APNS_KEY_PATH}")
        return None
    headers = {"alg": "ES256", "kid": APNS_KEY_ID}
    payload = {"iss": APNS_TEAM_ID, "iat": now}
    token = jwt.encode(payload, private_key, algorithm="ES256", headers=headers)
    _cached_token = token
    _cached_token_time = now
    return token


def _send_push_sync(device_token, title, body, badge=None, data=None):
    token = _generate_apns_token()
    if not token:
        return False
    try:
        import httpx
    except ImportError:
        logger.error("httpx not installed")
        return False
    host = _get_apns_host()
    url = f"{host}/3/device/{device_token}"
    headers = {
        "authorization": f"bearer {token}",
        "apns-topic": APNS_BUNDLE_ID,
        "apns-push-type": "alert",
        "apns-priority": "10",
    }
    apns_payload = {
        "aps": {
            "alert": {"title": title, "body": body},
            "sound": "default",
        }
    }
    if badge is not None:
        apns_payload["aps"]["badge"] = badge
    if data:
        apns_payload.update(data)
    try:
        with httpx.Client(http2=True, timeout=10) as client:
            response = client.post(url, headers=headers, content=json.dumps(apns_payload))
            if response.status_code == 200:
                logger.info(f"Push sent to {device_token[:12]}...")
                return True
            elif response.status_code == 410:
                logger.info(f"Push token {device_token[:12]}... inactive")
                return False
            else:
                logger.warning(f"APNs error {response.status_code}: {response.text}")
                return False
    except Exception as e:
        logger.error(f"Push send failed: {e}")
        return False


async def send_push(device_token, title, body, badge=None, data=None):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, partial(_send_push_sync, device_token, title, body, badge, data)
    )


async def send_push_to_player(db, player_id, title, body, badge=None, data=None):
    cursor = await db.execute(
        "SELECT token, platform FROM push_tokens WHERE player_id = ?",
        (player_id,)
    )
    tokens = await cursor.fetchall()
    if not tokens:
        return False
    any_delivered = False
    stale_tokens = []
    for row in tokens:
        device_token = row["token"]
        delivered = await send_push(device_token, title, body, badge, data)
        if delivered:
            any_delivered = True
        else:
            stale_tokens.append(device_token)
    for stale in stale_tokens:
        await db.execute(
            "DELETE FROM push_tokens WHERE player_id=? AND token=?",
            (player_id, stale)
        )
    if stale_tokens:
        await db.commit()
        logger.info(f"Removed {len(stale_tokens)} stale push tokens for player {player_id}")
    return any_delivered
