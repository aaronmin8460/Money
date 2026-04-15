from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import httpx

import _bootstrap  # noqa: F401


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class VerificationReport:
    passes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def add_pass(self, message: str) -> None:
        self.passes.append(message)

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)

    def add_failure(self, message: str) -> None:
        self.failures.append(message)

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else 0


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        values[key.strip()] = value.strip().strip("'\"")
    return values


def env_flag(values: Mapping[str, str], key: str, *, default: bool = False) -> bool:
    raw = values.get(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def resolve_app_path(app_dir: Path, configured_path: str | None, fallback: str) -> Path:
    candidate = Path(configured_path or fallback)
    if candidate.is_absolute():
        return candidate
    return (app_dir / candidate).resolve()


def parse_systemd_show(output: str) -> dict[str, str]:
    payload: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        payload[key] = value
    return payload


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def systemd_show(unit_name: str) -> tuple[dict[str, str], str]:
    result = run_command(
        [
            "systemctl",
            "show",
            unit_name,
            "--property",
            "LoadState,UnitFileState,ActiveState,SubState,Result,NextElapseUSecRealtime,LastTriggerUSec",
        ]
    )
    if result.returncode != 0:
        return {}, (result.stderr or result.stdout).strip()
    return parse_systemd_show(result.stdout), ""


def load_latest_jsonl_record(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    latest: dict[str, Any] | None = None
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                latest = payload
    return latest


def parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def determine_paper_safety(
    env_values: Mapping[str, str],
    auto_status: Mapping[str, Any],
) -> tuple[bool, str]:
    broker_mode = str(auto_status.get("broker_mode") or env_values.get("BROKER_MODE") or "").strip().lower()
    trading_enabled = bool(auto_status.get("trading_enabled", env_flag(env_values, "TRADING_ENABLED")))
    live_enabled = bool(auto_status.get("live_trading_enabled", env_flag(env_values, "LIVE_TRADING_ENABLED")))

    if broker_mode != "paper":
        return False, f"BROKER_MODE must remain paper for Phase 4 deployments (found {broker_mode or 'unset'})."
    if live_enabled:
        return False, "LIVE_TRADING_ENABLED must remain false for Phase 4 paper operations."
    if trading_enabled:
        return True, "Paper order submission is enabled and still guarded away from live trading."
    return True, "Deployment remains in paper-safe dry-run mode."


def fetch_json(url: str, *, timeout: float) -> tuple[dict[str, Any] | None, str]:
    try:
        response = httpx.get(url, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return None, str(exc)
    if not isinstance(payload, dict):
        return None, f"Expected JSON object from {url}, got {type(payload).__name__}."
    return payload, ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify the Money Phase 4 EC2/systemd paper-trading deployment.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="API base URL to verify.")
    parser.add_argument("--env-file", default="/etc/money/money.env", help="Deployment env file path.")
    parser.add_argument("--app-dir", default=str(REPO_ROOT), help="Deployed repository path.")
    parser.add_argument(
        "--news-max-age-hours",
        type=float,
        default=2.5,
        help="Maximum acceptable age for the latest news feature artifact.",
    )
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = VerificationReport()
    app_dir = Path(args.app_dir).resolve()
    env_file = Path(args.env_file)
    env_values = parse_env_file(env_file)

    if env_file.exists():
        report.add_pass(f"Deployment env file is present at {env_file}.")
    else:
        report.add_failure(f"Deployment env file is missing: {env_file}.")

    health_payload, health_error = fetch_json(f"{args.base_url}/health", timeout=args.timeout)
    if health_payload is None:
        report.add_failure(f"Health endpoint check failed: {health_error}")
    else:
        report.add_pass(
            f"Health endpoint responded with status={health_payload.get('status')} mode={health_payload.get('mode')}."
        )

    auto_status, auto_status_error = fetch_json(f"{args.base_url}/auto/status", timeout=args.timeout)
    if auto_status is None:
        report.add_failure(f"/auto/status check failed: {auto_status_error}")
        auto_status = {}
    else:
        report.add_pass(
            "Auto-trader status endpoint is reachable "
            f"(running={auto_status.get('running')} enabled={auto_status.get('enabled')})."
        )

    auto_trade_enabled = bool(auto_status.get("enabled", env_flag(env_values, "AUTO_TRADE_ENABLED")))
    auto_trader_running = bool(auto_status.get("running"))
    process_lock_acquired = bool(auto_status.get("process_lock_acquired"))
    if auto_trade_enabled:
        if auto_trader_running:
            report.add_pass("AUTO_TRADE_ENABLED is true and the in-process auto-trader loop is running.")
        else:
            report.add_failure("AUTO_TRADE_ENABLED is true but the in-process auto-trader loop is not running.")
        if process_lock_acquired:
            report.add_pass("The auto-trader process lock is acquired.")
        else:
            report.add_failure("The auto-trader process lock is not acquired.")
    else:
        report.add_warning("AUTO_TRADE_ENABLED is false; continuous paper trading is not active.")
        if auto_trader_running:
            report.add_warning("The auto-trader loop is running even though AUTO_TRADE_ENABLED is false.")

    api_unit, api_unit_error = systemd_show("money-api.service")
    if not api_unit:
        report.add_failure(f"Unable to inspect money-api.service: {api_unit_error or 'systemctl show failed'}")
    elif api_unit.get("LoadState") != "loaded" or api_unit.get("ActiveState") != "active":
        report.add_failure(
            "money-api.service is not healthy "
            f"(load={api_unit.get('LoadState')} active={api_unit.get('ActiveState')} sub={api_unit.get('SubState')})."
        )
    else:
        report.add_pass(
            "money-api.service is healthy "
            f"(active={api_unit.get('ActiveState')} sub={api_unit.get('SubState')})."
        )

    for timer_name in ("money-news.timer", "money-retrain.timer"):
        timer_state, timer_error = systemd_show(timer_name)
        next_run = timer_state.get("NextElapseUSecRealtime", "")
        if not timer_state:
            report.add_failure(f"Unable to inspect {timer_name}: {timer_error or 'systemctl show failed'}")
            continue
        if timer_state.get("LoadState") != "loaded":
            report.add_failure(f"{timer_name} is not installed correctly (load={timer_state.get('LoadState')}).")
            continue
        if timer_state.get("ActiveState") != "active":
            report.add_failure(f"{timer_name} is installed but not active (active={timer_state.get('ActiveState')}).")
            continue
        if not next_run or next_run == "n/a":
            report.add_failure(f"{timer_name} is active but the next run is not visible.")
            continue
        report.add_pass(f"{timer_name} is active and next runs at {next_run}.")

    log_dir = resolve_app_path(app_dir, env_values.get("LOG_DIR"), "logs")
    if log_dir.exists():
        report.add_pass(f"Log directory exists at {log_dir}.")
    else:
        report.add_failure(f"Log directory is missing: {log_dir}.")

    app_log = log_dir / "app.jsonl"
    if app_log.exists():
        report.add_pass(f"Structured application log exists at {app_log}.")
    else:
        report.add_failure(f"Structured application log is missing: {app_log}.")

    for artifact_name in ("signals.jsonl", "orders.jsonl", "outcomes.jsonl"):
        artifact_path = log_dir / artifact_name
        if artifact_path.exists():
            report.add_pass(f"Artifact exists: {artifact_path}.")
        else:
            report.add_warning(f"Artifact not present yet: {artifact_path}.")

    news_pipeline_enabled = env_flag(env_values, "NEWS_FEATURES_ENABLED") and env_flag(env_values, "NEWS_RSS_ENABLED")
    news_path = log_dir / "news_features.jsonl"
    latest_news_record = load_latest_jsonl_record(news_path)
    if news_pipeline_enabled:
        if latest_news_record is None:
            report.add_failure(f"News pipeline is enabled but no news feature records were found at {news_path}.")
        else:
            recorded_at = parse_iso8601(str(latest_news_record.get("recorded_at") or ""))
            max_age = timedelta(hours=args.news_max_age_hours)
            if recorded_at is None:
                report.add_failure(f"Latest news feature record has no parseable recorded_at timestamp: {news_path}.")
            elif datetime.now(timezone.utc) - recorded_at > max_age:
                report.add_failure(
                    "Latest news feature record is stale "
                    f"({recorded_at.isoformat().replace('+00:00', 'Z')} at {news_path})."
                )
            else:
                report.add_pass(
                    "Recent news features were written "
                    f"(recorded_at={recorded_at.isoformat().replace('+00:00', 'Z')} "
                    f"analysis_mode={latest_news_record.get('analysis_mode')} "
                    f"analysis_reason={latest_news_record.get('analysis_reason')})."
                )
    else:
        report.add_warning(
            "NEWS_FEATURES_ENABLED and NEWS_RSS_ENABLED are not both true; recent news feature verification was skipped."
        )

    registry_path = resolve_app_path(app_dir, env_values.get("ML_REGISTRY_PATH"), "models/registry.json")
    if not registry_path.exists():
        report.add_failure(f"Model registry is missing: {registry_path}.")
    else:
        try:
            json.loads(registry_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            report.add_failure(f"Model registry is not valid JSON: {registry_path} ({exc}).")
        else:
            report.add_pass(f"Model registry exists and is valid JSON at {registry_path}.")

    paper_safe, paper_safe_message = determine_paper_safety(env_values, auto_status)
    if paper_safe:
        report.add_pass(paper_safe_message)
    else:
        report.add_failure(paper_safe_message)

    if env_flag(env_values, "ML_RETRAIN_ENABLED"):
        report.add_pass("ML_RETRAIN_ENABLED is true; nightly retrain is configured to do real work when data is available.")
    else:
        report.add_warning("ML_RETRAIN_ENABLED is false; the nightly retrain timer will skip cleanly.")

    lock_path = resolve_app_path(app_dir, env_values.get("AUTO_TRADER_LOCK_PATH"), "logs/auto_trader.lock")
    if auto_trade_enabled:
        if lock_path.exists():
            report.add_pass(f"Auto-trader lock file exists at {lock_path}.")
        else:
            report.add_failure(f"Auto-trader lock file is missing at {lock_path}.")

    for message in report.passes:
        print(f"PASS {message}")
    for message in report.warnings:
        print(f"WARN {message}")
    for message in report.failures:
        print(f"FAIL {message}")

    if report.failures:
        print(f"Verification failed with {len(report.failures)} issue(s).")
    else:
        print("Verification passed.")

    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
