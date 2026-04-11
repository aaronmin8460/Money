from __future__ import annotations

import _bootstrap  # noqa: F401

from app.rl.train_stub import train_stub


def main() -> None:
    history = [
        {"close": 100.0},
        {"close": 101.5},
        {"close": 99.0},
        {"close": 102.0},
    ]
    result = train_stub(history)
    print(f"experimental_only=true result={result}")


if __name__ == "__main__":
    main()
