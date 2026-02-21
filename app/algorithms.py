"""
Game selection algorithms.
"""
import random
import logging
import aiosqlite
from app.notifications import notify_selection_results

logger = logging.getLogger("hoops.algorithms")


async def run_random_selection(db: aiosqlite.Connection, game_id: int):
    """
    Execute the random selection algorithm for a game.

    When random_high_auto is True:
      1. Owner-added players → always in
      2. High priority players → guaranteed if slots remain
      3. Standard/low players → random for remaining slots

    When random_high_auto is False:
      1. Owner-added players → always in
      2. ALL remaining players → pure random selection
    """
    cursor = await db.execute("SELECT * FROM games WHERE id = ?", (game_id,))
    game = await cursor.fetchone()
    if not game:
        raise ValueError(f"Game {game_id} not found")

    cap = game["cap"] if game["cap_enabled"] else 999999
    available = cap
    high_auto = game["random_high_auto"]
    group_id = game["group_id"]

    cursor = await db.execute(
        """SELECT gs.id, gs.player_id, gs.owner_added,
                  COALESCE(gm.priority, 'standard') as priority
           FROM game_signups gs
           JOIN players p ON p.id = gs.player_id
           LEFT JOIN group_members gm ON gm.player_id = gs.player_id AND gm.group_id = ?
           WHERE gs.game_id = ?
           ORDER BY gs.signed_up_at ASC""",
        (group_id, game_id),
    )
    signups = await cursor.fetchall()

    in_players = []
    waitlist_players = []

    # 1. Owner-added players are automatically in
    owner_added = [s for s in signups if s["owner_added"]]
    for s in owner_added:
        await db.execute(
            "UPDATE game_signups SET status = 'in' WHERE id = ?", (s["id"],)
        )
        in_players.append(s["player_id"])
        available -= 1

    remaining = [s for s in signups if not s["owner_added"]]

    if high_auto:
        # 2. High priority players get guaranteed spots
        high_pri = [s for s in remaining if s["priority"] == "high"]
        others = [s for s in remaining if s["priority"] != "high"]

        if len(high_pri) <= available:
            for s in high_pri:
                await db.execute(
                    "UPDATE game_signups SET status = 'in' WHERE id = ?", (s["id"],)
                )
                in_players.append(s["player_id"])
                available -= 1
        else:
            random.shuffle(high_pri)
            for i, s in enumerate(high_pri):
                if i < available:
                    await db.execute(
                        "UPDATE game_signups SET status = 'in' WHERE id = ?", (s["id"],)
                    )
                    in_players.append(s["player_id"])
                else:
                    waitlist_players.append(s)
            available = 0
    else:
        # No priority advantage — all remaining go into random pool
        others = remaining

    # 3. Remaining players — pure random
    if available > 0 and others:
        random.shuffle(others)
        for i, s in enumerate(others):
            if i < available:
                await db.execute(
                    "UPDATE game_signups SET status = 'in' WHERE id = ?", (s["id"],)
                )
                in_players.append(s["player_id"])
            else:
                waitlist_players.append(s)
    elif others:
        random.shuffle(others)
        waitlist_players.extend(others)

    # Set waitlist status
    for s in waitlist_players:
        await db.execute(
            "UPDATE game_signups SET status = 'waitlist' WHERE id = ?", (s["id"],)
        )

    # Mark game as selection done
    await db.execute(
        "UPDATE games SET selection_done = 1, phase = 'active' WHERE id = ?",
        (game_id,),
    )
    await db.commit()

    # Notify all players of results
    waitlist_info = [
        {"player_id": s["player_id"], "position": i + 1}
        for i, s in enumerate(waitlist_players)
    ]
    await notify_selection_results(db, game_id, in_players, waitlist_info)

    logger.info(
        f"Game {game_id} selection: {len(in_players)} in, "
        f"{len(waitlist_players)} waitlisted"
    )

    return {
        "in_count": len(in_players),
        "waitlist_count": len(waitlist_players),
    }
