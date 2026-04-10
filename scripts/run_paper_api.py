from __future__ import annotations

import argparse

import uvicorn


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Money API in a single-process mode suitable for the in-process auto-trader."
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Defaults to 127.0.0.1.")
    parser.add_argument("--port", type=int, default=8000, help="Bind port. Defaults to 8000.")
    parser.add_argument("--log-level", default="info", help="Uvicorn log level. Defaults to info.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    uvicorn.run(
        "main:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        reload=False,
        workers=1,
    )


if __name__ == "__main__":
    main()
