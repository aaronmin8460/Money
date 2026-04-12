from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import threading
import time
from pathlib import Path
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
_DEFAULT_DEDUPE_TTL_SECONDS = 45.0


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


@dataclass(frozen=True)
class NotificationEvent:
    category: str
    action: str
    symbol: str | None = None
    order_id: str | None = None
    cycle_id: str | None = None

    def dedupe_key(self) -> str:
        raw = "|".join(
            [
                self.category,
                self.action,
                self.symbol or "-",
                self.order_id or "-",
                self.cycle_id or "-",
            ]
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


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
                f"Time: {format_readable_notification_timestamp(timestamp, settings)}",
            ]
        )
    else:
        if reason:
            lines.append(reason)
        for key, value in (details or {}).items():
            _append_line(lines, _humanize(key), value)
        lines.append(f"Time: {format_readable_notification_timestamp(timestamp, settings)}")

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
    quantity = _coalesce(order_payload.get("qty"), order_payload.get("quantity"), proposal.quantity)
    filled_quantity = _coalesce(order_payload.get("filled_qty"), order_payload.get("filled_quantity"))
    notional = _coalesce(order_payload.get("notional"), proposal.notional)
    price = _coalesce(
        order_payload.get("filled_avg_price"),
        order_payload.get("avg_fill_price"),
        order_payload.get("price"),
        proposal.price,
        signal.entry_price,
        signal.price,
    )
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
    if action in {"filled", "partially_filled"} and filled_quantity is not None:
        detail_lines.append(f"Qty Filled: {_format_quantity(filled_quantity, asset_class)}")
    elif quantity is not None:
        detail_lines.append(f"Qty: {_format_quantity(quantity, asset_class)}")
    elif notional is not None:
        detail_lines.append(f"Notional: {_format_money(notional)}")
    if price is not None:
        price_label = "Avg Fill Price" if action in {"filled", "partially_filled"} else "Price"
        detail_lines.append(f"{price_label}: {_format_price(price, asset_class)}")
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
    detail_lines.append(f"Time: {format_readable_notification_timestamp(timestamp, settings)}")
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
                    f"{format_runtime_mode_label(settings)} | {title}",
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
    cycle_id: str,
    symbols_evaluated: int,
    outcome_counts: dict[str, int],
    highlights: list[dict[str, Any]],
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    ts = timestamp or datetime.now(timezone.utc)
    timestamp_str = format_readable_notification_timestamp(ts, settings)
    submitted_count = outcome_counts.get("submitted", 0)
    rejected_count = outcome_counts.get("rejected", 0)
    skipped_count = outcome_counts.get("skipped", 0)
    hold_count = outcome_counts.get("hold", 0)

    description_lines = [
        f"Cycle: {cycle_id}",
        f"Symbols Evaluated: {symbols_evaluated}",
        (
            "Outcomes: "
            f"submitted={submitted_count} | "
            f"rejected={rejected_count} | "
            f"skipped={skipped_count} | "
            f"hold={hold_count}"
        ),
    ]

    if highlights:
        description_lines.append("")
        description_lines.append("Highlights:")
        for row in highlights[:5]:
            symbol = str(row.get("symbol") or "?")
            action = str(row.get("action") or "unknown")
            reason = _truncate(str(row.get("reason") or row.get("decision_reason") or ""), 90)
            if reason:
                description_lines.append(f"- {symbol} {action}: {reason}")
            else:
                description_lines.append(f"- {symbol} {action}")

    description_lines.append("")
    description_lines.append(f"Time: {timestamp_str}")

    return DiscordMessage(
        embeds=[
            {
                "title": _truncate(
                    f"{format_runtime_mode_label(settings)} | Scan summary",
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
    dedupe_ttl_seconds: float = _DEFAULT_DEDUPE_TTL_SECONDS
    _recent_events: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _cache_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _dedupe_suppressed_count: int = field(default=0, init=False)
    _sent_event_count: int = field(default=0, init=False)
    _persistent_cache_loaded: bool = field(default=False, init=False, repr=False)

    @property
    def enabled(self) -> bool:
        return self.settings.discord_notifications_enabled and bool(self.settings.discord_webhook_url)

    def __post_init__(self) -> None:
        self.dedupe_ttl_seconds = float(self.settings.discord_dedupe_ttl_seconds)

    def send_trade_notification(
        self,
        *,
        action: str,
        signal: TradeSignal,
        proposal: OrderRequest,
        risk: RiskDecision | None = None,
        order: dict[str, Any] | None = None,
        dedupe_event: NotificationEvent | None = None,
    ) -> bool:
        if not self._should_send_trade_action(action):
            return False

        resolved_order = order or {}
        resolved_order_id = str(resolved_order.get("id") or resolved_order.get("client_order_id") or "")
        event = dedupe_event or NotificationEvent(
            category="trade_attempt",
            action=action,
            symbol=signal.symbol or proposal.symbol,
            order_id=resolved_order_id or None,
        )
        payload = build_trade_notification_payload(
            settings=self.settings,
            action=action,
            signal=signal,
            proposal=proposal,
            risk=risk,
            order=order,
        )
        return self._post_payload(payload, dedupe_event=event)

    def send_broker_lifecycle_notification(
        self,
        *,
        status: str,
        order: dict[str, Any],
        strategy_name: str | None = None,
    ) -> bool:
        if not self.enabled:
            return False

        normalized_status = _normalize_broker_status(status)
        if normalized_status is None:
            return False
        if normalized_status == "rejected" and not self.settings.discord_notify_rejections:
            return False

        order_id = str(order.get("id") or order.get("client_order_id") or "")
        symbol = str(order.get("symbol") or "?").upper()
        side = str(order.get("side") or "TRADE").upper()
        price = _coalesce(order.get("filled_avg_price"), order.get("avg_fill_price"), order.get("price"))
        qty_submitted = _coalesce(order.get("qty"), order.get("quantity"))
        qty_filled = _coalesce(order.get("filled_qty"), order.get("quantity"), order.get("qty"))
        timestamp = _coalesce(order.get("filled_at"), order.get("updated_at"), order.get("submitted_at"))

        lines = [_broker_lifecycle_summary(normalized_status)]
        if normalized_status in {"filled", "partially_filled"} and qty_filled is not None:
            lines.append(f"Qty Filled: {_format_decimal(float(qty_filled), min_decimals=0, max_decimals=6)}")
        elif qty_submitted is not None:
            lines.append(f"Qty: {_format_decimal(float(qty_submitted), min_decimals=0, max_decimals=6)}")
        if price is not None:
            lines.append(f"Avg Fill Price: {_format_money(price, max_decimals=6)}")
        if strategy_name:
            lines.append(f"Strategy: {strategy_name}")
        if order_id:
            lines.append(f"Order ID: {order_id}")
        lines.append(f"Time: {format_readable_notification_timestamp(timestamp, self.settings)}")

        payload = DiscordMessage(
            embeds=[
                {
                    "title": _truncate(
                        f"{format_runtime_mode_label(self.settings)} | {side} {symbol} {normalized_status}",
                        _MAX_EMBED_TITLE,
                    ),
                    "description": _truncate("\n".join(lines), _MAX_EMBED_DESCRIPTION),
                    "color": _trade_color(normalized_status),
                }
            ]
        ).to_payload()
        return self._post_payload(
            payload,
            dedupe_event=NotificationEvent(
                category="broker_lifecycle",
                action=normalized_status,
                symbol=symbol,
                order_id=order_id or None,
            ),
        )

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
        return self._post_payload(
            payload,
            dedupe_event=NotificationEvent(category="error", action=title.strip().lower().replace(" ", "_")),
        )

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
        return self._post_payload(
            payload,
            dedupe_event=NotificationEvent(
                category=category,
                action=f"{event.strip().lower().replace(' ', '_')}:{reason.strip().lower()}",
            ),
        )

    def send_scan_summary_notification(
        self,
        *,
        cycle_id: str,
        symbols_evaluated: int,
        outcome_counts: dict[str, int],
        highlights: list[dict[str, Any]],
        timestamp: datetime | None = None,
    ) -> bool:
        if not self.enabled or not self.settings.discord_notify_scan_summary:
            return False

        payload = build_scan_summary_notification_payload(
            settings=self.settings,
            cycle_id=cycle_id,
            symbols_evaluated=symbols_evaluated,
            outcome_counts=outcome_counts,
            highlights=highlights,
            timestamp=timestamp or datetime.now(timezone.utc),
        )
        return self._post_payload(
            payload,
            dedupe_event=NotificationEvent(category="scan_summary", action="cycle_summary", cycle_id=cycle_id),
        )

    def diagnostics(self) -> dict[str, Any]:
        with self._cache_lock:
            return {
                "enabled": self.enabled,
                "recent_event_cache_size": len(self._recent_events),
                "dedupe_ttl_seconds": self.dedupe_ttl_seconds,
                "dedupe_suppressed_count": self._dedupe_suppressed_count,
                "sent_event_count": self._sent_event_count,
            }

    def _should_send_trade_action(self, action: str) -> bool:
        if not self.enabled:
            return False
        normalized = action.strip().lower()
        if normalized in {"hold", "skipped"}:
            return False
        if normalized == "dry_run":
            return self.settings.discord_notify_dry_runs
        if normalized == "rejected":
            return self.settings.discord_notify_rejections
        return normalized in {
            "submitted",
            "accepted",
            "partially_filled",
            "filled",
            "canceled",
        }

    def _post_payload(
        self,
        payload: dict[str, Any],
        *,
        dedupe_event: NotificationEvent | None = None,
    ) -> bool:
        if not self.enabled:
            return False

        if dedupe_event is not None and self._is_duplicate(dedupe_event.dedupe_key()):
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
            if dedupe_event is not None:
                self._remember_event(dedupe_event.dedupe_key())
            with self._cache_lock:
                self._sent_event_count += 1
            return True

        body = _truncate((response.text or "").replace("\n", " ").strip() or "<empty>", _MAX_ERROR_BODY)
        logger.warning(
            "Discord webhook returned status %s for %s: %s",
            response.status_code,
            target,
            body,
        )
        return False

    def _is_duplicate(self, event_key: str) -> bool:
        now = time.time()
        with self._cache_lock:
            self._load_persistent_cache()
            self._purge_expired(now)
            seen_at = self._recent_events.get(event_key)
            if seen_at is None:
                return False
            if now - seen_at <= self.dedupe_ttl_seconds:
                self._dedupe_suppressed_count += 1
                return True
            return False

    def _remember_event(self, event_key: str) -> None:
        now = time.time()
        with self._cache_lock:
            self._load_persistent_cache()
            self._purge_expired(now)
            self._recent_events[event_key] = now
            self._persist_cache()

    def _purge_expired(self, now: float) -> None:
        expired = [
            key
            for key, seen_at in self._recent_events.items()
            if now - seen_at > self.dedupe_ttl_seconds
        ]
        for key in expired:
            self._recent_events.pop(key, None)
        if expired:
            self._persist_cache()

    def _cache_path(self) -> Path:
        return Path(self.settings.log_dir) / "discord_dedupe.json"

    def _load_persistent_cache(self) -> None:
        if self._persistent_cache_loaded:
            return
        cache_path = self._cache_path()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        if cache_path.exists():
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                self._recent_events.update(
                    {
                        str(key): float(value)
                        for key, value in payload.items()
                        if isinstance(key, str) and isinstance(value, (int, float))
                    }
                )
        self._persistent_cache_loaded = True

    def _persist_cache(self) -> None:
        cache_path = self._cache_path()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(self._recent_events), encoding="utf-8")


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
    mode_label = format_runtime_mode_label(settings)
    action_label = action.replace("_", " ")
    return f"{mode_label} | {side} {symbol} {action_label}"


def _trade_color(action: str) -> int:
    return {
        "submitted": 0x2ECC71,
        "accepted": 0x2ECC71,
        "partially_filled": 0x1ABC9C,
        "filled": 0x27AE60,
        "canceled": 0x95A5A6,
        "dry_run": 0xF1C40F,
        "rejected": 0xE67E22,
        "skipped": 0x95A5A6,
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
        "accepted": "Accepted by broker.",
        "partially_filled": "Partially filled.",
        "filled": "Order filled.",
        "canceled": "Order canceled.",
        "dry_run": "Dry run only.",
        "rejected": "Order rejected.",
        "skipped": "Trade opportunity skipped.",
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
    return f"{format_runtime_mode_label(settings)} | {event}"


def _system_color(event: str) -> int:
    return {
        "Bot started": 0x3498DB,
        "Bot stopped": 0x2C3E50,
        "Paper auto-trader started": 0x3498DB,
        "Paper auto-trader stopped": 0x2C3E50,
        "Scan summary": 0x3498DB,
    }.get(event, 0x3498DB)


def _normalize_broker_status(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    mappings = {
        "new": "accepted",
        "accepted": "accepted",
        "pending_new": "accepted",
        "pending_replace": "accepted",
        "accepted_for_bidding": "accepted",
        "partially_filled": "partially_filled",
        "filled": "filled",
        "canceled": "canceled",
        "expired": "canceled",
        "done_for_day": "canceled",
        "rejected": "rejected",
        "suspended": "rejected",
    }
    return mappings.get(normalized)


def _broker_lifecycle_summary(status: str) -> str:
    return {
        "accepted": "Accepted by broker.",
        "partially_filled": "Order partially filled.",
        "filled": "Order filled.",
        "canceled": "Order canceled.",
        "rejected": "Order rejected by broker.",
    }.get(status, "Broker order update.")


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
