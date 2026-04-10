from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx

from app.config.settings import Settings, get_settings
from app.monitoring.logger import get_logger

if TYPE_CHECKING:
    from app.risk.risk_manager import RiskDecision
    from app.services.broker import OrderRequest
    from app.strategies.base import TradeSignal


logger = get_logger("discord")

_MAX_EMBED_TITLE = 256
_MAX_EMBED_DESCRIPTION = 4096
_MAX_FIELD_NAME = 256
_MAX_FIELD_VALUE = 1024
_MAX_CONTENT = 2000


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 3:
        return value[:limit]
    return f"{value[: limit - 3]}..."


def _format_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, default=str)
    return str(value)


@dataclass
class DiscordNotifier:
    settings: Settings
    timeout_seconds: float = 3.0

    @property
    def enabled(self) -> bool:
        return self.settings.discord_notifications_enabled and bool(self.settings.discord_webhook_url)

    def send_trade_notification(
        self,
        *,
        action: str,
        signal: TradeSignal,
        proposal: OrderRequest,
        broker_mode: str,
        trading_enabled: bool,
        risk: RiskDecision | None = None,
        order: dict[str, Any] | None = None,
    ) -> None:
        if not self._should_send_trade_action(action):
            return

        quantity = (
            (order or {}).get("quantity")
            or proposal.quantity
            or self._derive_quantity(notional=proposal.notional, price=(order or {}).get("price") or proposal.price)
        )
        price = (order or {}).get("price") or proposal.price or signal.entry_price or signal.price
        timestamp = (
            (order or {}).get("executed_at")
            or getattr(signal, "timestamp", None)
            or self._isoformat(getattr(signal, "generated_at", None))
            or self._isoformat(datetime.now(timezone.utc))
        )
        dry_run = (order or {}).get("is_dry_run", proposal.is_dry_run)
        color = {
            "submitted": 0x2ECC71,
            "dry_run": 0xF1C40F,
            "rejected": 0xE67E22,
        }.get(action, 0x3498DB)
        title = {
            "submitted": "Trade Submitted",
            "dry_run": "Dry Run Trade",
            "rejected": "Trade Rejected",
        }.get(action, "Trade Notification")
        fields = [
            self._field("Symbol", signal.symbol),
            self._field("Asset Class", self._enum_value(getattr(signal, "asset_class", None))),
            self._field("Strategy", getattr(signal, "strategy_name", None)),
            self._field("Signal", self._enum_value(getattr(signal, "signal", None))),
            self._field("Action", action),
            self._field("Quantity", quantity),
            self._field("Price", price),
            self._field("Broker Mode", broker_mode),
            self._field("Trading Enabled", trading_enabled),
            self._field("Dry Run", dry_run),
            self._field("Risk Approved", risk.approved if risk is not None else action != "rejected"),
            self._field("Risk Reason", risk.reason if risk is not None else None, inline=False),
            self._field("Risk Rule", risk.rule if risk is not None else None),
            self._field("Rejection Details", risk.details if risk and risk.details else None, inline=False),
            self._field("Order Status", (order or {}).get("status")),
            self._field("Order Id", (order or {}).get("id") or (order or {}).get("client_order_id")),
            self._field("Timestamp", timestamp, inline=False),
        ]
        description = f"{self._enum_value(signal.signal)} {signal.symbol} via {signal.strategy_name}"
        self._send_embed(
            title=title,
            description=description,
            fields=fields,
            color=color,
            timestamp=timestamp,
        )

    def send_error_notification(
        self,
        *,
        title: str,
        message: str,
        error: Exception | str,
        context: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled or not self.settings.discord_notify_errors:
            return

        details = context or {}
        error_value = f"{error.__class__.__name__}: {error}" if isinstance(error, Exception) else str(error)
        fields = [self._field("Error", error_value, inline=False)]
        fields.extend(self._context_fields(details))
        self._send_embed(
            title=title,
            description=message,
            fields=fields,
            color=0xE74C3C,
            timestamp=self._isoformat(datetime.now(timezone.utc)),
        )

    def send_system_notification(
        self,
        *,
        title: str,
        message: str,
        context: dict[str, Any] | None = None,
        category: str = "general",
    ) -> None:
        if not self.enabled:
            return
        if category == "start_stop" and not self.settings.discord_notify_start_stop:
            return

        self._send_embed(
            title=title,
            description=message,
            fields=self._context_fields(context or {}),
            color=0x3498DB,
            timestamp=self._isoformat(datetime.now(timezone.utc)),
        )

    def _should_send_trade_action(self, action: str) -> bool:
        if not self.enabled:
            return False
        if action == "dry_run":
            return self.settings.discord_notify_dry_runs
        if action == "rejected":
            return self.settings.discord_notify_rejections
        return action == "submitted"

    def _send_embed(
        self,
        *,
        title: str,
        description: str,
        fields: list[dict[str, Any] | None],
        color: int,
        timestamp: str | None,
    ) -> None:
        payload = {
            "content": _truncate(title, _MAX_CONTENT),
            "allowed_mentions": {"parse": []},
            "embeds": [
                {
                    "title": _truncate(title, _MAX_EMBED_TITLE),
                    "description": _truncate(description, _MAX_EMBED_DESCRIPTION),
                    "color": color,
                    "fields": [field for field in fields if field is not None][:25],
                    "timestamp": timestamp,
                }
            ],
        }
        self._post(payload)

    def _post(self, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return

        try:
            response = httpx.post(
                str(self.settings.discord_webhook_url),
                json=payload,
                timeout=httpx.Timeout(self.timeout_seconds),
            )
            response.raise_for_status()
        except Exception as exc:
            logger.warning("Failed to send Discord notification: %s", exc)

    def _field(self, name: str, value: Any, inline: bool = True) -> dict[str, Any] | None:
        formatted = _format_value(value)
        if not formatted:
            return None
        return {
            "name": _truncate(name, _MAX_FIELD_NAME),
            "value": _truncate(formatted, _MAX_FIELD_VALUE),
            "inline": inline,
        }

    def _context_fields(self, context: dict[str, Any]) -> list[dict[str, Any] | None]:
        return [self._field(self._humanize_key(key), value, inline=False) for key, value in context.items()]

    def _derive_quantity(self, *, notional: float | None, price: float | None) -> float | None:
        if notional is None or price is None or price <= 0:
            return None
        return round(notional / price, 6)

    def _enum_value(self, value: Any) -> Any:
        return value.value if hasattr(value, "value") else value

    def _humanize_key(self, value: str) -> str:
        return value.replace("_", " ").title()

    def _isoformat(self, value: datetime | str | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()


_discord_notifier: DiscordNotifier | None = None


def get_discord_notifier(settings: Settings | None = None) -> DiscordNotifier:
    global _discord_notifier

    resolved_settings = settings or get_settings()
    if _discord_notifier is None or _discord_notifier.settings is not resolved_settings:
        _discord_notifier = DiscordNotifier(resolved_settings)
    return _discord_notifier


def reset_discord_notifier() -> None:
    global _discord_notifier

    _discord_notifier = None
