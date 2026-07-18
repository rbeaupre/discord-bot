"""
database/models.py
──────────────────
SQLAlchemy ORM models. Each class maps to one database table.

Tables
------
birthdays             — One row per registered user birthday per guild.
schedule_configs      — One row per (guild, feature) pair storing the channel,
                        posting schedule, and content options for each bot feature.
music_posts           — One row per release posted by the music feature. Used to
                        prevent the same artist from being posted repeatedly.
album_review_posts    — One row per album review posted by the album review
                        feature. Used to prevent the same album from being
                        re-posted if the monthly job fires more than once.
artist_watchlist      — Per-guild list of artists to monitor for concert alerts.
concert_alerts_posted — Records each concert alert posted per guild, used to
                        prevent the same event from being announced twice.
criterion_films       — Global catalog of Criterion Collection films fetched
                        from TMDB. Shared across all guilds.
movie_night_picks     — Per-guild record of which Criterion films have been
                        featured, so the rotation doesn't repeat films until
                        all have been shown.
live_game_states      — Per-guild tracking state for each in-progress playoff
                        game. Stores current scores and the index of the last
                        scoring play reported so polls are idempotent.
trivia_question_posts — Per-guild history of trivia questions Claude has
                        generated, tagged with the date-derived era they were
                        written for. Recent questions in the same era are fed
                        back into the generation prompt as an avoid-list so
                        the rotation doesn't keep landing on the same
                        "greatest hits" fact every time that era comes back
                        around.
"""

import json

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase
from datetime import datetime


class Base(DeclarativeBase):
    """Shared base class that all ORM models must inherit from."""
    pass


class Birthday(Base):
    """
    Stores a Discord server member's birthday.

    guild_id and user_id are Discord "snowflake" IDs — 64-bit integers that
    require BigInteger rather than plain Integer to avoid overflow in PostgreSQL.

    The month and day are stored separately so the database can efficiently
    answer "who has a birthday today?" with a simple WHERE clause.
    """

    __tablename__ = "birthdays"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # The Discord server (guild) this birthday belongs to.
    guild_id = Column(BigInteger, nullable=False, index=True)

    # The Discord user who registered the birthday.
    user_id = Column(BigInteger, nullable=False)

    # Calendar month (1 = January … 12 = December).
    birth_month = Column(Integer, nullable=False)

    # Calendar day of month (1–31).
    birth_day = Column(Integer, nullable=False)

    # Display name snapshotted at registration time. Used in announcements if the
    # member later changes their username or leaves the server.
    display_name = Column(String(100), nullable=False)

    # Enforce one birthday record per user per guild.
    __table_args__ = (
        UniqueConstraint("guild_id", "user_id", name="uq_guild_user_birthday"),
    )

    def __repr__(self) -> str:
        return (
            f"<Birthday guild={self.guild_id} user={self.user_id} "
            f"({self.birth_month}/{self.birth_day})>"
        )


class ScheduleConfig(Base):
    """
    Stores per-guild scheduling and channel configuration for each bot feature.

    There is exactly one row per (guild_id, feature) pair. Rows are created
    automatically with sensible defaults the first time a feature is used in a
    guild, then updated by admin slash commands.

    Feature identifiers
    -------------------
    "trivia"        — daily sports trivia (default 4 pm ET)
    "music"         — weekly new releases (default Monday 10 am ET)
    "birthday"      — daily birthday check (default 9 am ET)
    "album_review"  — monthly Pitchfork Best New Album post (default 1st, 10 am ET)
    "concerts"      — weekly concert alerts via Ticketmaster (default Monday 9 am ET)
    "movies"        — monthly Criterion Collection movie night pick (default 1st, 7 pm ET)
    "sports_scores" — continuous live playoff score polling (interval-based, not cron)

    Content options
    ---------------
    Stored as a JSON string in the `content_options` column and accessed through
    the @property pair below.

    trivia:        {"sports": ["soccer", "baseball", "football", "hockey"]}
    music:         {"genres": ["rock", "indie rock", "electronic"]}
    birthday:      {}   ← no content options, just scheduling / channel
    album_review:  {"day_of_month": 1}
    concerts:      {"cities": ["Toronto", "Montreal"]}
    movies:        {"day_of_month": 1}
    sports_scores: {"enabled": true, "enabled_sports": ["nfl", "nhl", "mlb", "soccer"]}
    """

    __tablename__ = "schedule_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Which Discord server this config row belongs to.
    guild_id = Column(BigInteger, nullable=False, index=True)

    # Which bot feature this row configures. See class docstring for identifiers.
    feature = Column(String(50), nullable=False)

    # ID of the Discord text channel where the feature posts its content.
    # NULL means the feature will fall back to searching by channel name.
    channel_id = Column(BigInteger, nullable=True)

    # Hour (0–23) and minute (0–59) in `timezone` when the scheduled post fires.
    hour = Column(Integer, nullable=False)
    minute = Column(Integer, nullable=False, default=0)

    # Day of week for weekly features: 'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'.
    # NULL means the job runs every day (trivia and birthday check).
    day_of_week = Column(String(10), nullable=True)

    # IANA timezone string, e.g. "America/New_York", "America/Los_Angeles".
    timezone = Column(String(50), nullable=False, default="America/New_York")

    # Raw JSON storage for feature-specific content options (see class docstring).
    # Accessed through the content_options property below — don't read this directly.
    _content_options = Column("content_options", String, nullable=True)

    # Enforce one config row per guild+feature combination.
    __table_args__ = (
        UniqueConstraint("guild_id", "feature", name="uq_guild_feature_config"),
    )

    @property
    def content_options(self) -> dict:
        """Deserialize the JSON content options string into a Python dict."""
        if self._content_options:
            return json.loads(self._content_options)
        return {}

    @content_options.setter
    def content_options(self, value: dict) -> None:
        """Serialize a Python dict into a JSON string for storage."""
        self._content_options = json.dumps(value)

    def __repr__(self) -> str:
        return (
            f"<ScheduleConfig guild={self.guild_id} feature={self.feature} "
            f"channel={self.channel_id} {self.hour:02d}:{self.minute:02d}>"
        )


class MusicPost(Base):
    """
    Records every release the music feature has posted in a guild.

    Used to prevent the same artist from appearing in the weekly post more
    than once. Before fetching new releases, the cog queries this table for
    all artist IDs ever posted in the guild and passes them to the Spotify
    client as an exclusion list.

    There is no cap on history — artists are excluded indefinitely unless
    the row is manually deleted from the database.
    """

    __tablename__ = "music_posts"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # The Discord server this post belongs to.
    guild_id = Column(BigInteger, nullable=False, index=True)

    # Spotify artist ID — used as the exclusion key in future searches.
    artist_id = Column(String(100), nullable=False)

    # Human-readable fields stored for reference and future admin commands.
    artist_name = Column(String(200), nullable=False)
    release_title = Column(String(200), nullable=False)

    # When this release was posted (UTC).
    posted_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self) -> str:
        return (
            f"<MusicPost guild={self.guild_id} artist={self.artist_name!r} "
            f"release={self.release_title!r} posted={self.posted_at}>"
        )


class AlbumReviewPost(Base):
    """
    Records every album review the album review feature has posted in a guild.

    The Pitchfork URL is used as the deduplication key — if the monthly job
    fires again before Pitchfork has published a new Best New Album, the cog
    will see the URL already in this table and skip the post.
    """

    __tablename__ = "album_review_posts"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # The Discord server this review was posted in.
    guild_id = Column(BigInteger, nullable=False, index=True)

    # Full Pitchfork review URL — used as the deduplication key so the same
    # album isn't posted twice in the same guild.
    pitchfork_url = Column(String(500), nullable=False)

    # Human-readable fields stored for reference and future admin commands.
    artist_name = Column(String(200), nullable=False)
    album_title = Column(String(200), nullable=False)

    # When this review was posted (UTC).
    posted_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self) -> str:
        return (
            f"<AlbumReviewPost guild={self.guild_id} artist={self.artist_name!r} "
            f"album={self.album_title!r} posted={self.posted_at}>"
        )


class ArtistWatchlist(Base):
    """
    Stores artists that a guild wants to monitor for upcoming concert alerts.

    Artists can be added via three sources:
      "seed"         — pre-loaded from the built-in seed list on first setup.
      "manual"       — added explicitly by an admin via /concert add.
      "new_releases" — auto-added when the music feature features that artist.

    The unique constraint on (guild_id, artist_name) ensures that the same
    artist never appears twice in a guild's watchlist regardless of source.
    """

    __tablename__ = "artist_watchlist"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # The Discord server this watchlist entry belongs to.
    guild_id = Column(BigInteger, nullable=False, index=True)

    # Artist name as a string (not a Spotify ID — sourced from multiple places).
    artist_name = Column(String(200), nullable=False)

    # Where this entry came from: "seed", "manual", or "new_releases".
    source = Column(String(50), nullable=False, default="manual")

    # When this entry was added.
    added_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Each artist can appear only once per guild.
    __table_args__ = (
        UniqueConstraint("guild_id", "artist_name", name="uq_guild_artist_watchlist"),
    )

    def __repr__(self) -> str:
        return (
            f"<ArtistWatchlist guild={self.guild_id} artist={self.artist_name!r} "
            f"source={self.source!r}>"
        )


class ConcertAlertPosted(Base):
    """
    Records each concert alert embed posted per guild.

    The Ticketmaster event ID is used as the deduplication key — if the weekly
    check runs again before an event has passed, the event won't be re-posted.
    """

    __tablename__ = "concert_alerts_posted"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # The Discord server this alert was posted in.
    guild_id = Column(BigInteger, nullable=False, index=True)

    # Ticketmaster's unique event identifier — used as the deduplication key.
    ticketmaster_event_id = Column(String(100), nullable=False)

    # Human-readable fields stored for reference.
    artist_name = Column(String(200), nullable=False)
    event_name = Column(String(300), nullable=False)
    venue_name = Column(String(200), nullable=True)
    city = Column(String(100), nullable=False)
    event_date = Column(String(50), nullable=True)

    # When this alert was posted (UTC).
    posted_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Each Ticketmaster event is posted at most once per guild.
    __table_args__ = (
        UniqueConstraint("guild_id", "ticketmaster_event_id", name="uq_guild_tm_event"),
    )

    def __repr__(self) -> str:
        return (
            f"<ConcertAlertPosted guild={self.guild_id} event={self.ticketmaster_event_id!r} "
            f"artist={self.artist_name!r}>"
        )


class CriterionFilm(Base):
    """
    Stores a single film from the Criterion Collection catalog.

    This table is GLOBAL — there is no guild_id column. All guilds share the
    same catalog fetched from TMDB. Per-guild pick history is tracked separately
    in movie_night_picks.

    The tmdb_id is used as the primary key (not autoincrement) because TMDB IDs
    are stable and serve as natural identifiers for the catalog.
    """

    __tablename__ = "criterion_films"

    # TMDB's stable numeric film ID — used as PK and FK in movie_night_picks.
    tmdb_id = Column(Integer, primary_key=True, autoincrement=False)

    # Film title as listed on TMDB.
    title = Column(String(300), nullable=False)

    # Four-digit release year, e.g. 1953. NULL if TMDB doesn't have a date.
    year = Column(Integer, nullable=True)

    # Director name from TMDB credits. NULL if credits were unavailable.
    director = Column(String(200), nullable=True)

    # Plot overview from TMDB, truncated to 1000 characters.
    overview = Column(String(1000), nullable=True)

    # Full URL to the movie poster image (TMDB image CDN), or NULL.
    poster_url = Column(String(500), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<CriterionFilm tmdb_id={self.tmdb_id} title={self.title!r} "
            f"year={self.year} director={self.director!r}>"
        )


class MovieNightPick(Base):
    """
    Records which Criterion films have been featured on movie night per guild.

    Used to ensure the monthly rotation doesn't repeat a film until all films
    have been shown. When no unwatched films remain, the cog clears this table
    for the guild and starts the rotation over.

    No unique constraint is intentional — if the history is reset, the same
    film can appear again in future picks.
    """

    __tablename__ = "movie_night_picks"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # The Discord server this pick belongs to.
    guild_id = Column(BigInteger, nullable=False, index=True)

    # The TMDB ID of the film that was picked.
    tmdb_id = Column(Integer, nullable=False)

    # Film title at the time of the pick (denormalized for convenience).
    movie_title = Column(String(300), nullable=False)

    # When this film was picked (UTC).
    picked_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self) -> str:
        return (
            f"<MovieNightPick guild={self.guild_id} tmdb_id={self.tmdb_id} "
            f"title={self.movie_title!r} picked={self.picked_at}>"
        )


class LiveGameState(Base):
    """
    Tracks the live state of an in-progress or recently finished playoff game
    for a single guild.

    Each row represents one game that the sports scores cog is monitoring. The
    polling job uses this table to determine:
      - Whether we have already announced this game starting.
      - Which scoring plays have already been posted (via last_play_index).
      - The last known score, so we can post a final if the game disappears
        from the ESPN feed between polls without returning STATUS_FINAL.

    Rows are never deleted automatically — status transitions from "in_progress"
    to "final" mark the game as done. Old rows do not affect correctness because
    the polling logic only acts on rows with status "in_progress".

    guild_id + game_id is unique: the same ESPN event won't be inserted twice
    for the same guild even if two poll ticks overlap.
    """

    __tablename__ = "live_game_states"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # The Discord server this game state belongs to.
    guild_id = Column(BigInteger, nullable=False, index=True)

    # ESPN's stable event ID string (e.g. "401671793").
    game_id = Column(String(50), nullable=False)

    # Sport name: "nfl", "nhl", "mlb", or "soccer".
    sport = Column(String(20), nullable=False)

    # Team names snapshotted at game-start detection time.
    home_team = Column(String(200), nullable=False, default="")
    away_team = Column(String(200), nullable=False, default="")

    # Current scores as of the most recent poll.
    home_score = Column(Integer, nullable=False, default=0)
    away_score = Column(Integer, nullable=False, default=0)

    # Simplified status: "in_progress" or "final".
    status = Column(String(50), nullable=False, default="in_progress")

    # True once we have posted the "game starting" embed for this game.
    start_announced = Column(Boolean, nullable=False, default=False)

    # Index into the ESPN details array of the last scoring play we posted.
    # Starts at -1 (no plays posted yet) and advances as plays are reported.
    # We set it to len(scoring_plays)-1 at game-start time so we don't
    # replay scoring plays that already happened before we detected the game.
    last_play_index = Column(Integer, nullable=False, default=-1)

    # When this row was last updated (UTC). Updated on every poll that touches
    # this game's state.
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # UTC timestamp of the first poll where we saw this game stalled at
    # STATUS_FULL_TIME with ESPN's completed flag set. NULL when no grace
    # period is currently running for this game. ESPN sets completed=True at
    # the 90-minute whistle even for soccer knockout games that are about to
    # go to extra time, so STATUS_FULL_TIME + completed=True is ambiguous —
    # we hold off treating it as a real final until this timestamp is more
    # than _FULL_TIME_GRACE_SECONDS in the past, giving ESPN a chance to
    # push STATUS_EXTRA_TIME instead (which resets this back to NULL).
    pending_final_since = Column(DateTime, nullable=True, default=None)

    # One game row per guild — prevents duplicate "game starting" embeds if
    # two poll ticks fire simultaneously.
    __table_args__ = (
        UniqueConstraint("guild_id", "game_id", name="uq_guild_game"),
    )

    def __repr__(self) -> str:
        return (
            f"<LiveGameState guild={self.guild_id} game={self.game_id!r} "
            f"sport={self.sport!r} status={self.status!r} "
            f"{self.away_team} {self.away_score}–{self.home_score} {self.home_team}>"
        )


class TriviaQuestionPost(Base):
    """
    Records every trivia question Claude has generated and posted in a guild.

    Used to stop the daily era rotation (see utils.claude_client.get_daily_trivia_era)
    from repeating the same "greatest hits" fact every time an era comes back
    around — e.g. always landing on the same 1980s statistic every four days.
    Before generating a new question, the trivia cog looks up recent rows for
    the same (guild_id, era) pair and passes their question_text into the
    prompt as an explicit avoid-list.

    No cap on history, same convention as music_posts — old rows are harmless,
    they just add to the avoid-list context sent to Claude. The cog limits how
    many recent rows it actually queries and sends (see trivia.py).
    """

    __tablename__ = "trivia_question_posts"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # The Discord server this question was posted in.
    guild_id = Column(BigInteger, nullable=False, index=True)

    # Sport the question was about, as returned by Claude (e.g. "soccer").
    sport = Column(String(50), nullable=False)

    # Stable era key from get_daily_trivia_era(), e.g. "1970s_1990s" — NOT the
    # full descriptive era label used in the prompt, so history lookups keep
    # working even if the prompt wording for an era is edited later.
    era = Column(String(50), nullable=False, index=True)

    # The generated question text — this is what gets echoed back to Claude
    # as something to avoid repeating.
    question_text = Column(String(500), nullable=False)

    # When this question was posted (UTC).
    posted_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self) -> str:
        return (
            f"<TriviaQuestionPost guild={self.guild_id} sport={self.sport!r} "
            f"era={self.era!r} posted={self.posted_at}>"
        )
