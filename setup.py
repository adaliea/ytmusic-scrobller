"""Interactive setup for YT Music auth and Last.fm session key (no password needed)."""

import os
import sys
import webbrowser

import pylast
from ytmusicapi import setup as ytmusic_setup


def setup_ytmusic():
    print("=== YT Music Authentication Setup ===")
    print()
    print("You need to provide request headers from an authenticated")
    print("YouTube Music session in your browser.")
    print()
    print("Steps:")
    print("  1. Open https://music.youtube.com in your browser")
    print("  2. Make sure you're logged in")
    print("  3. Open Developer Tools (F12) -> Network tab")
    print("  4. Click on any request to music.youtube.com")
    print("  5. Find the request headers and copy them")
    print()
    print("For detailed instructions, see:")
    print("  https://ytmusicapi.readthedocs.io/en/stable/setup/browser.html")
    print()

    config_dir = os.path.join(os.path.dirname(__file__), "config")
    os.makedirs(config_dir, exist_ok=True)
    output_path = os.path.join(config_dir, "browser.json")

    ytmusic_setup(filepath=output_path)
    print(f"\nSaved to {output_path}")


def setup_lastfm():
    print("\n=== Last.fm API Setup ===")
    print()
    print("Create an API account at: https://www.last.fm/api/account/create")
    print()

    api_key = input("Last.fm API Key: ").strip()
    api_secret = input("Last.fm API Secret: ").strip()

    # Web auth flow — no password needed
    network = pylast.LastFMNetwork(api_key=api_key, api_secret=api_secret)
    skg = pylast.SessionKeyGenerator(network)
    auth_url = skg.get_web_auth_url()

    print()
    print("Authorizing with Last.fm via your browser...")
    print(f"Opening: {auth_url}")
    print()
    webbrowser.open(auth_url)
    input("Press Enter after you've authorized the application in your browser...")

    session_key = skg.get_web_auth_session_key(auth_url)

    # Verify it works
    network = pylast.LastFMNetwork(
        api_key=api_key, api_secret=api_secret, session_key=session_key
    )
    username = network.get_authenticated_user().get_name()
    print(f"Authenticated as: {username}")

    poll_interval = input("Poll interval in seconds [300]: ").strip() or "300"
    history_limit = input("History items to fetch per poll [50]: ").strip() or "50"

    env_path = os.path.join(os.path.dirname(__file__), ".env")
    with open(env_path, "w") as f:
        f.write(f"LASTFM_API_KEY={api_key}\n")
        f.write(f"LASTFM_API_SECRET={api_secret}\n")
        f.write(f"LASTFM_SESSION_KEY={session_key}\n")
        f.write(f"POLL_INTERVAL={poll_interval}\n")
        f.write(f"HISTORY_LIMIT={history_limit}\n")

    print(f"\nSaved to {env_path}")


def main():
    if "--lastfm" in sys.argv:
        setup_lastfm()
    elif "--ytmusic" in sys.argv:
        setup_ytmusic()
    else:
        setup_ytmusic()
        setup_lastfm()

    print("\n=== Setup Complete ===")
    print("Run: docker compose up -d")


if __name__ == "__main__":
    main()
