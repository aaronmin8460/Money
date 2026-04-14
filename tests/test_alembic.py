from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from app.config import settings as settings_module
from app.config.settings import Settings
from app.db.init_db import init_db
from app.db.models import Base
from app.db import session as db_session_module


def test_alembic_files_exist() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    assert (repo_root / "alembic.ini").exists()
    assert (repo_root / "alembic" / "env.py").exists()
    assert (repo_root / "alembic" / "script.py.mako").exists()
    assert any((repo_root / "alembic" / "versions").glob("*.py"))


@pytest.mark.skipif(importlib.util.find_spec("alembic.command") is None, reason="alembic is not installed")
def test_alembic_upgrade_head_smoke(tmp_path) -> None:
    from alembic import command
    from alembic.config import Config

    repo_root = Path(__file__).resolve().parents[1]
    database_url = f"sqlite:///{tmp_path / 'alembic-smoke.db'}"
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["force_sqlalchemy_url"] = database_url

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "orders" in tables
        assert "positions" in tables
        assert "fills" in tables
        assert "runtime_safety_state" in tables
        assert "alembic_version" in tables
    finally:
        engine.dispose()


@pytest.mark.skipif(importlib.util.find_spec("alembic.command") is None, reason="alembic is not installed")
def test_init_db_stamps_existing_matching_schema(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'existing-schema.db'}"
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        database_url=database_url,
    )
    settings_module._settings = settings
    engine = db_session_module.get_engine(settings)
    Base.metadata.create_all(bind=engine)

    init_db()

    with engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    assert version == "20260414_0002"
