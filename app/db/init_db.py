from __future__ import annotations

from app.db.models import Base
from app.db.session import engine


def init_db() -> None:
    """Initialize the database schema for the paper trading bot."""
    Base.metadata.create_all(bind=engine)
