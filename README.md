# Discord Bot

A Discord bot for a private server with nine features: sports trivia, weekly new music releases, monthly Pitchfork album reviews, birthday announcements, concert alerts, Criterion Collection movie night, live sports scores (with an optional ESPN Fantasy Football tie-in), and general chat via @mention. Schedules and channels for the first seven are configurable per-server by admins via slash commands; the fantasy tie-in has its own `/fantasy config` commands; general chat has no schedule or config and is always on.

---

## Features

| Feature | Default schedule | Default channel | On-demand command |
|---|---|---|---|
| Sports trivia | Every day at 4:00 PM ET | `#sports-chat` | `/trivia play` |
| New music releases | Every Monday at 10:00 AM ET | `#new_releases` | `/music releases` (admin) |
| Pitchfork album review | 1st of every month at 10:00 AM ET | `#album-reviews` | `/album post` (admin) |
| Birthday announcements | Every day at 9:00 AM ET | `#general` | — |
| Concert alerts | Every Monday at 9:00 AM ET | `#concert-alerts` | `/concert check` (admin) |
| Movie night | 1st of every month at 7:00 PM ET | `#movie-night` | `/movie pick` (admin) |
| Live sports scores | Continuous — playoffs (all sports) + NFL regular season | configurable | — |
| Fantasy football tie-in (optional) | Daily roster resync at 8:00 AM ET | n/a (enriches live score embeds) | `/fantasy refresh` (admin) |
| General chat | Always on — @mention the bot anywhere | any channel | @mention |

---

## How it works

### Boot sequence

1. `init_db()` creates any missing database tables on startup
2. The bot connects to Discord and loads all eight feature cogs
3. APScheduler starts inside the same asyncio event loop as the bot
4. `on_ready` fires in each cog — each one reads its config from the database and registers scheduled jobs. If a guild has no config yet, default values are written automatically.

### Database

**`birthdays`** — one row per registered user per server. Stores Discord IDs, the birth month and day (as separate columns for efficient daily queries), and a snapshot of the member's display name.

**`schedule_configs`** — one row per feature per server. Stores the target channel ID, posting time (hour/minute/timezone), day of week for weekly features, and a JSON blob of content options (sports list, genres, cities, day of month, enabled sports, etc.). When an admin runs a config command, this row is updated and the APScheduler job is rescheduled immediately — no bot restart needed.

**`music_posts`** — one row per Spotify release posted per server. The bot queries this table before each weekly post to exclude artists that have appeared in the last 90 days, preventing the same acts from cycling back too quickly.

**`album_review_posts`** — one row per Pitchfork review posted per server. The Pitchfork URL is used as the deduplication key so the same album is never posted twice even if the monthly job fires more than once before Pitchfork publishes a new Best New Album.

**`artist_watchlist`** — per-guild list of artists to monitor for concert alerts. Populate it using `/concert import <playlist_url>` with a public Spotify playlist, or add artists one at a time with `/concert add`. New artists from the weekly music releases post are also auto-added with source="new_releases".

**`concert_alerts_posted`** — one row per Ticketmaster event per server. Used to prevent the same show from being posted twice across consecutive weekly checks.

**`criterion_films`** — global catalog of Criterion Collection films fetched from TMDB (shared across all guilds). Populated by `/movie setup`. Stores title, year, director, overview, and poster URL.

**`movie_night_picks`** — per-guild record of which Criterion films have been featured, so the monthly rotation doesn't repeat a film until all films in the catalog have been shown (then resets automatically).

**`live_game_states`** — per-guild tracking state for each in-progress or recently finished playoff game. Stores the current score, the index of the last scoring play reported, and whether the game-start embed has been posted. Used to make ESPN polling idempotent across bot restarts.

**`fantasy_league_configs`** — one row per guild storing its ESPN Fantasy Football league ID, season, and session cookies (`espn_s2`, `SWID`). Cookies are entered through a Discord modal via `/fantasy config cookies`, never a command argument — see "Setting up the fantasy football tie-in" below for why.

**`fantasy_roster_entries`** — per-guild cache of which fantasy manager owns which NFL player, refreshed daily from ESPN and replaced wholesale each time (ESPN doesn't expose an incremental roster-change feed). Matched to live scoring plays by ESPN's universal athlete ID, not by name.

### Content generation

**Trivia** — Claude (Haiku model) is prompted to return a JSON object containing the question, four answer options, the correct letter, and an explanation. The answer is posted inside a Discord spoiler tag (`||text||`) so members have to click to reveal it.

**Music** — The Spotify API is queried with `genre:"rock" year:2026` style searches (one per genre) to find recent releases. Claude then writes a short hype blurb for each result. Everything is posted as a single embed with one field per genre. Artists are also auto-added to the concert watchlist.

**Album review** — The bot scrapes Pitchfork's Best New Albums page using `requests` + `BeautifulSoup`. It tries to read structured data from Next.js's `__NEXT_DATA__` JSON blob first (more reliable than HTML class names that change with redesigns), then falls back to HTML selectors. Claude summarizes the review text into 3–4 sentences, and Spotify is searched to attach a "Listen on Spotify" link. Note: Pitchfork renders scores client-side via JavaScript so scores are not included.

**Birthdays** — Purely database-driven. The daily job runs `WHERE birth_month = today AND birth_day = today` for the guild and posts an announcement embed for each match.

**Concert alerts** — Ticketmaster's Discovery API is queried for all music events in the configured cities (default: Toronto and Montreal) for the next 90 days. Results are matched against the guild's artist watchlist (case-insensitive). Matched events not already in `concert_alerts_posted` get an embed with date, venue, city, and a ticket link.

**Movie night** — A random unwatched film is picked from the `criterion_films` table (those not already in `movie_night_picks` for this guild). Claude writes a short enthusiastic pitch using the TMDB overview. An embed with the poster image and director byline is posted. When all films have been picked, the guild's history resets and the rotation starts over.

**Live sports scores** — ESPN's public scoreboard API is polled independently per sport. NFL fires every 15 seconds (to catch touchdown + PAT as separate events); NHL, MLB, and soccer fire every 60 seconds. NHL, MLB, and soccer track playoff/tournament games only (NFL/NHL/MLB: postseason type; soccer: FIFA Men's World Cup). NFL also tracks the regular season — one game per day: the day's only game, or whichever has the latest kickoff ("primetime") when several are on. A "game starting" embed is posted when a game is first detected as active, a scoring play embed is posted for each new play since the last poll, and a final score embed (labeled "Playoff" or "Regular Season") is posted when the game ends. NFL scoring plays are attributed to the player who scored — name plus a headshot thumbnail, pulled from ESPN's per-game summary endpoint (the scoreboard endpoint alone never says who scored for this sport). Every embed also shows both teams' logos (home team as the small author icon, away team as the thumbnail — or a scorer's headshot in that thumbnail slot instead, when there is one).

**Fantasy football tie-in (optional)** — When a scoring NFL player is on a manager's roster in your connected ESPN Fantasy Football league, the scoring play embed leads with the manager's name instead of just the player's, e.g. "Alice's player, Kyren Williams, scores!" Roster data is refreshed daily and matched to live scoring plays by ESPN's universal athlete ID (shared between the site API and the Fantasy API), not by name — sidesteps mismatches from suffixes ("Jr.", "Sr.") or punctuation. See "Setting up the fantasy football tie-in" below to enable it.

**General chat** — @mention the bot anywhere and Claude (Haiku model) replies conversationally. Only the single message text is sent to Claude — no channel history, no DB rows, no tools/function-calling — so there's structurally nothing sensitive for a prompt injection to leak.

### Admin slash commands

All `/config` subcommands require Manage Server permissions. Changes are live immediately.

| Command | Description |
|---|---|
| `/trivia play` | Post a trivia question right now |
| `/trivia config channel #ch` | Set the trivia channel |
| `/trivia config time 16:00` | Set the daily post time (24h ET) |
| `/trivia config sports` | Toggle which sports are included |
| `/music releases` | Post new releases right now (admin) |
| `/music config channel #ch` | Set the music channel |
| `/music config day mon` | Set which day of the week |
| `/music config time 10:00` | Set the post time (24h ET) |
| `/music config genres` | Toggle which genres are included |
| `/birthday set <month> <day>` | Register your own birthday |
| `/birthday remove` | Remove your birthday registration |
| `/birthday list` | See all registered birthdays in this server |
| `/birthday config channel #ch` | Set the announcement channel |
| `/birthday config time 09:00` | Set the daily check time (24h ET) |
| `/album post` | Post the latest Pitchfork Best New Album right now (admin) |
| `/album config channel #ch` | Set the album review channel |
| `/album config day 1` | Set which day of the month to post (1–31) |
| `/album config time 10:00` | Set the post time (24h ET) |
| `/concert add <artist>` | Add an artist to the concert watchlist (admin) |
| `/concert remove <artist>` | Remove an artist from the concert watchlist (admin) |
| `/concert list` | Show the first 20 watchlist artists |
| `/concert check` | Run the concert check right now (admin) |
| `/concert config channel #ch` | Set the concert alerts channel (admin) |
| `/concert config day mon` | Set the check day of week (admin) |
| `/concert config time 09:00` | Set the check time (24h ET) (admin) |
| `/movie pick` | Post a random Criterion film right now (admin) |
| `/movie setup` | Load or refresh the Criterion catalog from TMDB (admin) |
| `/movie config channel #ch` | Set the movie night channel (admin) |
| `/movie config day 1` | Set which day of the month (admin) |
| `/movie config time 19:00` | Set the post time (24h ET) (admin) |
| `/scores status` | Show current score alert config |
| `/scores config channel #ch` | Set the score alerts channel (admin) |
| `/scores config sports` | Toggle individual sports on/off (admin) |
| `/scores config enable` | Enable live score alerts |
| `/scores config disable` | Disable live score alerts |
| `/fantasy status` | Show fantasy league config, cookie health, cached roster size |
| `/fantasy refresh` | Refresh the fantasy roster cache from ESPN right now (admin) |
| `/fantasy config league <id> [season]` | Set the ESPN Fantasy league ID (admin) |
| `/fantasy config cookies` | Open a private form to set `espn_s2`/`SWID` (admin) |
| `/fantasy config channel #ch` | Set the cookie-expiration alert channel (admin) |
| `/fantasy config time 08:00` | Set the daily roster refresh time (24h ET) (admin) |

---

## Project structure

```
discord_bot/
├── bot.py                  Entry point — run this to start the bot
├── config.py               Loads and validates all environment variables
├── database/
│   ├── models.py           SQLAlchemy ORM models (11 tables)
│   └── db.py               Engine + session factory, init_db()
├── utils/
│   ├── claude_client.py    Anthropic API wrapper (trivia, music blurbs, review summaries, movie pitches, chat replies)
│   ├── spotify_client.py   Spotify API wrapper (new release search + album lookup)
│   ├── pitchfork_client.py Pitchfork scraper (Best New Albums page)
│   ├── ticketmaster_client.py  Ticketmaster Discovery API wrapper (upcoming events by city)
│   ├── tmdb_client.py      TMDB API wrapper (Criterion Collection catalog)
│   ├── sports_client.py    ESPN public API wrapper (live scoreboards + player-attributed NFL play-by-play)
│   └── espn_fantasy_client.py  ESPN Fantasy Football API wrapper (private league rosters)
├── cogs/
│   ├── trivia.py           Sports trivia cog
│   ├── music.py            Music releases cog
│   ├── birthdays.py        Birthday cog
│   ├── album_reviews.py    Monthly Pitchfork album review cog
│   ├── concerts.py         Weekly concert alerts cog
│   ├── movies.py           Monthly Criterion movie night cog
│   ├── sports_scores.py    Live sports score cog (playoffs + NFL regular season)
│   ├── fantasy.py          Fantasy football tie-in cog (daily roster resync)
│   └── chat.py             General chat cog (@mention listener, no schedule/config)
├── Dockerfile              Production container image
├── docker-compose.yml      Local dev — runs Postgres + bot together
└── .env.example            Template for all required secrets
```

---

## Setup

### 1. Clone and create your `.env`

```bash
git clone https://github.com/rbeaupre/discord-bot.git
cd discord-bot
cp .env.example .env
```

Fill in all values in `.env`:

| Variable | Where to get it |
|---|---|
| `DISCORD_TOKEN` | Discord Developer Portal → Your App → Bot → Token |
| `DEV_GUILD_ID` | Right-click your server icon → Copy Server ID (requires Developer Mode) |
| `ANTHROPIC_API_KEY` | https://console.anthropic.com → API Keys |
| `SPOTIFY_CLIENT_ID` | https://developer.spotify.com/dashboard → Create App |
| `SPOTIFY_CLIENT_SECRET` | Same Spotify app dashboard |
| `TICKETMASTER_API_KEY` | https://developer.ticketmaster.com → My Apps → Consumer Key |
| `TMDB_API_KEY` | https://www.themoviedb.org/settings/api → API Read Access Token (v3) |
| `DATABASE_URL` | Leave as `sqlite:///./discord_bot.db` for plain Python dev; `docker compose` overrides it automatically |

### 2. Discord Developer Portal setup

- **Bot token**: Application → Bot → Reset Token → copy
- **Privileged Intents**: Application → Bot → enable **Server Members Intent**
- **Invite the bot**: OAuth2 → URL Generator → scopes: `bot` + `applications.commands` → permissions: `Send Messages`, `Embed Links`, `Read Message History` → open the generated URL and add the bot to your server

### 3. Run locally

See the [Local development](#local-development) section below.

---

## Setting up the fantasy football tie-in

This is optional — the rest of the bot works fine without it. It links live NFL scoring alerts to a private ESPN Fantasy Football league so a scoring embed can call out "Alice's player, Kyren Williams, scores!" instead of just the player's name.

There's no env var for this — everything is per-server and stored in the database, set up entirely through Discord commands.

### Initial setup

1. **Find your league ID.** Open your league on fantasy.espn.com and copy the numeric `leagueId` from the URL, e.g. `...leagues/1234567?...` → `1234567`.
2. **Set it**: `/fantasy config league <league_id>` (add a `season` argument only if you need a season other than the current one).
3. **Check if your league is private** (most home leagues are — this determines whether you need cookies at all):
   ```bash
   curl -H "User-Agent: Mozilla/5.0" \
     "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2026/segments/0/leagues/<league_id>"
   ```
   - `{"messages":["You are not authorized to view this League."]}` → private, continue to step 4.
   - A JSON blob with team/league data → public, skip to step 6.
4. **Grab your session cookies.** Log into fantasy.espn.com in a browser, then open DevTools (`Cmd+Option+I` on Mac, `F12` on Windows) → **Application** tab (Chrome) or **Storage** tab (Firefox) → **Cookies** → `https://fantasy.espn.com`. Copy the values of:
   - `espn_s2` — a long opaque string
   - `SWID` — looks like `{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}`, **including** the curly braces
5. **Set them**: run `/fantasy config cookies` in Discord. This opens a private form (a Discord modal), not command arguments — paste `espn_s2` and `SWID` into the two fields and submit. This is deliberate: Discord shows slash command *argument values* in the channel as part of the "X used /command" message even when the bot's reply is ephemeral, which would leak a session credential to everyone in the channel. The modal has no such public echo — treat these values like a password, not a config setting.
6. **Sync now**: `/fantasy refresh` — it reports how many players got cached, or a clear error if something's wrong.
7. *(Optional)* `/fantasy config channel #ch` to pick where cookie-expiration alerts get posted, and `/fantasy config time HH:MM` to change the daily refresh time (default 8:00 AM ET).

### When it stops working (cookie expiration)

ESPN's `espn_s2`/`SWID` cookies have no published expiry and can stop working at any time — this is normal, expected maintenance, not a bug. When it happens, every refresh gets a 401 and the bot posts a one-time alert to your configured fantasy channel. To fix it: repeat steps 4–6 above (grab fresh cookies, paste into `/fantasy config cookies`, run `/fantasy refresh`). No VM access, redeploy, or code change is ever needed.

This can't be fully automated end-to-end: ESPN's actual login goes through Disney's identity service rather than a documented public API, so a scripted login is fragile and tends to break silently whenever ESPN changes that flow. A headless-browser approach could technically drive a real login, but it would require storing your ESPN **account password** — a bigger secret than the two session cookies — for something that only needs doing a few times a year. Two minutes of copy-pasting was judged the better trade-off.

---

## Local development

There are two ways to run the bot locally depending on what you're working on.

### Option A — Plain Python with SQLite (fastest for quick iteration)

No Docker needed. Good for testing slash commands or logic changes where you don't care about Postgres-specific behaviour.

```bash
# Create and activate the virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Make sure DATABASE_URL in .env is set to the SQLite default:
#   DATABASE_URL=sqlite:///./discord_bot.db

python bot.py
```

The bot will create `discord_bot.db` in the project directory on first run.

### Option B — Docker Compose with Postgres (mirrors production)

Runs a real PostgreSQL container alongside the bot. Use this when testing anything DB-related (new columns, query behaviour, etc.) or before deploying.

```bash
docker compose up --build
```

The compose file automatically sets `DATABASE_URL` to point at the Postgres container, overriding whatever is in your `.env`. Data persists in a Docker volume between restarts (`docker compose down -v` wipes it).

To run in the background:
```bash
docker compose up --build -d

# Tail the bot's logs
docker compose logs -f bot

# Stop everything
docker compose down
```

---

## Deployment (GCP)

### Infrastructure

| Component | GCP service | Est. cost |
|---|---|---|
| Bot process | Compute Engine e2-micro (us-central1) | Free tier |
| Database | Cloud SQL PostgreSQL db-f1-micro (us-central1) | ~$9/month |

Both resources are on the **default VPC network** in **us-central1**. Cloud SQL has Private IP enabled; the bot connects to it via private IP — no public exposure needed.

### Deploying an update

SSH into the VM, then:

```bash
cd discord-bot
git pull
docker stop discord-bot && docker rm discord-bot
docker build -t discord-bot .
docker run -d --restart=always --env-file .env --name discord-bot discord-bot
docker logs -f discord-bot
```

### First-time setup steps

1. **Create a Cloud SQL instance** — PostgreSQL, `db-f1-micro`, us-central1, default VPC. Enable Private IP. Create a database named `discord_bot` and a user with a strong password. Note the private IP.

2. **Create a Compute Engine VM** — e2-micro, Debian 12, us-central1, default VPC. Install Docker:
   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER
   # Log out and back in for the group change to take effect
   ```

3. **Clone the repo and create `.env`** on the VM:
   ```bash
   git clone https://github.com/rbeaupre/discord-bot.git
   cd discord-bot
   cp .env.example .env
   nano .env
   ```
   Set `DATABASE_URL` to:
   ```
   postgresql://USER:PASSWORD@CLOUD_SQL_PRIVATE_IP:5432/discord_bot
   ```
   Leave `DEV_GUILD_ID` blank for global slash command sync.

4. **Build and run**:
   ```bash
   docker build -t discord-bot .
   docker run -d --restart=always --env-file .env --name discord-bot discord-bot
   ```
   `--restart=always` ensures the bot restarts automatically after VM reboots. Tables are created automatically on first run — no DDL needed.

5. **Check logs**:
   ```bash
   docker logs -f discord-bot
   ```
   Look for all seven cogs loading and "Bot ready" at the end.

6. **Initialize the Criterion catalog** — run `/movie setup` once in any Discord server after the bot is live. This fetches all Criterion films from TMDB and stores them in the database. Takes a few minutes on the first run.

---

## Common VM operations

```bash
# View live logs
docker logs -f discord-bot

# View last 100 lines
docker logs --tail 100 discord-bot

# Check if the container is running
docker ps

# Stop the bot
docker stop discord-bot

# Start a stopped container
docker start discord-bot

# Edit secrets (e.g. rotate a key or toggle DEV_GUILD_ID)
nano ~/discord-bot/.env
docker stop discord-bot && docker rm discord-bot
docker run -d --restart=always --env-file .env --name discord-bot discord-bot
```

---

## Implementation checklist

### Discord Developer Portal
- [ ] Create application at https://discord.com/developers/applications
- [ ] Bot → Add Bot → copy Token → set as `DISCORD_TOKEN`
- [ ] Bot → Privileged Gateway Intents → enable **Server Members Intent**
- [ ] OAuth2 → URL Generator → scopes: `bot` + `applications.commands`, permissions: `Send Messages` + `Embed Links` + `Read Message History` → invite bot to server
- [ ] (Dev) Copy server ID into `DEV_GUILD_ID` for instant slash command sync

### Anthropic
- [ ] Create API key at https://console.anthropic.com → set as `ANTHROPIC_API_KEY`
- [ ] Confirm account has credits (Haiku is very cheap — trivia calls ~$0.0001 each)

### Spotify
- [ ] Create app at https://developer.spotify.com/dashboard (set redirect URI to `http://localhost`)
- [ ] Copy Client ID → `SPOTIFY_CLIENT_ID`
- [ ] Copy Client Secret → `SPOTIFY_CLIENT_SECRET`

### Ticketmaster
- [ ] Create account and app at https://developer.ticketmaster.com
- [ ] Copy **Consumer Key** (not Consumer Secret) → `TICKETMASTER_API_KEY`

### TMDB
- [ ] Create account at https://www.themoviedb.org
- [ ] Settings → API → request a v3 API key → `TMDB_API_KEY`
- [ ] After bot is live, run `/movie setup` once to populate the Criterion catalog

### GCP — Cloud SQL
- [ ] Create PostgreSQL instance (`db-f1-micro`)
- [ ] Create database `discord_bot` and a user with a strong password
- [ ] Note the private IP for `DATABASE_URL`

### GCP — Compute Engine
- [ ] Create e2-micro VM in same region/VPC as Cloud SQL
- [ ] Install Docker on the VM
- [ ] Clone repo, write production `.env`, build image, run container with `--restart=always`

### Local smoke test (before deploying)
- [ ] Copy `.env.example` → `.env`, fill in all values
- [ ] `docker compose up --build` — both containers start cleanly
- [ ] Slash commands appear in Discord server
- [ ] `/trivia play` returns an embed
- [ ] `/music releases` returns an embed
- [ ] `/birthday set 1 1` confirms registration
- [ ] `/album post` returns an embed with album cover, summary, and Spotify link
- [ ] `/concert check` posts alerts or reports "no new shows found"
- [ ] `/movie setup` completes without error, then `/movie pick` posts a film
- [ ] `/scores config enable` + `/scores config channel #ch` — verify setup (score alerts only fire during active playoffs)
- [ ] *(Optional)* Fantasy tie-in: `/fantasy config league`, `/fantasy config cookies`, `/fantasy refresh` — reports a cached player count
