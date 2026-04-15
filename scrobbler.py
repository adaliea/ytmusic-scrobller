import hashlib
import json
import logging
import os
import re
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
LASTFM_GRACE_PERIOD = int(os.environ.get("LASTFM_GRACE_PERIOD", 5))
# Number of history items to store for sequence matching
HISTORY_ANCHOR_SIZE = 10


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
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


def clean_title(title):
    """Remove featured artist tags and other parenthetical noise from a title.

    Used both for scrobbling (so Last.fm gets a clean title) and for dedup matching.
    Examples:
        "Hope (feat. Winona Oak)" -> "Hope"
        "Don't Let Me Down (Illenium Remix) (feat. D..." -> "Don't Let Me Down (Illenium Remix)"
        "BANG BANG" -> "BANG BANG"
    """
    # Remove (feat. ...), (ft. ...), (with ...) — case insensitive
    title = re.sub(r"\s*\((?:feat|ft|with)\.?\s+[^)]*\)\.?", "", title, flags=re.IGNORECASE)
    # Also handle [feat. ...] brackets
    title = re.sub(r"\s*\[(?:feat|ft|with)\.?\s+[^]]*\]", "", title, flags=re.IGNORECASE)
    return title.strip()


def normalize_for_match(text):
    """Normalize a string for fuzzy comparison.

    Strips parenthetical suffixes, punctuation, and extra whitespace,
    then lowercases. Used only for dedup matching, not for scrobbling.
    """
    # Remove all parenthetical/bracket content
    text = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", text)
    # Remove punctuation
    text = re.sub(r"[^\w\s]", "", text)
    # Collapse whitespace and lowercase
    return re.sub(r"\s+", " ", text).strip().lower()


def split_artists(artist_string):
    """Split a combined artist string into individual artists.

    Handles: "A & B", "A, B, & C", "A feat. B", "A ft. B", "A x B"
    Returns the list of individual artist names.
    """
    # First split on feat./ft./with (these are secondary artists)
    primary = re.split(r"\s+(?:feat|ft|with)\.?\s+", artist_string, flags=re.IGNORECASE)[0]
    # Then split the primary part on &, comma, or "x" (as separator)
    artists = re.split(r"\s*(?:,\s*(?:&\s*)?|&\s*|\s+x\s+)", primary)
    return [a.strip() for a in artists if a.strip()]


def artists_match(yt_artist, lastfm_artist):
    """Check if a YT Music artist matches a Last.fm artist, accounting for
    multi-artist strings and normalization.

    The browser extension typically scrobbles only the primary artist,
    while YT Music may list "A & B" or "A, B, & C".
    """
    yt_norm = normalize_for_match(yt_artist)
    lastfm_norm = normalize_for_match(lastfm_artist)

    # Direct match
    if yt_norm == lastfm_norm:
        return True

    # Check if the Last.fm artist matches any individual YT artist
    yt_individuals = [normalize_for_match(a) for a in split_artists(yt_artist)]
    if lastfm_norm in yt_individuals:
        return True

    # Check if the first YT artist matches
    if yt_individuals and yt_individuals[0] == lastfm_norm:
        return True

    return False


def titles_match(yt_title, lastfm_title):
    """Fuzzy match titles, ignoring parenthetical differences like (feat. ...) or (Remix)."""
    # Try exact match first
    if yt_title.lower() == lastfm_title.lower():
        return True

    # Try with cleaned titles (feat. removed)
    if clean_title(yt_title).lower() == clean_title(lastfm_title).lower():
        return True

    # Try fully normalized (all parens removed, no punctuation)
    if normalize_for_match(yt_title) == normalize_for_match(lastfm_title):
        return True

    return False


def fetch_recent_lastfm(api_key, username, since_ts=None):
    """Fetch recent Last.fm scrobbles using the public GET API (no auth needed).

    If since_ts is provided, only returns scrobbles after that timestamp.
    """
    try:
        params = {
            "method": "user.getRecentTracks",
            "user": username,
            "api_key": api_key,
            "format": "json",
            "limit": 50,
        }
        if since_ts:
            params["from"] = since_ts
        resp = httpx.get(
            "https://ws.audioscrobbler.com/2.0/",
            params=params,
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
        track_artist = track.get("artist", {}).get("#text", "")
        track_title = track.get("name", "")
        if artists_match(artist, track_artist) and titles_match(title, track_title):
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
    """Extract artist, title, album, and liked status from a YT Music history item.

    Returns the primary artist only (first listed), and cleans featured artist
    tags from the title since Last.fm handles featured artists separately.
    """
    raw_title = item.get("title", "")
    artists = item.get("artists")
    if artists and len(artists) > 0:
        # Use only the first artist from the YT Music artists list
        artist = artists[0].get("name", "Unknown Artist")
    else:
        artist = "Unknown Artist"
    title = clean_title(raw_title)
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


def history_to_sequence(items):
    """Convert history items to a list of videoId strings for sequence matching."""
    return [item.get("videoId", "") for item in items]


def save_history_anchor(db, history):
    """Save the top N items as a JSON list of videoIds for sequence matching."""
    seq = history_to_sequence(history[:HISTORY_ANCHOR_SIZE])
    set_state(db, "history_anchor", json.dumps(seq))


def load_history_anchor(db):
    """Load the stored anchor sequence."""
    raw = get_state(db, "history_anchor")
    if raw:
        return json.loads(raw)
    return None


def find_new_items(history, anchor):
    """Find items in history that appeared since the last poll.

    Uses sequence matching against the stored anchor (top N videoIds from
    last poll). Finds where the anchor sequence starts in the current
    history by looking for a run of 2+ consecutive matching videoIds.
    This handles replays of the top song correctly — a single videoId
    match could be a replay, but 2+ consecutive matches means we found
    the real boundary.
    """
    if anchor is None:
        # First run — don't scrobble the entire history, just record state
        return []

    current_ids = history_to_sequence(history)

    # Try to find where the anchor sequence appears in current history.
    # Look for at least 2 consecutive matching IDs to avoid false matches
    # from replayed songs.
    min_match = min(2, len(anchor))

    for i in range(len(current_ids)):
        # Check if anchor starts at position i
        match_count = 0
        for j in range(min(len(anchor), len(current_ids) - i)):
            if current_ids[i + j] == anchor[j]:
                match_count += 1
            else:
                break

        if match_count >= min_match:
            log.debug(
                "Anchor matched at position %d (%d consecutive IDs).", i, match_count
            )
            return history[:i]

    # Try single-ID fallback: if the anchor's first ID appears and it's
    # not at position 0, use it. This handles cases where only 1 anchor
    # item remains in the history window.
    for i in range(1, len(current_ids)):
        if current_ids[i] == anchor[0]:
            log.debug("Anchor single-ID fallback matched at position %d.", i)
            return history[:i]

    # Anchor not found at all — history shifted too much. Skip this cycle.
    log.warning(
        "Anchor sequence not found in history. "
        "Skipping this cycle to avoid duplicates. "
        "Anchor: %s",
        anchor[:3],
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

    anchor = load_history_anchor(db)
    new_items = find_new_items(history, anchor)

    # Always update the anchor
    if history:
        save_history_anchor(db, history)

    if not new_items:
        log.info("No new tracks since last poll.")
        return

    log.info("Found %d new track(s) to process.", len(new_items))

    # Grace period: give the browser extension time to submit its scrobbles
    # before we check Last.fm for duplicates
    if LASTFM_GRACE_PERIOD > 0:
        log.debug("Waiting %ds for browser extension to submit scrobbles...", LASTFM_GRACE_PERIOD)
        time.sleep(LASTFM_GRACE_PERIOD)

    # Fetch only new Last.fm scrobbles since our last check
    lastfm_since = get_state(db, "lastfm_last_ts")
    lastfm_since_ts = int(lastfm_since) if lastfm_since else None
    recent_lastfm = fetch_recent_lastfm(api_key, username, since_ts=lastfm_since_ts)
    log.debug(
        "Fetched %d recent Last.fm scrobbles for dedup (since ts=%s).",
        len(recent_lastfm),
        lastfm_since_ts,
    )

    # Update the Last.fm timestamp watermark to the most recent scrobble
    if recent_lastfm:
        newest_ts = max(
            int(t.get("date", {}).get("uts", 0))
            for t in recent_lastfm
            if not t.get("@attr", {}).get("nowplaying")
        )
        if newest_ts:
            set_state(db, "lastfm_last_ts", str(newest_ts))

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

        # For dedup, use the raw title and a combined artist string so fuzzy
        # matching can compare against however the extension scrobbled it
        raw_title = item.get("title", "")
        all_artist_names = ", ".join(
            a.get("name", "") for a in (item.get("artists") or [])
        )
        dedup_artist = all_artist_names or artist

        # Check if the browser extension already scrobbled this
        if is_on_lastfm(recent_lastfm, dedup_artist, raw_title):
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
