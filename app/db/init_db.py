from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect

from app.config.settings import get_settings
from app.db.models import Base
from app.db.session import get_engine
from app.monitoring.logger import get_logger

logger = get_logger("db.init")
ALEMBIC_INI_PATH = Path(__file__).resolve().parents[2] / "alembic.ini"


def _run_alembic_upgrade() -> bool:
    try:
        from alembic import command
        from alembic.config import Config
    except (ImportError, ModuleNotFoundError):
        return False

    settings = get_settings()
    engine = get_engine(settings)
    existing_tables = set(inspect(engine).get_table_names())
    expected_tables = set(Base.metadata.tables)
    config = Config(str(ALEMBIC_INI_PATH))
    config.set_main_option("script_location", str(ALEMBIC_INI_PATH.with_name("alembic")))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    config.attributes["force_sqlalchemy_url"] = settings.database_url
    if existing_tables and "alembic_version" not in existing_tables:
        if expected_tables.issubset(existing_tables):
            logger.info(
                "Stamping existing schema at Alembic head because the database already matches the current models."
            )
            command.stamp(config, "head")
            return True
        raise RuntimeError(
            "Existing database schema does not match the initial Alembic migration. "
            "Back up or reset the database, then run 'alembic upgrade head' manually."
        )
    command.upgrade(config, "head")
    return True


def init_db() -> None:
    """Initialize or migrate the database schema for the paper trading bot."""
    if _run_alembic_upgrade():
        return

    logger.warning(
        "Alembic is not installed; falling back to SQLAlchemy create_all for database initialization."
    )
    Base.metadata.create_all(bind=get_engine())
