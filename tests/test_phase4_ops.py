from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from app.config.settings import Settings


def _load_verify_phase4_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "verify_phase4.py"
    scripts_dir = str(script_path.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("verify_phase4_module", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_settings_expose_news_llm_status_reasons() -> None:
    with patch.dict(os.environ, {}, clear=True):
        base_kwargs = {
            "_env_file": None,
            "broker_mode": "mock",
            "trading_enabled": False,
            "news_features_enabled": True,
            "news_rss_enabled": True,
            "news_llm_enabled": True,
        }

        missing_key = Settings(**base_kwargs)
        llm_disabled = Settings(**{**base_kwargs, "news_llm_enabled": False})
        rss_disabled = Settings(**{**base_kwargs, "news_rss_enabled": False})
        features_disabled = Settings(**{**base_kwargs, "news_features_enabled": False})
        available = Settings(**{**base_kwargs, "openai_api_key": "test-key"})

    assert missing_key.news_llm_status == "openai_api_key_missing"
    assert missing_key.news_llm_available is False
    assert llm_disabled.news_llm_status == "news_llm_disabled"
    assert rss_disabled.news_llm_status == "news_rss_disabled"
    assert features_disabled.news_llm_status == "news_features_disabled"
    assert available.news_llm_status == "available"
    assert available.news_llm_available is True


def test_retrain_script_skips_cleanly_when_disabled() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "run_nightly_retrain.sh"
    env = dict(os.environ)
    env["ML_RETRAIN_ENABLED"] = "false"

    result = subprocess.run(
        ["bash", str(script_path)],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "nightly_retrain_skipped=true reason=ml_retrain_disabled" in result.stdout


def test_deploy_env_example_keeps_safe_phase4_defaults() -> None:
    env_example_path = Path(__file__).resolve().parents[1] / "deploy" / "env" / "money.env.example"
    env_values: dict[str, str] = {}
    contents = env_example_path.read_text(encoding="utf-8")

    for raw_line in contents.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        env_values[key] = value

    assert env_values["BROKER_MODE"] == "paper"
    assert env_values["TRADING_ENABLED"] == "false"
    assert env_values["AUTO_TRADE_ENABLED"] == "false"
    assert env_values["LIVE_TRADING_ENABLED"] == "false"
    assert env_values["TRADING_PROFILE"] == "conservative"
    assert env_values["AGGRESSIVE_MODE_ENABLED"] == "false"
    assert env_values["DISCORD_NOTIFICATIONS_ENABLED"] == "false"
    assert env_values["ML_ENABLED"] == "false"
    assert env_values["ML_RETRAIN_ENABLED"] == "false"
    assert env_values["NEWS_FEATURES_ENABLED"] == "false"
    assert env_values["NEWS_RSS_ENABLED"] == "false"
    assert env_values["NEWS_LLM_ENABLED"] == "false"
    assert env_values["BENZINGA_RSS_ENABLED"] == "false"
    assert env_values["REUTERS_RSS_URLS"] == "[]"
    assert env_values["BENZINGA_RSS_URLS"] == "[]"
    assert env_values["SEC_RSS_ENABLED"] == "false"
    assert env_values["SEC_COMPANY_TICKERS_CACHE_TTL_HOURS"] == "24"
    assert env_values["RATE_LIMIT_ENABLED"] == "false"
    assert env_values["ALPACA_API_KEY"] == ""
    assert env_values["ALPACA_SECRET_KEY"] == ""
    assert env_values["DISCORD_WEBHOOK_URL"] == ""
    assert env_values["OPENAI_API_KEY"] == ""
    assert env_values["API_ADMIN_TOKEN"] == ""
    assert "paper dry-run" in contents.lower()
    assert "paper order-submission mode" in contents.lower()
    assert "aggressive paper profile example" in contents.lower()
    assert "benzinga rss example" in contents.lower()
    assert "sec rss example" in contents.lower()
    assert "rate limit example" in contents.lower()
    assert "www.benzinga.com/feeds/news" not in contents


def test_verify_phase4_helpers_parse_env_and_check_paper_safety(tmp_path) -> None:
    verify_phase4 = _load_verify_phase4_module()
    env_path = tmp_path / "money.env"
    env_path.write_text(
        "\n".join(
            [
                "# comment",
                "BROKER_MODE=paper",
                "TRADING_ENABLED=true",
                "LIVE_TRADING_ENABLED=false",
                "LOG_DIR=logs",
                "",
            ]
        ),
        encoding="utf-8",
    )

    env_values = verify_phase4.parse_env_file(env_path)
    paper_safe, message = verify_phase4.determine_paper_safety(
        env_values,
        {
            "broker_mode": "paper",
            "trading_enabled": True,
            "live_trading_enabled": False,
        },
    )
    resolved_log_dir = verify_phase4.resolve_app_path(tmp_path, env_values.get("LOG_DIR"), "logs")

    assert env_values["BROKER_MODE"] == "paper"
    assert verify_phase4.env_flag({"AUTO_TRADE_ENABLED": "true"}, "AUTO_TRADE_ENABLED") is True
    assert paper_safe is True
    assert "paper order submission" in message.lower()
    assert resolved_log_dir == (tmp_path / "logs").resolve()
