from __future__ import annotations

import argparse
import os
import sys

import uvicorn

import _bootstrap  # noqa: F401


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
    print(
        "money_api_entrypoint=run_paper_api "
        f"pid={os.getpid()} "
        f"python_bin={sys.executable} "
        f"host={args.host} "
        f"port={args.port} "
        "workers=1 reload=false",
        flush=True,
    )
    uvicorn.run(
        "main:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        reload=False,
        workers=1,
    )
    print("money_api_entrypoint_stopped=true", flush=True)


if __name__ == "__main__":
    main()
