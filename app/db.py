from __future__ import annotations

import time

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


_engine_kwargs: dict = {"pool_pre_ping": True, "future": True}
if config.IS_SQLITE:
    _engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
else:
    _engine_kwargs.update(pool_size=5, max_overflow=10, pool_recycle=1800)

engine = create_engine(config.DATABASE_URL, **_engine_kwargs)

if config.IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def wait_for_db(retries: int = 30, delay: float = 2.0) -> None:
    """External databases may start after us (docker compose); wait for them."""
    last = None
    for _ in range(retries):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except Exception as exc:  # pragma: no cover
            last = exc
            print(f"[db] waiting for database: {exc.__class__.__name__}")
            time.sleep(delay)
    raise RuntimeError(f"Database not reachable: {last}")


def init_db() -> None:
    """Create tables and add any columns introduced by newer versions.

    New columns are always nullable/defaulted, so this light-weight approach
    works identically on SQLite, PostgreSQL and MySQL/MariaDB.
    """
    from . import models  # noqa: F401  (register models)

    wait_for_db()
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                coltype = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {coltype}'))
                # Backfill scalar defaults so existing rows get sensible values (e.g. is_active=True).
                if col.default is not None and getattr(col.default, "is_scalar", False):
                    conn.execute(table.update().values({col.name: col.default.arg}))
                print(f"[db] migrated: added {table.name}.{col.name}")
