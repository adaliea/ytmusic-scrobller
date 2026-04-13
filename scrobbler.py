import hashlib
import logging
import os
import sqlite3
import time

import httpx
import pylast
from ytmusicapi import YTMusic

log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("yt-scrobbler")

DB_PATH = os.environ.get("DB_PATH", "/data/scrobbled.db")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 300))
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", 50))


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS scrobbled (
            fingerprint TEXT PRIMARY KEY,
            title TEXT,
            artist TEXT,
            scrobbled_at INTEGER
        )"""
    )
    db.commit()
    return db


def get_state(db, key):
    row = db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_state(db, key, value):
    db.execute(
        "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value)
    )
    db.commit()


def history_fingerprint(items):
    """Create a fingerprint of the top of history to detect changes."""
    parts = []
    for item in items[:5]:
        vid = item.get("videoId", "")
        title = item.get("title", "")
        parts.append(f"{vid}:{title}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def item_fingerprint(item, position):
    """Fingerprint a single history item including its position context.

    We hash videoId + title + the videoIds of adjacent items to create a
    play-session-unique identifier. This allows the same song to appear
    multiple times in history (replays) while still deduplicating within
    the same poll window.
    """
    vid = item.get("videoId", "")
    title = item.get("title", "")
    return hashlib.sha256(f"{vid}:{title}:{position}".encode()).hexdigest()[:20]


def was_scrobbled(db, fp):
    row = db.execute(
        "SELECT 1 FROM scrobbled WHERE fingerprint = ?", (fp,)
    ).fetchone()
    return row is not None


def mark_scrobbled(db, fp, title, artist, ts):
    db.execute(
        "INSERT OR IGNORE INTO scrobbled (fingerprint, title, artist, scrobbled_at) VALUES (?, ?, ?, ?)",
        (fp, title, artist, ts),
    )
    db.commit()


def fetch_recent_lastfm(api_key, username):
    """Fetch recent Last.fm scrobbles using the public GET API (no auth needed)."""
    try:
        resp = httpx.get(
            "https://ws.audioscrobbler.com/2.0/",
            params={
                "method": "user.getRecentTracks",
                "user": username,
                "api_key": api_key,
                "format": "json",
                "limit": 50,
            },
            timeout=10,
        )
        log.debug("Last.fm getRecentTracks status=%d", resp.status_code)
        if resp.status_code != 200:
            log.warning(
                "Last.fm getRecentTracks failed: %d %s", resp.status_code, resp.text[:200]
            )
            return []
        data = resp.json()
        tracks = data.get("recenttracks", {}).get("track", [])
        return tracks
    except Exception as e:
        log.warning("Failed to fetch Last.fm recent tracks: %s", e)
        return []


def is_on_lastfm(recent_tracks, artist, title):
    """Check if this track was recently scrobbled on Last.fm (e.g. by browser extension)."""
    for track in recent_tracks:
        track_artist = track.get("artist", {}).get("#text", "").lower()
        track_title = track.get("name", "").lower()
        if track_artist == artist.lower() and track_title == title.lower():
            return True
    return False


def get_lastfm_network():
    api_key = os.environ["LASTFM_API_KEY"]
    api_secret = os.environ["LASTFM_API_SECRET"]
    session_key = os.environ["LASTFM_SESSION_KEY"]

    return pylast.LastFMNetwork(
        api_key=api_key,
        api_secret=api_secret,
        session_key=session_key,
    )


def extract_track_info(item):
    """Extract artist, title, album, and liked status from a YT Music history item."""
    title = item.get("title", "")
    artists = item.get("artists")
    if artists and len(artists) > 0:
        artist = artists[0].get("name", "Unknown Artist")
    else:
        artist = "Unknown Artist"
    album_info = item.get("album")
    album = album_info.get("name", "") if album_info else ""
    liked = item.get("likeStatus") == "LIKE"
    return artist, title, album, liked


def fetch_history(ytmusic):
    """Fetch recent YT Music history."""
    try:
        history = ytmusic.get_history()
        return history[:HISTORY_LIMIT] if history else []
    except Exception as e:
        log.error("Failed to fetch YT Music history: %s", e)
        return []


def find_new_items(history, last_top_video_id):
    """Find items in history that appeared since the last poll.

    YT Music history is ordered most-recent-first. We find where the
    previous top item now sits and return everything above it (the new plays).
    If the previous top item is gone (scrolled out), we return nothing to
    avoid mass-scrobbling old entries.
    """
    if last_top_video_id is None:
        # First run — don't scrobble the entire history, just record the top
        return []

    for i, item in enumerate(history):
        if item.get("videoId") == last_top_video_id:
            return history[:i]  # everything newer than the last-seen top

    # Previous top not found in current history window — too many new items
    # or history shifted significantly. Be conservative: skip this cycle.
    log.warning(
        "Previous top track (%s) not found in history. "
        "Skipping this cycle to avoid duplicates.",
        last_top_video_id,
    )
    return []


def poll_and_scrobble(ytmusic, network, username, api_key, db):
    log.info("Polling YT Music history...")
    history = fetch_history(ytmusic)

    if not history:
        log.info("No history items found.")
        return

    log.info("Fetched %d history items.", len(history))

    # Debug: log top 3 history items
    for i, item in enumerate(history[:3]):
        log.debug(
            "  history[%d]: videoId=%s title=%s artists=%s album=%s likeStatus=%s",
            i,
            item.get("videoId"),
            item.get("title"),
            [a.get("name") for a in (item.get("artists") or [])],
            (item.get("album") or {}).get("name"),
            item.get("likeStatus"),
        )

    last_top = get_state(db, "last_top_video_id")
    new_items = find_new_items(history, last_top)

    # Always update the top marker
    if history:
        set_state(db, "last_top_video_id", history[0].get("videoId", ""))

    if not new_items:
        log.info("No new tracks since last poll.")
        return

    log.info("Found %d new track(s) to process.", len(new_items))

    # Fetch Last.fm recent tracks once per cycle (public GET, no auth issues)
    recent_lastfm = fetch_recent_lastfm(api_key, username)
    log.debug("Fetched %d recent Last.fm scrobbles for dedup.", len(recent_lastfm))

    scrobbled_count = 0

    # Process oldest first
    for item in reversed(new_items):
        video_id = item.get("videoId")
        if not video_id:
            continue

        artist, title, album, liked = extract_track_info(item)
        if not title or artist == "Unknown Artist":
            log.debug("Skipping item with missing metadata: %s", video_id)
            continue

        # Check if the browser extension already scrobbled this
        if is_on_lastfm(recent_lastfm, artist, title):
            log.info("Already on Last.fm (likely from extension): %s - %s", artist, title)
            continue

        ts = int(time.time()) - (len(new_items) - scrobbled_count) * 60

        try:
            network.scrobble(
                artist=artist,
                title=title,
                timestamp=ts,
                album=album or None,
            )
            scrobbled_count += 1
            log.info("Scrobbled: %s - %s [%s] (liked=%s)", artist, title, album or "no album", liked)

            # Love the track on Last.fm if it's liked on YT Music
            if liked:
                try:
                    track = network.get_track(artist, title)
                    track.love()
                    log.info("Loved on Last.fm: %s - %s", artist, title)
                except Exception as e:
                    log.warning("Failed to love %s - %s: %s", artist, title, e)

        except Exception as e:
            log.error("Failed to scrobble %s - %s: %s", artist, title, e)

    log.info("Scrobbled %d new tracks this cycle.", scrobbled_count)


def main():
    log.info("Starting YT Music Scrobbler")
    log.info("Poll interval: %ds, History limit: %d", POLL_INTERVAL, HISTORY_LIMIT)

    api_key = os.environ["LASTFM_API_KEY"]

    ytmusic = YTMusic("/config/browser.json")
    network = get_lastfm_network()
    db = init_db()

    # Verify Last.fm auth and resolve username via signed API call
    # (pylast's user methods break when network.username is empty)
    try:
        session_key = os.environ["LASTFM_SESSION_KEY"]
        api_secret = os.environ["LASTFM_API_SECRET"]
        params = {
            "method": "user.getInfo",
            "api_key": api_key,
            "sk": session_key,
        }
        # Last.fm API signature: md5 of sorted params + secret
        sig_str = "".join(k + params[k] for k in sorted(params)) + api_secret
        params["api_sig"] = hashlib.md5(sig_str.encode()).hexdigest()
        params["format"] = "json"  # added after signing
        resp = httpx.post(
            "https://ws.audioscrobbler.com/2.0/",
            data=params,
            timeout=10,
        )
        resp.raise_for_status()
        username = resp.json()["user"]["name"]
        network.username = username
        log.info("Authenticated with Last.fm as: %s", username)
    except Exception as e:
        log.error("Last.fm authentication failed: %s", e)
        raise

    while True:
        try:
            poll_and_scrobble(ytmusic, network, username, api_key, db)
        except Exception as e:
            log.error("Error during poll cycle: %s", e)
        log.info("Sleeping %ds until next poll...", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
