from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from app.config.settings import Settings, get_settings
from app.db.models import RiskEvent as RiskEventRecord
from app.db.session import SessionLocal
from app.domain.models import AssetClass
from app.monitoring.logger import get_logger
from app.portfolio.portfolio import Portfolio
from app.services.market_data import normalize_asset_class

logger = get_logger("risk")


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    rule: str = "general"
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "reason": self.reason,
            "rule": self.rule,
            "details": dict(self.details),
        }


class RiskManager:
    def __init__(
        self,
        portfolio: Portfolio,
        settings: Settings | None = None,
        broker: Any | None = None,
    ):
        self.settings = settings or get_settings()
        self.portfolio = portfolio
        self.broker = broker
        self._symbol_cooldowns: dict[str, datetime] = {}
        self._strategy_cooldowns: dict[str, datetime] = {}
        self._recent_rejections: list[dict[str, Any]] = []
        self._latest_rejection: dict[str, Any] | None = None

    def get_account_snapshot(self) -> dict[str, float]:
        if self.broker is not None:
            try:
                account = self.broker.get_account()
                return {
                    "cash": float(account.cash),
                    "equity": float(account.equity),
                    "buying_power": float(account.buying_power),
                }
            except Exception:
                pass

        equity = self.portfolio.calculate_equity()
        return {
            "cash": float(self.portfolio.cash),
            "equity": float(equity),
            "buying_power": float(self.portfolio.cash),
        }

    def get_runtime_snapshot(self) -> dict[str, Any]:
        account = self.get_account_snapshot()
        current_daily_loss_amount = self.portfolio.current_daily_loss_amount(equity=account["equity"])
        current_daily_loss_pct = self.portfolio.current_daily_loss_pct(equity=account["equity"])
        return {
            "trading_enabled": self.settings.trading_enabled,
            "auto_trade_enabled": self.settings.auto_trade_enabled,
            "live_trading_enabled": self.settings.live_trading_enabled,
            "kill_switch_enabled": self.settings.kill_switch_enabled,
            "short_selling_enabled": self.settings.short_selling_enabled,
            "broker_mode": self.settings.broker_mode,
            "broker_backend": self.settings.broker_backend,
            "active_strategy": self.settings.active_strategy,
            "allow_extended_hours": self.settings.allow_extended_hours,
            "cash": account["cash"],
            "equity": account["equity"],
            "buying_power": account["buying_power"],
            "open_positions_count": len(self.portfolio.positions),
            "positions_by_asset_class": self.portfolio.position_counts_by_asset_class(),
            "exposure": self.portfolio.exposure(),
            "exposure_by_asset_class": self.portfolio.exposure_by_asset_class(),
            "risk_events": list(self.portfolio.risk_events),
            "drawdown_pct": self.portfolio.drawdown_pct(),
            "daily_baseline_equity": self.portfolio.daily_baseline_equity,
            "daily_baseline_date": (
                self.portfolio.daily_baseline_date.isoformat()
                if self.portfolio.daily_baseline_date is not None
                else None
            ),
            "daily_loss_amount": current_daily_loss_amount,
            "daily_loss_pct": current_daily_loss_pct,
            "active_cooldowns": self.get_active_cooldowns(),
            "latest_rejection": self._latest_rejection,
        }

    def get_active_cooldowns(self) -> dict[str, list[dict[str, Any]]]:
        now = datetime.utcnow()
        symbol_cooldowns = [
            {
                "symbol": symbol,
                "expires_at": expires_at.isoformat() + "Z",
                "remaining_seconds": max(0.0, (expires_at - now).total_seconds()),
            }
            for symbol, expires_at in sorted(self._symbol_cooldowns.items())
            if expires_at > now
        ]
        strategy_cooldowns = [
            {
                "strategy_name": strategy_name,
                "expires_at": expires_at.isoformat() + "Z",
                "remaining_seconds": max(0.0, (expires_at - now).total_seconds()),
            }
            for strategy_name, expires_at in sorted(self._strategy_cooldowns.items())
            if expires_at > now
        ]
        return {
            "symbols": symbol_cooldowns,
            "strategies": strategy_cooldowns,
        }

    def get_recent_rejections(self, limit: int = 10) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        return list(self._recent_rejections[-limit:])

    def get_rejection_snapshot(self, limit: int = 10) -> dict[str, Any]:
        return {
            "latest": self._latest_rejection,
            "recent": self.get_recent_rejections(limit=limit),
        }

    def clear_runtime_state(self) -> None:
        self._symbol_cooldowns.clear()
        self._strategy_cooldowns.clear()
        self._recent_rejections.clear()
        self._latest_rejection = None

    def get_diagnostics(self, *, limit: int = 10) -> dict[str, Any]:
        snapshot = self.get_runtime_snapshot()
        snapshot.update(
            {
                "current_daily_loss_amount": snapshot["daily_loss_amount"],
                "current_daily_loss_pct": snapshot["daily_loss_pct"],
                "latest_risk_events": list(self.portfolio.risk_events[-limit:]),
                "recent_rejections": self.get_recent_rejections(limit=limit),
            }
        )
        return snapshot

    def evaluate_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        stop_price: float | None = None,
        *,
        asset_class: AssetClass | str | None = None,
        strategy_name: str | None = None,
        spread_pct: float | None = None,
        quote_bid: float | None = None,
        quote_ask: float | None = None,
        quote_mid: float | None = None,
        quote_timestamp: str | None = None,
        quote_age_seconds: float | None = None,
        quote_available: bool | None = None,
        quote_stale: bool | None = None,
        spread_abs: float | None = None,
        price_source_used: str | None = None,
        fallback_pricing_used: bool | None = None,
        avg_volume: float | None = None,
        dollar_volume: float | None = None,
        data_age_seconds: float | None = None,
        exchange: str | None = None,
        sizing: dict[str, Any] | None = None,
    ) -> RiskDecision:
        normalized_side = side.value if hasattr(side, "value") else str(side)
        normalized_side = normalized_side.upper()
        resolved_asset_class = normalize_asset_class(asset_class)
        if resolved_asset_class == AssetClass.UNKNOWN:
            resolved_asset_class = AssetClass.EQUITY
        reduces_exposure = self._is_risk_reducing_sell(symbol, normalized_side, quantity)
        account = self.get_account_snapshot()
        quantity_decimal = self._decimal(quantity)
        price_decimal = self._decimal(price)
        decision_details = self._build_decision_details(
            symbol=symbol,
            side=normalized_side,
            quantity=quantity,
            price=price,
            account=account,
            asset_class=resolved_asset_class,
            strategy_name=strategy_name,
            spread_pct=spread_pct,
            quote_bid=quote_bid,
            quote_ask=quote_ask,
            quote_mid=quote_mid,
            quote_timestamp=quote_timestamp,
            quote_age_seconds=quote_age_seconds,
            quote_available=quote_available,
            quote_stale=quote_stale,
            spread_abs=spread_abs,
            price_source_used=price_source_used,
            fallback_pricing_used=fallback_pricing_used,
            avg_volume=avg_volume,
            dollar_volume=dollar_volume,
            data_age_seconds=data_age_seconds,
            exchange=exchange,
            reduces_exposure=reduces_exposure,
            sizing=sizing,
        )

        if self.settings.kill_switch_enabled:
            return RiskDecision(False, "Hard kill switch is enabled.", rule="kill_switch", details=decision_details)

        if not self.settings.trading_enabled:
            return RiskDecision(
                True,
                "Trading is disabled. The order will be evaluated as a dry-run.",
                rule="dry_run",
                details=decision_details,
            )

        if not self._asset_class_enabled(resolved_asset_class):
            return RiskDecision(
                False,
                f"Trading is disabled for asset class '{resolved_asset_class.value}'.",
                rule="asset_class_disabled",
                details=decision_details,
            )

        if normalized_side == "SELL" and not reduces_exposure:
            if not self.settings.short_selling_enabled:
                return RiskDecision(
                    False,
                    "No tracked long position to sell while short selling is disabled.",
                    rule="no_position_to_sell",
                    details=decision_details,
                )
            return RiskDecision(
                False,
                "Short selling is enabled in settings, but true short entries are not implemented end-to-end.",
                rule="short_selling_not_supported",
                details=decision_details,
            )

        if quantity_decimal <= 0 or price_decimal <= 0:
            return RiskDecision(False, "Invalid order quantity or price.", rule="input_validation", details=decision_details)

        if not reduces_exposure:
            drawdown_pct = self.portfolio.drawdown_pct()
            if drawdown_pct >= self.settings.max_drawdown_pct:
                return RiskDecision(
                    False,
                    f"Max drawdown ({drawdown_pct:.2%}) exceeded ({self.settings.max_drawdown_pct:.2%}).",
                    rule="drawdown_limit",
                    details=decision_details,
                )

            daily_loss_pct = self.portfolio.current_daily_loss_pct(equity=account["equity"])
            if daily_loss_pct >= self.settings.max_daily_loss_pct:
                return RiskDecision(
                    False,
                    f"Max daily loss ({daily_loss_pct:.2%}) reached ({self.settings.max_daily_loss_pct:.2%}).",
                    rule="daily_loss_pct_limit",
                    details=decision_details,
                )

            current_loss = self.portfolio.current_daily_loss_amount(equity=account["equity"])
            if current_loss >= self.settings.max_daily_loss:
                return RiskDecision(
                    False,
                    f"Max daily loss notional ({current_loss:.2f}) reached ({self.settings.max_daily_loss:.2f}).",
                    rule="daily_loss_limit",
                    details=decision_details,
                )

        if data_age_seconds is not None and data_age_seconds > self.settings.data_stale_after_seconds:
            return RiskDecision(False, "Market data is stale.", rule="stale_data", details=decision_details)

        normalized_quote_available = bool(quote_available)
        normalized_quote_stale = bool(quote_stale)
        if spread_pct is not None:
            decision_details["spread_pct"] = spread_pct
        if normalized_quote_stale and not reduces_exposure:
            return RiskDecision(
                False,
                "Quote data is stale and cannot be used for safe spread validation.",
                rule="stale_quote",
                details=decision_details,
            )
        if normalized_quote_available and spread_pct is not None and spread_pct > self.settings.max_spread_pct:
            return RiskDecision(
                False,
                (
                    f"Spread exceeds configured limit using bid/ask quote data "
                    f"(bid={quote_bid}, ask={quote_ask}, spread_pct={spread_pct:.6f})."
                ),
                rule="spread_limit",
                details=decision_details,
            )
        if not normalized_quote_available:
            decision_details["spread_check_skipped"] = True
            decision_details["spread_skip_reason"] = "quotes_unavailable"

        if resolved_asset_class != AssetClass.CRYPTO and price < self.settings.min_price:
            return RiskDecision(False, "Price below configured minimum.", rule="min_price", details=decision_details)

        if resolved_asset_class != AssetClass.CRYPTO and avg_volume is not None and avg_volume < self.settings.min_avg_volume:
            return RiskDecision(False, "Average volume below configured minimum.", rule="avg_volume", details=decision_details)

        if resolved_asset_class != AssetClass.CRYPTO and dollar_volume is not None and dollar_volume < self.settings.min_dollar_volume:
            return RiskDecision(False, "Dollar volume below configured minimum.", rule="dollar_volume", details=decision_details)

        cooldown = self._symbol_cooldowns.get(symbol.upper())
        if cooldown and datetime.utcnow() < cooldown:
            return RiskDecision(False, "Symbol cooldown is active.", rule="symbol_cooldown", details=decision_details)

        if strategy_name:
            strategy_key = strategy_name.lower()
            strategy_cooldown = self._strategy_cooldowns.get(strategy_key)
            if strategy_cooldown and datetime.utcnow() < strategy_cooldown:
                return RiskDecision(False, "Strategy cooldown is active.", rule="strategy_cooldown", details=decision_details)

        order_notional = self._round_money(quantity_decimal * price_decimal)
        if decision_details.get("final_submitted_notional") is not None:
            order_notional = self._round_money(self._decimal(decision_details["final_submitted_notional"]))
        allow_duplicate_buy_for_scale_in = bool(decision_details.get("allow_duplicate_buy_for_scale_in"))
        tranche_consumes_new_slot = bool(decision_details.get("tranche_consumes_new_slot", True))
        if normalized_side == "BUY":
            if symbol in self.portfolio.positions and not allow_duplicate_buy_for_scale_in:
                return RiskDecision(
                    False,
                    "Duplicate buy order blocked for existing position.",
                    rule="duplicate_position",
                    details=decision_details,
                )

            max_positions = self.settings.max_positions_total
            if tranche_consumes_new_slot:
                if len(self.portfolio.positions) >= max_positions:
                    return RiskDecision(
                        False,
                        f"Maximum simultaneous positions ({max_positions}) reached.",
                        rule="position_count",
                        details=decision_details,
                    )

            positions_by_class = self.portfolio.position_counts_by_asset_class()
            class_limit = int(
                self.settings.max_positions_per_asset_class.get(
                    resolved_asset_class.value,
                    max_positions,
                )
            )
            if tranche_consumes_new_slot and positions_by_class.get(resolved_asset_class.value, 0) >= class_limit:
                return RiskDecision(
                    False,
                    f"Maximum positions for asset class '{resolved_asset_class.value}' reached.",
                    rule="asset_class_position_count",
                    details=decision_details,
                )

            class_exposure = self._decimal(self.portfolio.exposure_by_asset_class().get(resolved_asset_class.value, 0.0))
            class_limit = self._decimal(
                self.settings.max_notional_per_asset_class.get(
                    resolved_asset_class.value,
                    self.settings.max_total_exposure,
                )
            )
            if class_exposure + order_notional > class_limit:
                return RiskDecision(
                    False,
                    "Asset-class notional limit would be exceeded.",
                    rule="asset_class_notional",
                    details=decision_details,
                )

            total_exposure = self._decimal(self.portfolio.exposure())
            max_total_exposure = self._decimal(self.settings.max_total_exposure)
            if total_exposure + order_notional > max_total_exposure:
                return RiskDecision(False, "Max total exposure would be exceeded.", rule="total_exposure", details=decision_details)

            max_position_notional = self._decimal(self.settings.max_position_notional)
            comparison_operator = decision_details.get("comparison_operator", ">")
            if order_notional > max_position_notional:
                submitted_notional_text = str(order_notional.normalize())
                max_notional_text = str(max_position_notional.normalize())
                return RiskDecision(
                    False,
                    (
                        f"Final submitted notional ({submitted_notional_text}) exceeds max position notional "
                        f"({max_notional_text}) using comparison '{comparison_operator}'."
                    ),
                    rule="position_notional",
                    details=decision_details,
                )

            if tranche_consumes_new_slot:
                correlated_open_positions = sum(
                    1
                    for position in self.portfolio.positions.values()
                    if position.asset_class == resolved_asset_class
                    and (exchange is None or position.exchange == exchange)
                )
                if correlated_open_positions >= self.settings.max_correlated_positions:
                    return RiskDecision(
                        False,
                        "Correlated position limit reached for this asset class/exchange group.",
                        rule="correlated_positions",
                        details=decision_details,
                    )

            if self.settings.is_simulated_mode and order_notional > self._decimal(account["cash"]):
                return RiskDecision(
                    False,
                    f"Order notional ({float(order_notional):.2f}) exceeds available cash ({account['cash']:.2f}).",
                    rule="cash_limit",
                    details=decision_details,
                )

            if order_notional > self._decimal(account["buying_power"]):
                return RiskDecision(
                    False,
                    f"Order notional ({float(order_notional):.2f}) exceeds buying power ({account['buying_power']:.2f}).",
                    rule="buying_power",
                    details=decision_details,
                )

            if stop_price is not None:
                risk_per_share = price - stop_price
                if risk_per_share <= 0:
                    return RiskDecision(
                        False,
                        "Stop price must be below entry price for a long position.",
                        rule="stop_validation",
                        details=decision_details,
                    )

                trade_risk = self._round_money(quantity_decimal * self._decimal(risk_per_share))
                max_trade_risk = self._round_money(
                    self._decimal(account["equity"]) * self._decimal(self.settings.max_risk_per_trade)
                )
                if trade_risk > max_trade_risk:
                    return RiskDecision(
                        False,
                        f"Stop-based trade risk ({float(trade_risk):.2f}) exceeds max risk per trade ({float(max_trade_risk):.2f}).",
                        rule="trade_risk",
                        details=decision_details,
                    )

        if (
            self.settings.is_alpaca_mode
            and resolved_asset_class != AssetClass.CRYPTO
            and not self.settings.allow_extended_hours
        ):
            broker = self.broker
            if broker is not None and hasattr(broker, "is_market_open") and not broker.is_market_open(resolved_asset_class):
                return RiskDecision(False, "Market is closed and extended hours not allowed.", rule="market_hours", details=decision_details)

        return RiskDecision(True, "Order approved by risk manager.", rule="approved", details=decision_details)

    def mark_executed(self, symbol: str, strategy_name: str | None = None) -> None:
        self._symbol_cooldowns[symbol.upper()] = datetime.utcnow() + timedelta(
            seconds=self.settings.cooldown_seconds_per_symbol
        )
        if strategy_name:
            self._strategy_cooldowns[strategy_name.lower()] = datetime.utcnow() + timedelta(
                seconds=self.settings.cooldown_seconds_per_strategy
            )

    def _asset_class_enabled(self, asset_class: AssetClass) -> bool:
        if asset_class == AssetClass.EQUITY:
            return self.settings.equity_trading_enabled
        if asset_class == AssetClass.ETF:
            return self.settings.etf_trading_enabled
        if asset_class == AssetClass.CRYPTO:
            return self.settings.crypto_trading_enabled
        if asset_class == AssetClass.OPTION:
            return self.settings.option_trading_enabled
        return False

    def _is_risk_reducing_sell(self, symbol: str, side: str, quantity: float) -> bool:
        if side != "SELL" or quantity <= 0:
            return False

        position = self.portfolio.get_position(symbol)
        if position is None:
            return False

        position_side = str(position.side).upper()
        if position_side in {"SELL", "SHORT"}:
            return False

        return quantity <= (position.quantity + 1e-9)

    def record_event(self, symbol: Optional[str], reason: str, details: Any = None) -> None:
        event_payload = {
            "symbol": symbol,
            "reason": reason,
            "details": details,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        self.portfolio.risk_events.append(event_payload)
        persisted_details = details
        if not isinstance(details, str) and details is not None:
            persisted_details = json.dumps(details, default=str)
        try:
            with SessionLocal() as session:
                session.add(
                    RiskEventRecord(
                        symbol=symbol,
                        reason=reason,
                        details=persisted_details,
                        is_blocked=True,
                    )
                )
                session.commit()
        except Exception as exc:
            logger.warning("Failed to persist risk event: %s", exc)

    def guard_against(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        stop_price: float | None = None,
        **kwargs: Any,
    ) -> RiskDecision:
        decision = self.evaluate_order(symbol, side, quantity, price, stop_price=stop_price, **kwargs)
        if not decision.approved:
            self._remember_rejection(symbol, side, decision)
            self.record_event(
                symbol,
                decision.reason,
                {
                    "side": side,
                    "quantity": quantity,
                    "price": price,
                    "stop_price": stop_price,
                    "rule": decision.rule,
                    "decision_details": decision.details,
                    "extras": kwargs,
                },
            )
        return decision

    def record_manual_rejection(self, symbol: str, side: str, decision: RiskDecision) -> None:
        if decision.approved:
            return
        self._remember_rejection(symbol, side, decision)
        self.record_event(
            symbol,
            decision.reason,
            {
                "side": side,
                "rule": decision.rule,
                "decision_details": decision.details,
                "source": "manual_rejection",
            },
        )

    def _build_decision_details(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        account: dict[str, float],
        asset_class: AssetClass,
        strategy_name: str | None,
        spread_pct: float | None,
        quote_bid: float | None,
        quote_ask: float | None,
        quote_mid: float | None,
        quote_timestamp: str | None,
        quote_age_seconds: float | None,
        quote_available: bool | None,
        quote_stale: bool | None,
        spread_abs: float | None,
        price_source_used: str | None,
        fallback_pricing_used: bool | None,
        avg_volume: float | None,
        dollar_volume: float | None,
        data_age_seconds: float | None,
        exchange: str | None,
        reduces_exposure: bool,
        sizing: dict[str, Any] | None,
    ) -> dict[str, Any]:
        position = self.portfolio.get_position(symbol)
        current_daily_loss_amount = self.portfolio.current_daily_loss_amount(equity=account["equity"])
        current_daily_loss_pct = self.portfolio.current_daily_loss_pct(equity=account["equity"])
        has_tracked_position = position is not None
        has_tracked_long_position = self.portfolio.is_sellable_long_position(symbol)
        details = {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "rounded_price": sizing.get("rounded_price") if sizing else price,
            "order_notional": float(
                self._round_money(
                    self._decimal(
                        sizing.get("final_submitted_notional")
                        if sizing and sizing.get("final_submitted_notional") is not None
                        else (self._decimal(quantity) * self._decimal(price))
                    )
                )
            ),
            "asset_class": asset_class.value,
            "strategy_name": strategy_name,
            "short_selling_enabled": self.settings.short_selling_enabled,
            "is_risk_reducing_sell": reduces_exposure,
            "has_tracked_position": has_tracked_position,
            "has_tracked_long_position": has_tracked_long_position,
            "tracked_position_quantity": position.quantity if position is not None else 0.0,
            "tracked_position_side": str(position.side) if position is not None else None,
            "tracked_position_entry_price": position.entry_price if position is not None else None,
            "tracked_position_asset_class": position.asset_class.value if position is not None else None,
            "tracked_position_exchange": position.exchange if position is not None else None,
            "tracked_position_sellable": has_tracked_long_position,
            "cash": account["cash"],
            "equity": account["equity"],
            "buying_power": account["buying_power"],
            "daily_baseline_equity": self.portfolio.daily_baseline_equity,
            "daily_baseline_date": (
                self.portfolio.daily_baseline_date.isoformat()
                if self.portfolio.daily_baseline_date is not None
                else None
            ),
            "current_daily_loss_amount": current_daily_loss_amount,
            "current_daily_loss_pct": current_daily_loss_pct,
            "drawdown_pct": self.portfolio.drawdown_pct(),
            "spread_pct": spread_pct,
            "bid": quote_bid,
            "ask": quote_ask,
            "mid": quote_mid,
            "spread_abs": spread_abs,
            "quote_timestamp": quote_timestamp,
            "quote_age_seconds": quote_age_seconds,
            "quote_available": quote_available,
            "quote_stale": quote_stale,
            "price_source_used": price_source_used,
            "fallback_pricing_used": fallback_pricing_used,
            "avg_volume": avg_volume,
            "dollar_volume": dollar_volume,
            "data_age_seconds": data_age_seconds,
            "exchange": exchange,
        }
        if sizing:
            details.update(sizing)
            details.setdefault(
                "max_allowed_notional",
                sizing.get("effective_max_order_notional", self.settings.effective_max_position_notional),
            )
        else:
            details["max_allowed_notional"] = self.settings.effective_max_position_notional
            details["comparison_operator"] = ">"
        return details

    def _remember_rejection(self, symbol: str, side: str, decision: RiskDecision) -> None:
        rejection_payload = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "symbol": symbol,
            "side": side.value if hasattr(side, "value") else str(side),
            "rule": decision.rule,
            "reason": decision.reason,
            **dict(decision.details),
        }
        self._latest_rejection = rejection_payload
        self._recent_rejections.append(rejection_payload)
        self._recent_rejections = self._recent_rejections[-50:]

    def _decimal(self, value: Any) -> Decimal:
        return Decimal(str(value))

    def _round_money(self, value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
