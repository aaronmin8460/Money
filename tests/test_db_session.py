from __future__ import annotations

from app.config.settings import Settings
from app.db import session as db_session_module


def test_sqlite_engine_options_apply_sqlite_only_flags() -> None:
    options = db_session_module.build_engine_options("sqlite:///./trading.db")

    assert options["connect_args"] == {
        "check_same_thread": False,
        "timeout": 15,
    }
    assert "pool_pre_ping" not in options


def test_postgres_engine_options_apply_pool_pre_ping_without_sqlite_hacks() -> None:
    options = db_session_module.build_engine_options(
        "postgresql+psycopg://money:secret@127.0.0.1:5432/money"
    )

    assert options["pool_pre_ping"] is True
    assert "connect_args" not in options


def test_get_engine_reconfigures_session_factory_for_settings_database_url(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'session-test.db'}"
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        database_url=database_url,
    )

    engine = db_session_module.get_engine(settings)

    assert str(engine.url) == database_url
    assert db_session_module.SessionLocal.kw["bind"] is engine


def test_check_database_connection_succeeds_for_sqlite_file(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'readiness.db'}"

    assert db_session_module.check_database_connection(database_url=database_url) is True


def test_sqlite_engine_initializes_wal_mode(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'wal.db'}"
    engine = db_session_module.create_db_engine(database_url)

    try:
        with engine.connect() as connection:
            journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
            synchronous = connection.exec_driver_sql("PRAGMA synchronous").scalar_one()
    finally:
        engine.dispose()

    assert journal_mode == "wal"
    assert synchronous == 1


def test_check_database_connection_fails_for_missing_sqlite_directory(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'missing-dir' / 'readiness.db'}"

    assert db_session_module.check_database_connection(database_url=database_url) is False
