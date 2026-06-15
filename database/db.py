"""
database/db.py
──────────────
Creates the SQLAlchemy engine and session factory used throughout the bot.

Usage in other modules
-----------------------
    from database.db import SessionLocal

    # Use as a context manager — session is closed automatically on exit.
    with SessionLocal() as session:
        record = session.query(Birthday).filter_by(user_id=123).first()
        session.add(new_record)
        session.commit()
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
from database.models import Base


# SQLite requires check_same_thread=False when accessed from multiple threads
# (APScheduler runs scheduled jobs in threads by default). PostgreSQL ignores
# this argument entirely, so it's safe to include unconditionally.
_connect_args = (
    {"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {}
)

engine = create_engine(
    config.DATABASE_URL,
    connect_args=_connect_args,
    # Keep connections alive by checking them before use (prevents "server closed
    # the connection unexpectedly" errors after periods of inactivity).
    pool_pre_ping=True,
    # Recycle connections after 30 minutes to prevent stale socket issues,
    # especially relevant for Cloud SQL which has its own idle timeout.
    pool_recycle=1800,
)

# sessionmaker returns a factory — every call to SessionLocal() creates a
# fresh Session object bound to the engine above.
SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,   # we commit explicitly so we can roll back on error
    autoflush=False,    # we flush explicitly for the same reason
    expire_on_commit=False,  # keep attribute values accessible after commit
)


def init_db() -> None:
    """
    Create all database tables that don't already exist.

    This is safe to call on every bot startup — SQLAlchemy only creates missing
    tables and never modifies or drops existing ones. For schema changes to
    existing tables, use Alembic migrations (see alembic/ directory) instead.
    """
    Base.metadata.create_all(bind=engine)
