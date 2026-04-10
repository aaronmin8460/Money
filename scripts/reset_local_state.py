import argparse
import json

from app.services.local_state_reset import LocalStateResetOptions, reset_local_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reset local Money bot paper-trading state.")
    parser.add_argument(
        "--close-positions",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Close paper positions before clearing local state.",
    )
    parser.add_argument(
        "--cancel-open-orders",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Cancel paper open orders before clearing local state.",
    )
    parser.add_argument(
        "--wipe-local-db",
        action="store_true",
        help="Drop and recreate the local SQLite database schema.",
    )
    parser.add_argument(
        "--reset-daily-baseline-to-current-equity",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Reset the runtime daily baseline to the current account equity after clearing state.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = reset_local_state(
        LocalStateResetOptions(
            close_positions=args.close_positions,
            cancel_open_orders=args.cancel_open_orders,
            wipe_local_db=args.wipe_local_db,
            reset_daily_baseline_to_current_equity=args.reset_daily_baseline_to_current_equity,
        )
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
