from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
import pytz

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
_MAX_EMBED_TITLE = 256
_MAX_EMBED_DESCRIPTION = 4096


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
    resolved = _resolve_timestamp(value)
    if resolved is None:
        return str(value)
    return resolved.strftime("%Y-%m-%dT%H:%M:%SZ")


def format_mode_label(settings: Settings, *, dry_run: bool = False) -> str:
    if dry_run:
        return "DRY_RUN"
    return "LIVE" if settings.is_live_enabled else "PAPER"


def format_runtime_mode_label(settings: Settings) -> str:
    return "LIVE" if settings.is_alpaca_mode and settings.live_trading_enabled else "PAPER"


def format_readable_notification_timestamp(value: datetime | str | None = None, settings: Settings | None = None) -> str:
    resolved = _resolve_timestamp(value)
    if resolved is None:
        return str(value)
    
    # Apply configured timezone
    configured_settings = settings or get_settings()
    tz_name = configured_settings.discord_timezone or "America/Indiana/Indianapolis"
    try:
        local_tz = pytz.timezone(tz_name)
        local_time = resolved.astimezone(local_tz)
        tz_abbrev = local_time.strftime("%Z")
        return local_time.strftime(f"%Y-%m-%d %H:%M:%S {tz_abbrev}")
    except Exception:
        # Fallback to UTC if timezone is invalid
        return resolved.strftime("%Y-%m-%d %H:%M:%S UTC")


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


@dataclass(frozen=True)
class DiscordMessage:
    content: str | None = None
    embeds: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"allowed_mentions": {"parse": []}}
        if self.content:
            payload["content"] = _truncate(self.content, _MAX_CONTENT)
        if self.embeds:
            payload["embeds"] = self.embeds
        return payload


def build_system_notification_payload(
    *,
    settings: Settings,
    event: str,
    reason: str,
    details: dict[str, Any] | None = None,
    category: str = "general",
    timestamp: datetime | str | None = None,
) -> dict[str, Any]:
    mode_label = format_runtime_mode_label(settings)
    lines: list[str] = []
    if category == "start_stop":
        lines.extend(
            [
                f"Mode: {mode_label}",
                f"Auto-trade: {_enabled_label(settings.auto_trade_enabled)}",
                f"Strategy: {settings.active_strategy}",
                f"Time: {format_readable_notification_timestamp(timestamp)}",
            ]
        )
    else:
        if reason:
            lines.append(reason)
        for key, value in (details or {}).items():
            _append_line(lines, _humanize(key), value)
        lines.append(f"Time: {format_readable_notification_timestamp(timestamp)}")

    return DiscordMessage(
        embeds=[
            {
                "title": _truncate(_system_title(settings=settings, event=event), _MAX_EMBED_TITLE),
                "description": _truncate("\n".join(lines), _MAX_EMBED_DESCRIPTION),
                "color": _system_color(event),
            }
        ]
    ).to_payload()


def build_trade_notification_payload(
    *,
    settings: Settings,
    action: str,
    signal: TradeSignal,
    proposal: OrderRequest,
    risk: RiskDecision | None = None,
    order: dict[str, Any] | None = None,
) -> dict[str, Any]:
    order_payload = order or {}
    side = (_format_scalar(signal.signal) or _format_scalar(proposal.side) or "TRADE").upper()
    symbol = signal.symbol or proposal.symbol
    asset_class = _resolve_trade_asset_class(signal=signal, proposal=proposal)
    quantity = _coalesce(order_payload.get("quantity"), proposal.quantity)
    notional = _coalesce(order_payload.get("notional"), proposal.notional)
    price = _coalesce(order_payload.get("price"), proposal.price, signal.entry_price, signal.price)
    strategy = signal.strategy_name
    order_status = order_payload.get("status")
    order_id = _coalesce(order_payload.get("id"), order_payload.get("client_order_id"))
    timestamp = _coalesce(
        order_payload.get("executed_at"),
        getattr(signal, "timestamp", None),
        getattr(signal, "generated_at", None),
    )

    lines: list[str] = []
    summary = _resolve_trade_summary(action=action, signal=signal, risk=risk)
    if summary:
        lines.append(summary)
    detail_lines: list[str] = []
    if quantity is not None:
        detail_lines.append(f"Qty: {_format_quantity(quantity, asset_class)}")
    elif notional is not None:
        detail_lines.append(f"Notional: {_format_money(notional)}")
    if price is not None:
        detail_lines.append(f"Price: {_format_price(price, asset_class)}")
    if settings.active_strategy:
        detail_lines.append(f"Active Strategy: {settings.active_strategy}")
    if strategy:
        detail_lines.append(f"Strategy: {strategy}")
    tranche_meta = (proposal.metadata or {}).get("tranche") if proposal.metadata else None
    if isinstance(tranche_meta, dict):
        tranche_number = tranche_meta.get("tranche_number")
        tranche_count_total = tranche_meta.get("tranche_count_total")
        if tranche_number and tranche_count_total:
            detail_lines.append(f"Tranche: {int(tranche_number)}/{int(tranche_count_total)}")
        tranche_notional = tranche_meta.get("next_tranche_notional")
        if tranche_notional is not None:
            detail_lines.append(f"Tranche Notional: {_format_money(tranche_notional)}")
        projected_notional = tranche_meta.get("projected_position_notional_after_fill")
        if projected_notional is not None:
            detail_lines.append(f"Post-Fill Position Notional: {_format_money(projected_notional)}")
        remaining_allocation = tranche_meta.get("remaining_planned_allocation")
        if remaining_allocation is not None:
            detail_lines.append(f"Remaining Planned Allocation: {_format_money(remaining_allocation)}")
        scale_in_mode = tranche_meta.get("scale_in_mode")
        if scale_in_mode:
            detail_lines.append(f"Scale-In Mode: {scale_in_mode}")
        decision_reason = tranche_meta.get("decision_reason")
        if decision_reason:
            detail_lines.append(f"Add Reason: {decision_reason}")
    relevant_rule = _format_relevant_rule(risk)
    if relevant_rule:
        detail_lines.append(f"Rule: {relevant_rule}")
    if action == "rejected" and risk is not None:
        detail_lines.extend(_format_rejection_context_lines(risk))
    if order_status:
        detail_lines.append(f"Status: {order_status}")
    if order_id:
        detail_lines.append(f"Order ID: {order_id}")
    detail_lines.append(f"Time: {format_readable_notification_timestamp(timestamp)}")
    if detail_lines:
        if lines:
            lines.append("")
        lines.extend(detail_lines)

    return DiscordMessage(
        embeds=[
            {
                "title": _truncate(
                    _trade_title(settings=settings, action=action, side=side, symbol=symbol),
                    _MAX_EMBED_TITLE,
                ),
                "description": _truncate("\n".join(lines), _MAX_EMBED_DESCRIPTION),
                "color": _trade_color(action),
            }
        ]
    ).to_payload()


def build_error_notification_payload(
    *,
    settings: Settings,
    title: str,
    message: str,
    error: Exception | str,
    context: dict[str, Any] | None = None,
    timestamp: datetime | str | None = None,
) -> dict[str, Any]:
    error_text = f"{error.__class__.__name__}: {error}" if isinstance(error, Exception) else str(error)
    lines = [message, "", f"Error: {error_text}"]
    for key, value in (context or {}).items():
        _append_line(lines, _humanize(key), value)
    lines.append(f"Time: {format_readable_notification_timestamp(timestamp, settings)}")

    return DiscordMessage(
        embeds=[
            {
                "title": _truncate(
                    f"🔴 {format_runtime_mode_label(settings)} | {title}",
                    _MAX_EMBED_TITLE,
                ),
                "description": _truncate("\n".join(lines), _MAX_EMBED_DESCRIPTION),
                "color": 0xE74C3C,
            }
        ]
    ).to_payload()


def build_scan_summary_notification_payload(
    *,
    settings: Settings,
    symbols_scanned: list[str],
    results: list[dict[str, Any]],
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    """Build a scan summary notification payload."""
    ts = timestamp or datetime.now(timezone.utc)
    timestamp_str = format_readable_notification_timestamp(ts, settings)
    
    scanned_count = len(symbols_scanned)
    buy_signals = sum(1 for r in results if r.get("signal") == "BUY")
    sell_signals = sum(1 for r in results if r.get("signal") == "SELL")
    hold_signals = sum(1 for r in results if r.get("signal") == "HOLD")
    
    description_lines = [
        f"**Scan Results**",
        f"Symbols Scanned: {scanned_count}",
        f"BUY Signals: {buy_signals}",
        f"SELL Signals: {sell_signals}",
        f"HOLD Signals: {hold_signals}",
        "",
    ]
    
    # Add top signals summary
    if results:
        description_lines.append("**Top Signals**")
        for i, result in enumerate(results[:5]):
            symbol = result.get("symbol", "?")
            signal = result.get("signal", "?")
            asset_class = result.get("asset_class", "?")
            price = result.get("price", "?")
            reason = result.get("reason", "")[:50]
            description_lines.append(
                f"{i+1}. {symbol} ({asset_class}): {signal} @ {price} - {reason}"
            )
    
    description_lines.append(f"\nTime: {timestamp_str}")
    
    return DiscordMessage(
        embeds=[
            {
                "title": _truncate(
                    f"📊 {format_runtime_mode_label(settings)} | Scan Summary",
                    _MAX_EMBED_TITLE,
                ),
                "description": _truncate("\n".join(description_lines), _MAX_EMBED_DESCRIPTION),
                "color": 0x3498DB,
            }
        ]
    ).to_payload()


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

        payload = build_trade_notification_payload(
            settings=self.settings,
            action=action,
            signal=signal,
            proposal=proposal,
            risk=risk,
            order=order,
        )
        return self._post_payload(payload)

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

        payload = build_error_notification_payload(
            settings=self.settings,
            title=title,
            message=message,
            error=error,
            context=context,
        )
        return self._post_payload(payload)

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

        payload = build_system_notification_payload(
            settings=self.settings,
            event=event,
            reason=reason,
            details=details,
            category=category,
        )
        return self._post_payload(payload)

    def send_scan_summary_notification(
        self,
        *,
        symbols_scanned: list[str],
        results: list[dict[str, Any]],
        timestamp: datetime | None = None,
    ) -> bool:
        """Send a scan summary notification with top results."""
        if not self.enabled or not self.settings.discord_notify_scan_summary:
            return False

        payload = build_scan_summary_notification_payload(
            settings=self.settings,
            symbols_scanned=symbols_scanned,
            results=results,
            timestamp=timestamp or datetime.now(timezone.utc),
        )
        return self._post_payload(payload)

    def _should_send_trade_action(self, action: str) -> bool:
        if not self.enabled:
            return False
        if action == "dry_run":
            return self.settings.discord_notify_dry_runs
        if action == "rejected":
            return self.settings.discord_notify_rejections
        return action == "submitted"

    def _post_payload(self, payload: dict[str, Any]) -> bool:
        if not self.enabled:
            return False

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


def _resolve_timestamp(value: datetime | str | None) -> datetime | None:
    if value is None:
        resolved = datetime.now(timezone.utc)
    elif isinstance(value, str):
        try:
            resolved = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        resolved = value

    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc)


def _enabled_label(value: bool) -> str:
    return "enabled" if value else "disabled"


def _yes_no(value: Any) -> str:
    return "yes" if bool(value) else "no"


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _format_decimal(
    value: float | int,
    *,
    min_decimals: int,
    max_decimals: int,
    use_grouping: bool = False,
) -> str:
    formatter = f",.{max_decimals}f" if use_grouping else f".{max_decimals}f"
    formatted = format(float(value), formatter)
    whole, _, decimals = formatted.partition(".")
    decimals = decimals.rstrip("0")
    if len(decimals) < min_decimals:
        decimals = decimals.ljust(min_decimals, "0")
    return f"{whole}.{decimals}" if decimals else whole


def _format_money(value: Any, *, max_decimals: int = 2) -> str:
    return f"${_format_decimal(float(value), min_decimals=2, max_decimals=max_decimals, use_grouping=True)}"


def _resolve_trade_asset_class(*, signal: TradeSignal, proposal: OrderRequest) -> AssetClass:
    if getattr(signal, "asset_class", AssetClass.UNKNOWN) != AssetClass.UNKNOWN:
        return signal.asset_class
    if proposal.asset_class != AssetClass.UNKNOWN:
        return proposal.asset_class
    return AssetClass.EQUITY


def _format_price(value: Any, asset_class: AssetClass) -> str:
    max_decimals = 6 if asset_class == AssetClass.CRYPTO else 4
    return _format_money(value, max_decimals=max_decimals)


def _format_quantity(value: Any, asset_class: AssetClass) -> str:
    max_decimals = 6 if asset_class == AssetClass.CRYPTO else 4
    return _format_decimal(float(value), min_decimals=0, max_decimals=max_decimals)


def _trade_title(*, settings: Settings, action: str, side: str, symbol: str) -> str:
    emoji = {
        "submitted": "🟢",
        "dry_run": "🟡",
        "rejected": "🟠",
    }.get(action, "ℹ️")
    mode_label = format_runtime_mode_label(settings)
    action_label = {
        "submitted": "submitted",
        "dry_run": "dry run",
        "rejected": "rejected",
    }.get(action, action.replace("_", " "))
    return f"{emoji} {mode_label} | {side} {symbol} {action_label}"


def _trade_color(action: str) -> int:
    return {
        "submitted": 0x2ECC71,
        "dry_run": 0xF1C40F,
        "rejected": 0xE67E22,
    }.get(action, 0x5D6D7E)


def _resolve_trade_summary(
    *,
    action: str,
    signal: TradeSignal,
    risk: RiskDecision | None,
) -> str:
    if action == "rejected" and risk is not None and risk.reason:
        return risk.reason
    if signal.reason:
        return signal.reason
    if risk is not None and risk.reason and risk.rule not in {"approved", "dry_run"}:
        return risk.reason
    return {
        "submitted": "Order submitted.",
        "dry_run": "Dry run only.",
        "rejected": "Order rejected.",
    }.get(action, "Trade update.")


def _format_relevant_rule(risk: RiskDecision | None) -> str | None:
    if risk is None or not risk.rule or risk.rule in {"approved", "general", "dry_run"}:
        return None
    return risk.rule


def _format_rejection_context_lines(risk: RiskDecision) -> list[str]:
    details = risk.details or {}
    lines: list[str] = []
    blocked_reason = details.get("blocked_reason")
    if blocked_reason:
        lines.append(f"Blocked Reason: {blocked_reason}")
    tranche_number = details.get("tranche_number")
    tranche_count_total = details.get("tranche_count_total")
    if tranche_number and tranche_count_total:
        lines.append(f"Attempted Tranche: {int(tranche_number)}/{int(tranche_count_total)}")
    scale_in_mode = details.get("scale_in_mode")
    if scale_in_mode:
        lines.append(f"Scale-In Mode: {scale_in_mode}")
    if str(details.get("side", "")).upper() == "SELL" and "is_risk_reducing_sell" in details:
        lines.append(f"Risk-Reducing Sell: {_yes_no(details.get('is_risk_reducing_sell'))}")
    if "has_tracked_position" in details:
        lines.append(f"Tracked Position: {_yes_no(details.get('has_tracked_position'))}")
    if str(details.get("side", "")).upper() == "SELL" and "tracked_position_sellable" in details:
        lines.append(f"Sellable Long: {_yes_no(details.get('tracked_position_sellable'))}")
    equity = details.get("equity")
    if equity is not None:
        lines.append(f"Equity: {_format_money(equity)}")
    daily_baseline_equity = details.get("daily_baseline_equity")
    if daily_baseline_equity is not None:
        lines.append(f"Daily Baseline: {_format_money(daily_baseline_equity)}")
    daily_loss_pct = details.get("current_daily_loss_pct")
    daily_loss_amount = details.get("current_daily_loss_amount")
    if daily_loss_pct is not None or daily_loss_amount is not None:
        loss_parts: list[str] = []
        if daily_loss_pct is not None:
            loss_parts.append(f"{float(daily_loss_pct):.2%}")
        if daily_loss_amount is not None:
            loss_parts.append(_format_money(daily_loss_amount))
        lines.append(f"Daily Loss: {' / '.join(loss_parts)}")
    raw_calculated_qty = details.get("raw_calculated_qty")
    if raw_calculated_qty is not None:
        lines.append(f"Raw Qty: {_format_decimal(float(raw_calculated_qty), min_decimals=0, max_decimals=6)}")
    rounded_qty = details.get("rounded_qty", details.get("rounded_quantity"))
    if rounded_qty is not None:
        lines.append(f"Rounded Qty: {_format_decimal(float(rounded_qty), min_decimals=0, max_decimals=6)}")
    raw_price = details.get("raw_price")
    if raw_price is not None:
        lines.append(f"Raw Price: {_format_money(raw_price, max_decimals=6)}")
    raw_notional_before_rounding = details.get("raw_notional_before_rounding")
    if raw_notional_before_rounding is not None:
        lines.append(f"Raw Notional: {_format_money(raw_notional_before_rounding, max_decimals=6)}")
    final_submitted_notional = details.get("final_submitted_notional", details.get("rounded_notional"))
    if final_submitted_notional is not None:
        lines.append(f"Final Submitted Notional: {_format_money(final_submitted_notional)}")
    max_allowed_notional = details.get("max_allowed_notional")
    if max_allowed_notional is not None:
        lines.append(f"Max Allowed Notional: {_format_money(max_allowed_notional)}")
    max_position_notional = details.get("max_position_notional")
    if max_position_notional is not None:
        lines.append(f"Max Position Notional: {_format_money(max_position_notional)}")
    hard_max_position_notional = details.get("hard_max_position_notional")
    if hard_max_position_notional is not None:
        lines.append(f"Hard Max Notional: {_format_money(hard_max_position_notional)}")
    comparison_operator = details.get("comparison_operator")
    if comparison_operator:
        lines.append(f"Comparison: {comparison_operator}")
    if "quantity_reduced_to_fit_cap" in details:
        lines.append(f"Qty Reduced To Fit Cap: {_yes_no(details.get('quantity_reduced_to_fit_cap'))}")
    return lines


def _system_title(*, settings: Settings, event: str) -> str:
    emoji = {
        "Bot started": "🔵",
        "Bot stopped": "⚫",
    }.get(event, "🔵")
    return f"{emoji} {format_runtime_mode_label(settings)} | {event}"


def _system_color(event: str) -> int:
    return {
        "Bot started": 0x3498DB,
        "Bot stopped": 0x2C3E50,
    }.get(event, 0x3498DB)


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
