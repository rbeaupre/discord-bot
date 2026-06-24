# Discord Bot

A Discord bot for a private server with four features: sports trivia, weekly new music releases, monthly Pitchfork album reviews, and birthday announcements. All schedules and channels are configurable per-server by admins via slash commands.

---

## Features

| Feature | Default schedule | Default channel | On-demand command |
|---|---|---|---|
| Sports trivia | Every day at 4:00 PM ET | `#sports-chat` | `/trivia play` |
| New music releases | Every Monday at 10:00 AM ET | `#new_releases` | `/music releases` |
| Pitchfork album review | 1st of every month at 10:00 AM ET | `#album-reviews` | `/album post` |
| Birthday announcements | Every day at 9:00 AM ET | `#general` | — |

---

## How it works

### Boot sequence

1. `init_db()` creates any missing database tables on startup
2. The bot connects to Discord and loads the four feature cogs (trivia, music, birthdays, album reviews)
3. APScheduler starts inside the same asyncio event loop as the bot
4. `on_ready` fires in each cog — each one reads its config from the database and registers a scheduled job. If a guild has no config yet, default values are written automatically.

### Database

Two tables power the whole bot:

**`birthdays`** — one row per registered user per server. Stores Discord IDs, the birth month and day (as separate columns for efficient daily queries), and a snapshot of the member's display name.

**`schedule_configs`** — one row per feature per server. Stores the target channel ID, posting time (hour/minute/timezone), day of week for weekly features, and a JSON blob of content options (which sports or which genres to include; day of month for the album review). When an admin runs a config command, this row is updated and the APScheduler job is rescheduled immediately — no bot restart needed.

**`music_posts`** — one row per Spotify release posted per server. The bot queries this table before each weekly post to exclude artists that have appeared in the last 90 days, preventing the same acts from cycling back too quickly.

**`album_review_posts`** — one row per Pitchfork review posted per server. The Pitchfork URL is used as the deduplication key so the same album is never posted twice even if the monthly job fires more than once before Pitchfork publishes a new Best New Album.

### Content generation

**Trivia** — Claude (Haiku model) is prompted to return a JSON object containing the question, four answer options, the correct letter, and an explanation. The answer is posted inside a Discord spoiler tag (`||text||`) so members have to click to reveal it.

**Music** — The Spotify API is queried with `genre:"rock" year:2026` style searches (one per genre) to find recent releases. Claude then writes a short hype blurb for each result. Everything is posted as a single embed with one field per genre.

**Album review** — The bot scrapes Pitchfork's Best New Albums page using `requests` + `BeautifulSoup`. It tries to read structured data from Next.js's `__NEXT_DATA__` JSON blob first (more reliable than HTML class names that change with redesigns), then falls back to HTML selectors. Claude summarizes the review text into 3–4 sentences, and Spotify is searched to attach a "Listen" link. If the scraper fails, a plain-text alert is posted in the channel so the admin knows to investigate.

**Birthdays** — Purely database-driven. The daily job runs `WHERE birth_month = today AND birth_day = today` for the guild and posts an announcement embed for each match. Current server members get `@mentioned`; members who have left are referred to by their stored display name.

### Admin slash commands

All `/config` subcommands require Administrator permissions. Changes are live immediately.

| Command | Description |
|---|---|
| `/trivia play` | Post a trivia question right now |
| `/trivia config channel #ch` | Set the trivia channel |
| `/trivia config time 16:00` | Set the daily post time (24h ET) |
| `/trivia config sports` | Toggle which sports are included |
| `/music releases` | Post new releases right now |
| `/music config channel #ch` | Set the music channel |
| `/music config day mon` | Set which day of the week |
| `/music config time 10:00` | Set the post time (24h ET) |
| `/music config genres` | Toggle which genres are included |
| `/birthday set <month> <day>` | Register your own birthday |
| `/birthday remove` | Remove your birthday registration |
| `/birthday list` | See all registered birthdays in this server |
| `/birthday config channel #ch` | Set the announcement channel |
| `/birthday config time 09:00` | Set the daily check time (24h ET) |
| `/album post` | Post the latest Pitchfork Best New Album right now |
| `/album config channel #ch` | Set the album review channel |
| `/album config day 1` | Set which day of the month to post (1–31) |
| `/album config time 10:00` | Set the post time (24h ET) |

---

## Project structure

```
discord_bot/
├── bot.py                  Entry point — run this to start the bot
├── config.py               Loads and validates all environment variables
├── database/
│   ├── models.py           SQLAlchemy ORM models (Birthday, ScheduleConfig)
│   └── db.py               Engine + session factory, init_db()
├── utils/
│   ├── claude_client.py    Anthropic API wrapper (trivia, release blurbs, review summaries)
│   ├── spotify_client.py   Spotify API wrapper (new release search + album lookup)
│   └── pitchfork_client.py Pitchfork scraper (Best New Albums page)
├── cogs/
│   ├── trivia.py           Sports trivia cog
│   ├── music.py            Music releases cog
│   ├── birthdays.py        Birthday cog
│   └── album_reviews.py    Monthly Pitchfork album review cog
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

Fill in all five values in `.env`:

| Variable | Where to get it |
|---|---|
| `DISCORD_TOKEN` | Discord Developer Portal → Your App → Bot → Token |
| `DEV_GUILD_ID` | Right-click your server icon → Copy Server ID (requires Developer Mode) |
| `ANTHROPIC_API_KEY` | https://console.anthropic.com → API Keys |
| `SPOTIFY_CLIENT_ID` | https://developer.spotify.com/dashboard → Create App |
| `SPOTIFY_CLIENT_SECRET` | Same Spotify app dashboard |
| `DATABASE_URL` | Leave as `sqlite:///./discord_bot.db` for plain Python dev; `docker compose` overrides it automatically |

### 2. Discord Developer Portal setup

- **Bot token**: Application → Bot → Reset Token → copy
- **Privileged Intents**: Application → Bot → enable **Server Members Intent**
- **Invite the bot**: OAuth2 → URL Generator → scopes: `bot` + `applications.commands` → permissions: `Send Messages`, `Embed Links`, `Read Message History` → open the generated URL and add the bot to your server

### 3. Run locally

See the [Local development](#local-development) section below.

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

## Local dev workflows

### Example 1 — Adding a new birthday and verifying the announcement

This walks through registering a birthday and triggering the announcement manually to confirm it works without waiting until 9 AM.

**Step 1** — Start the bot (either method above) and confirm it's online in Discord.

**Step 2** — In your Discord server, run:
```
/birthday set 6 15
```
You should get an ephemeral reply: *"Birthday registered! I'll announce it on June 15."*

**Step 3** — Verify it was saved. Open a Python shell in the project:
```bash
source .venv/bin/activate
python3 -c "
from database.db import SessionLocal
from database.models import Birthday
with SessionLocal() as s:
    for b in s.query(Birthday).all():
        print(b)
"
```
You should see your birthday row printed.

**Step 4** — Trigger the announcement manually without waiting for 9 AM. In the Python shell:
```bash
python3 -c "
import asyncio, discord
from bot import DiscordBot
# Easier: just call _check_birthdays directly via the running bot's scheduler
# or test the embed by running the cog in isolation.
"
```
The easiest way during development is to temporarily set `DEV_GUILD_ID` in `.env`, which gives instant slash command sync so you can rapidly test config changes — then check `#general` after running `/birthday config time` to a time a minute from now.

---

### Example 2 — Changing the trivia schedule and testing it

This demonstrates how admin config commands interact with the scheduler.

**Step 1** — Start the bot. Open your Discord server and run:
```
/trivia config channel #sports-chat
/trivia config time 16:00
```
You should get confirmation messages. The APScheduler job for your guild is updated immediately in memory — no restart needed.

**Step 2** — Test on-demand to make sure the Claude integration is working:
```
/trivia play
```
A trivia embed should appear in the channel within a few seconds. If it fails, check the bot's terminal output for errors (Claude API key missing, rate limit, etc.).

**Step 3** — Verify the schedule was saved to the database:
```bash
source .venv/bin/activate
python3 -c "
from database.db import SessionLocal
from database.models import ScheduleConfig
with SessionLocal() as s:
    for cfg in s.query(ScheduleConfig).all():
        print(cfg)
"
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
   Look for all four cogs loading and "Bot ready" at the end.

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
- [ ] `/album post` returns an embed with album, score, summary, and links
