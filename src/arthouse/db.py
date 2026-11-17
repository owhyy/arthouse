"""SQLite engine setup and schema helpers.

The ORM models live in `arthouse.models`; this module owns everything about
the database connection: pragmas, table creation, and the additive index pass.
"""

from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool import ConnectionPoolEntry

from arthouse.models import Base


def create_db_engine(database: Path, *, echo: bool = False) -> Engine:
    """SQLite engine tuned for one writer and concurrent readers.

    WAL lets the enrichment passes write while the app keeps serving reads,
    `busy_timeout` waits for a blocked writer instead of failing immediately,
    and the larger cache keeps the derived tables in memory. `synchronous=NORMAL`
    is the recommended pairing with WAL for a local app: safe against process
    crashes, though a power loss can cost the last transaction.
    """
    engine = create_engine(f"sqlite:///{database}", echo=echo)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(
        dbapi_connection: DBAPIConnection, _connection_record: ConnectionPoolEntry
    ) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA cache_size=-16000")
        cursor.close()

    return engine


def ensure_indexes(engine: Engine) -> None:
    """Create missing indexes on tables `create_all` left untouched.

    `create_all` skips a table that already exists together with its indexes,
    so a DB created before an index was added would never get it. Index DDL is
    additive and idempotent, so completing it here needs no data rebuild.
    """
    for table in Base.metadata.sorted_tables:
        for index in table.indexes:
            index.create(engine, checkfirst=True)


def create_schema(engine: Engine) -> None:
    """Create any missing tables, then backfill their indexes."""
    Base.metadata.create_all(engine)
    ensure_indexes(engine)
