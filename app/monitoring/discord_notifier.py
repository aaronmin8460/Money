from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from app.config.settings import Settings, get_settings
from app.domain.models import AssetClass
from app.monitoring.logger import get_logger

if TYPE_CHECKING:
    from app.risk.risk_manager import RiskDecision
    from app.services.broker import OrderRequest
    from app.strategies.base import TradeSignal


logger = get_logger("discord")

_MAX_CONTENT = 2000
_MAX_ERROR_BODY = 500


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 3:
        return value[:limit]
    return f"{value[: limit - 3]}..."


def _format_scalar(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "value"):
        return str(value.value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def _humanize(label: str) -> str:
    return label.replace("_", " ").title()


def format_notification_timestamp(value: datetime | str | None = None) -> str:
    if value is None:
        resolved = datetime.now(timezone.utc)
    elif isinstance(value, str):
        try:
            resolved = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    else:
        resolved = value

    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_mode_label(settings: Settings, *, dry_run: bool = False) -> str:
    if dry_run:
        return "DRY_RUN"
    return "LIVE" if settings.is_live_enabled else "PAPER"


def sanitize_webhook_target(webhook_url: str | None) -> str:
    if not webhook_url:
        return "<missing webhook>"

    parsed = urlparse(webhook_url)
    host = parsed.netloc or "<invalid host>"
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) >= 4 and segments[0] == "api" and segments[1] == "webhooks":
        webhook_id = _mask_identifier(segments[2])
        return f"{host}/api/webhooks/{webhook_id}/***"
    path = parsed.path or "/"
    return f"{host}{_truncate(path, 80)}"


def build_system_notification_message(
    *,
    mode_label: str,
    event: str,
    reason: str,
    details: dict[str, Any] | None = None,
    timestamp: datetime | str | None = None,
) -> str:
    lines = [
        f"[Money Bot][{mode_label}]",
        event,
        f"Reason: {reason}",
    ]
    for key, value in (details or {}).items():
        _append_line(lines, _humanize(key), value)
    lines.append(f"Time: {format_notification_timestamp(timestamp)}")
    return "\n".join(lines)


def build_trade_notification_message(
    *,
    settings: Settings,
    action: str,
    signal: TradeSignal,
    proposal: OrderRequest,
    risk: RiskDecision | None = None,
    order: dict[str, Any] | None = None,
) -> str:
    order_payload = order or {}
    dry_run = action == "dry_run" or bool(order_payload.get("is_dry_run")) or proposal.is_dry_run
    mode_label = format_mode_label(settings, dry_run=dry_run)
    symbol = signal.symbol or proposal.symbol
    asset_class = _resolve_asset_class(signal=signal, proposal=proposal)
    side = _format_scalar(signal.signal) or _format_scalar(proposal.side)
    quantity = order_payload.get("quantity") or proposal.quantity
    notional = order_payload.get("notional") or proposal.notional
    price = order_payload.get("price") or proposal.price or signal.entry_price or signal.price
    strategy = signal.strategy_name
    order_status = order_payload.get("status")
    order_id = order_payload.get("id") or order_payload.get("client_order_id")
    timestamp = (
        order_payload.get("executed_at")
        or getattr(signal, "timestamp", None)
        or getattr(signal, "generated_at", None)
    )
    title = {
        "submitted": "Trade executed",
        "dry_run": "Dry run trade",
        "rejected": "Trade rejected",
    }.get(action, "Trade notification")

    lines = [
        f"[Money Bot][{mode_label}]",
        title,
    ]
    _append_line(lines, "Symbol", symbol)
    _append_line(lines, "Asset Class", asset_class)
    _append_line(lines, "Side", side)
    if quantity is not None:
        _append_line(lines, "Quantity", quantity)
    else:
        _append_line(lines, "Notional", notional)
    _append_line(lines, "Price", price)
    _append_line(lines, "Strategy", strategy)
    _append_line(lines, "Action", action.upper())
    _append_line(lines, "Order Status", order_status)
    _append_line(lines, "Order ID", order_id)
    if action == "rejected":
        _append_line(lines, "Risk Reason", risk.reason if risk is not None else None)
    lines.append(f"Time: {format_notification_timestamp(timestamp)}")
    return "\n".join(lines)


def build_error_notification_message(
    *,
    mode_label: str,
    title: str,
    message: str,
    error: Exception | str,
    context: dict[str, Any] | None = None,
    timestamp: datetime | str | None = None,
) -> str:
    error_text = f"{error.__class__.__name__}: {error}" if isinstance(error, Exception) else str(error)
    lines = [
        f"[Money Bot][{mode_label}]",
        title,
        f"Reason: {message}",
        f"Error: {error_text}",
    ]
    for key, value in (context or {}).items():
        _append_line(lines, _humanize(key), value)
    lines.append(f"Time: {format_notification_timestamp(timestamp)}")
    return "\n".join(lines)


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
        risk: RiskDecision | None = None,
        order: dict[str, Any] | None = None,
    ) -> bool:
        if not self._should_send_trade_action(action):
            return False

        content = build_trade_notification_message(
            settings=self.settings,
            action=action,
            signal=signal,
            proposal=proposal,
            risk=risk,
            order=order,
        )
        return self._post_content(content)

    def send_error_notification(
        self,
        *,
        title: str,
        message: str,
        error: Exception | str,
        context: dict[str, Any] | None = None,
    ) -> bool:
        if not self.enabled or not self.settings.discord_notify_errors:
            return False

        content = build_error_notification_message(
            mode_label=format_mode_label(self.settings),
            title=title,
            message=message,
            error=error,
            context=context,
        )
        return self._post_content(content)

    def send_system_notification(
        self,
        *,
        event: str,
        reason: str,
        details: dict[str, Any] | None = None,
        category: str = "general",
    ) -> bool:
        if not self.enabled:
            return False
        if category == "start_stop" and not self.settings.discord_notify_start_stop:
            return False

        content = build_system_notification_message(
            mode_label=format_mode_label(self.settings),
            event=event,
            reason=reason,
            details=details,
        )
        return self._post_content(content)

    def _should_send_trade_action(self, action: str) -> bool:
        if not self.enabled:
            return False
        if action == "dry_run":
            return self.settings.discord_notify_dry_runs
        if action == "rejected":
            return self.settings.discord_notify_rejections
        return action == "submitted"

    def _post_content(self, content: str) -> bool:
        if not self.enabled:
            return False

        payload = {
            "content": _truncate(content, _MAX_CONTENT),
            "allowed_mentions": {"parse": []},
        }
        target = sanitize_webhook_target(str(self.settings.discord_webhook_url))

        try:
            response = httpx.post(
                str(self.settings.discord_webhook_url),
                json=payload,
                timeout=httpx.Timeout(self.timeout_seconds),
            )
        except Exception as exc:
            logger.warning("Discord notification request failed for %s: %s", target, exc)
            return False

        if 200 <= response.status_code < 300:
            return True

        body = _truncate((response.text or "").replace("\n", " ").strip() or "<empty>", _MAX_ERROR_BODY)
        logger.warning(
            "Discord webhook returned status %s for %s: %s",
            response.status_code,
            target,
            body,
        )
        return False


def _append_line(lines: list[str], label: str, value: Any) -> None:
    formatted = _format_scalar(value)
    if formatted:
        lines.append(f"{label}: {formatted}")


def _resolve_asset_class(*, signal: TradeSignal, proposal: OrderRequest) -> str | None:
    if getattr(signal, "asset_class", AssetClass.UNKNOWN) != AssetClass.UNKNOWN:
        return _format_scalar(signal.asset_class)
    if proposal.asset_class != AssetClass.UNKNOWN:
        return _format_scalar(proposal.asset_class)
    return None


def _mask_identifier(value: str) -> str:
    if len(value) <= 8:
        return f"{value[:2]}***"
    return f"{value[:4]}...{value[-4:]}"


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
