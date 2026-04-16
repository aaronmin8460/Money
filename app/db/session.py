from __future__ import annotations

import os
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.config.settings import Settings, get_settings

DEFAULT_DATABASE_URL = "sqlite:///./trading.db"
SQLITE_TIMEOUT_SECONDS = 15


def is_sqlite_database_url(database_url: str) -> bool:
    return make_url(database_url).get_backend_name() == "sqlite"


def build_engine_options(database_url: str) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if is_sqlite_database_url(database_url):
        options["connect_args"] = {
            "check_same_thread": False,
            "timeout": SQLITE_TIMEOUT_SECONDS,
        }
    else:
        options["pool_pre_ping"] = True
    return options


def _configure_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def create_db_engine(database_url: str) -> Engine:
    db_engine = create_engine(database_url, **build_engine_options(database_url))
    if is_sqlite_database_url(database_url):
        event.listen(db_engine, "connect", _configure_sqlite_pragmas)
    return db_engine


_engine_url = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
engine = create_db_engine(_engine_url)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def reset_engine(database_url: str | None = None) -> Engine:
    global engine, _engine_url

    target_url = database_url or os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
    engine.dispose()
    engine = create_db_engine(target_url)
    _engine_url = target_url
    SessionLocal.configure(bind=engine)
    return engine


def get_engine(settings: Settings | None = None) -> Engine:
    global engine, _engine_url

    resolved_url = (settings or get_settings()).database_url
    if resolved_url != _engine_url:
        engine.dispose()
        engine = create_db_engine(resolved_url)
        _engine_url = resolved_url
        SessionLocal.configure(bind=engine)
    return engine


def check_database_connection(
    settings: Settings | None = None,
    *,
    database_url: str | None = None,
) -> bool:
    owns_engine = database_url is not None
    db_engine = create_db_engine(database_url) if database_url else get_engine(settings)
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except (OSError, SQLAlchemyError):
        return False
    finally:
        if owns_engine:
            db_engine.dispose()


def get_db():
    get_engine()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
