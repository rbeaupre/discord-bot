"""
database/models.py
──────────────────
SQLAlchemy ORM models. Each class maps to one database table.

Tables
------
birthdays        — One row per registered user birthday per guild.
schedule_configs — One row per (guild, feature) pair storing the channel,
                   posting schedule, and content options for each bot feature.
music_posts      — One row per release posted by the music feature. Used to
                   prevent the same artist from being posted repeatedly.
"""

import json

from sqlalchemy import BigInteger, Column, DateTime, Integer, String, UniqueConstraint
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
    "trivia"   — daily sports trivia (default 4 pm ET)
    "music"    — weekly new releases (default Monday 10 am ET)
    "birthday" — daily birthday check (default 9 am ET)

    Content options
    ---------------
    Stored as a JSON string in the `content_options` column and accessed through
    the @property pair below.

    trivia:   {"sports": ["soccer", "baseball", "football", "hockey"]}
    music:    {"genres": ["rock", "indie rock", "electronic"]}
    birthday: {}   ← no content options, just scheduling / channel
    """

    __tablename__ = "schedule_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Which Discord server this config row belongs to.
    guild_id = Column(BigInteger, nullable=False, index=True)

    # Which bot feature this row configures ("trivia", "music", "birthday").
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
