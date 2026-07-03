"""
auth_spotify.py
───────────────
One-time script to authenticate with Spotify using OAuth and save the token
to a local cache file (.spotify_cache by default).

Run this locally ONCE, then copy the cache file to the VM. Spotipy will
auto-refresh the access token from the stored refresh token — you should
not need to run this again unless the token is revoked.

Usage
-----
  source .venv/bin/activate   # or activate your virtualenv
  python auth_spotify.py

What it does
------------
1. Opens the Spotify authorization page in your browser.
2. After you log in and click "Agree", Spotify redirects to localhost:8888.
   Spotipy catches the redirect automatically and exchanges the code for tokens.
3. Tokens are written to SPOTIFY_CACHE_PATH (default: .spotify_cache).

After running
-------------
Copy the cache file to the VM and restart the bot container:

  scp .spotify_cache USER@VM_IP:~/discord-bot/.spotify_cache

  # On the VM — if the container is already running with the volume mount:
  docker restart discord-bot

  # If starting fresh (include the -v flag so the cache is visible inside):
  docker run -d --restart=always \\
    --env-file .env \\
    -v ~/discord-bot/.spotify_cache:/app/.spotify_cache \\
    --name discord-bot discord-bot
"""

import sys

from spotipy.oauth2 import SpotifyOAuth

import config

print("Spotify OAuth setup")
print("═══════════════════")
print()
print(f"Redirect URI : {config.SPOTIFY_REDIRECT_URI}")
print(f"Cache path   : {config.SPOTIFY_CACHE_PATH}")
print()
print("Make sure this redirect URI is added to your Spotify app's")
print("'Redirect URIs' list in the Developer Dashboard:")
print("  https://developer.spotify.com/dashboard")
print()

sp_oauth = SpotifyOAuth(
    client_id=config.SPOTIFY_CLIENT_ID,
    client_secret=config.SPOTIFY_CLIENT_SECRET,
    redirect_uri=config.SPOTIFY_REDIRECT_URI,
    scope="playlist-read-private",
    cache_path=config.SPOTIFY_CACHE_PATH,
    open_browser=True,
)

try:
    # get_access_token() triggers the full OAuth flow if no cache exists:
    # opens a browser, starts a local HTTP server on port 8888 to catch
    # the redirect, exchanges the code for tokens, and writes the cache.
    token = sp_oauth.get_access_token(as_dict=False)
except Exception as exc:
    print(f"\nAuth failed: {exc}", file=sys.stderr)
    sys.exit(1)

print()
print(f"Auth successful! Token cached to: {config.SPOTIFY_CACHE_PATH}")
print()
print("Next steps:")
print(f"  scp {config.SPOTIFY_CACHE_PATH} USER@VM_IP:~/discord-bot/.spotify_cache")
print()
print("Then restart the bot on the VM with the volume mount (see file docstring).")
