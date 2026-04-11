import _bootstrap  # noqa: F401

from app.db.init_db import init_db


if __name__ == "__main__":
    init_db()
    print("Database initialized.")
