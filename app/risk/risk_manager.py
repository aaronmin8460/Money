from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
        return {
            "trading_enabled": self.settings.trading_enabled,
            "live_trading_enabled": self.settings.live_trading_enabled,
            "kill_switch_enabled": self.settings.kill_switch_enabled,
            "broker_mode": self.settings.broker_mode,
            "cash": account["cash"],
            "equity": account["equity"],
            "buying_power": account["buying_power"],
            "open_positions_count": len(self.portfolio.positions),
            "positions_by_asset_class": self.portfolio.position_counts_by_asset_class(),
            "exposure": self.portfolio.exposure(),
            "exposure_by_asset_class": self.portfolio.exposure_by_asset_class(),
            "risk_events": list(self.portfolio.risk_events),
            "drawdown_pct": self.portfolio.drawdown_pct(),
            "daily_loss_pct": self.portfolio.current_daily_loss_pct(equity=account["equity"]),
        }

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
        avg_volume: float | None = None,
        dollar_volume: float | None = None,
        data_age_seconds: float | None = None,
        exchange: str | None = None,
    ) -> RiskDecision:
        normalized_side = side.value if hasattr(side, "value") else str(side)
        normalized_side = normalized_side.upper()
        resolved_asset_class = normalize_asset_class(asset_class)
        if resolved_asset_class == AssetClass.UNKNOWN:
            resolved_asset_class = AssetClass.EQUITY
        reduces_exposure = self._is_risk_reducing_sell(symbol, normalized_side, quantity)

        if self.settings.kill_switch_enabled:
            return RiskDecision(False, "Hard kill switch is enabled.", rule="kill_switch")

        if not self.settings.trading_enabled:
            return RiskDecision(
                True,
                "Trading is disabled. The order will be evaluated as a dry-run.",
                rule="dry_run",
            )

        if quantity <= 0 or price <= 0:
            return RiskDecision(False, "Invalid order quantity or price.", rule="input_validation")

        if not self._asset_class_enabled(resolved_asset_class):
            return RiskDecision(
                False,
                f"Trading is disabled for asset class '{resolved_asset_class.value}'.",
                rule="asset_class_disabled",
            )

        account = self.get_account_snapshot()
        if not reduces_exposure:
            drawdown_pct = self.portfolio.drawdown_pct()
            if drawdown_pct >= self.settings.max_drawdown_pct:
                return RiskDecision(
                    False,
                    f"Max drawdown ({drawdown_pct:.2%}) exceeded ({self.settings.max_drawdown_pct:.2%}).",
                    rule="drawdown_limit",
                )

            daily_loss_pct = self.portfolio.current_daily_loss_pct(equity=account["equity"])
            if daily_loss_pct >= self.settings.max_daily_loss_pct:
                return RiskDecision(
                    False,
                    f"Max daily loss ({daily_loss_pct:.2%}) reached ({self.settings.max_daily_loss_pct:.2%}).",
                    rule="daily_loss_pct_limit",
                )

            current_loss = self.portfolio.current_daily_loss_amount(equity=account["equity"])
            if current_loss >= self.settings.max_daily_loss:
                return RiskDecision(
                    False,
                    f"Max daily loss notional ({current_loss:.2f}) reached ({self.settings.max_daily_loss:.2f}).",
                    rule="daily_loss_limit",
                )

        if data_age_seconds is not None and data_age_seconds > self.settings.data_stale_after_seconds:
            return RiskDecision(False, "Market data is stale.", rule="stale_data")

        if spread_pct is not None and spread_pct > self.settings.max_spread_pct:
            return RiskDecision(False, "Spread exceeds configured limit.", rule="spread_limit")

        if resolved_asset_class != AssetClass.CRYPTO and price < self.settings.min_price:
            return RiskDecision(False, "Price below configured minimum.", rule="min_price")

        if resolved_asset_class != AssetClass.CRYPTO and avg_volume is not None and avg_volume < self.settings.min_avg_volume:
            return RiskDecision(False, "Average volume below configured minimum.", rule="avg_volume")

        if resolved_asset_class != AssetClass.CRYPTO and dollar_volume is not None and dollar_volume < self.settings.min_dollar_volume:
            return RiskDecision(False, "Dollar volume below configured minimum.", rule="dollar_volume")

        cooldown = self._symbol_cooldowns.get(symbol.upper())
        if cooldown and datetime.utcnow() < cooldown:
            return RiskDecision(False, "Symbol cooldown is active.", rule="symbol_cooldown")

        if strategy_name:
            strategy_key = strategy_name.lower()
            strategy_cooldown = self._strategy_cooldowns.get(strategy_key)
            if strategy_cooldown and datetime.utcnow() < strategy_cooldown:
                return RiskDecision(False, "Strategy cooldown is active.", rule="strategy_cooldown")

        order_notional = quantity * price
        if normalized_side == "BUY":
            if symbol in self.portfolio.positions:
                return RiskDecision(False, "Duplicate buy order blocked for existing position.", rule="duplicate_position")

            max_positions = self.settings.max_positions_total
            if len(self.portfolio.positions) >= max_positions:
                return RiskDecision(False, f"Maximum simultaneous positions ({max_positions}) reached.", rule="position_count")

            positions_by_class = self.portfolio.position_counts_by_asset_class()
            class_limit = int(
                self.settings.max_positions_per_asset_class.get(
                    resolved_asset_class.value,
                    max_positions,
                )
            )
            if positions_by_class.get(resolved_asset_class.value, 0) >= class_limit:
                return RiskDecision(
                    False,
                    f"Maximum positions for asset class '{resolved_asset_class.value}' reached.",
                    rule="asset_class_position_count",
                )

            class_exposure = self.portfolio.exposure_by_asset_class().get(resolved_asset_class.value, 0.0)
            if class_exposure + order_notional > self.settings.max_notional_per_asset_class.get(
                resolved_asset_class.value,
                self.settings.max_total_exposure,
            ):
                return RiskDecision(
                    False,
                    "Asset-class notional limit would be exceeded.",
                    rule="asset_class_notional",
                )

            if self.portfolio.exposure() + order_notional > self.settings.max_total_exposure:
                return RiskDecision(False, "Max total exposure would be exceeded.", rule="total_exposure")

            if order_notional > self.settings.max_notional_per_position:
                return RiskDecision(
                    False,
                    f"Order notional ({order_notional:.2f}) exceeds max position notional ({self.settings.max_notional_per_position:.2f}).",
                    rule="position_notional",
                )

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
                )

            if self.settings.is_paper_mode and order_notional > account["cash"]:
                return RiskDecision(
                    False,
                    f"Order notional ({order_notional:.2f}) exceeds available cash ({account['cash']:.2f}).",
                    rule="cash_limit",
                )

            if order_notional > account["buying_power"]:
                return RiskDecision(
                    False,
                    f"Order notional ({order_notional:.2f}) exceeds buying power ({account['buying_power']:.2f}).",
                    rule="buying_power",
                )

            if stop_price is not None:
                risk_per_share = price - stop_price
                if risk_per_share <= 0:
                    return RiskDecision(False, "Stop price must be below entry price for a long position.", rule="stop_validation")

                trade_risk = quantity * risk_per_share
                max_trade_risk = account["equity"] * self.settings.max_risk_per_trade
                if trade_risk > max_trade_risk:
                    return RiskDecision(
                        False,
                        f"Stop-based trade risk ({trade_risk:.2f}) exceeds max risk per trade ({max_trade_risk:.2f}).",
                        rule="trade_risk",
                    )

        if (
            self.settings.is_alpaca_mode
            and resolved_asset_class != AssetClass.CRYPTO
            and not self.settings.allow_extended_hours
        ):
            broker = self.broker
            if broker is not None and hasattr(broker, "is_market_open") and not broker.is_market_open(resolved_asset_class):
                return RiskDecision(False, "Market is closed and extended hours not allowed.", rule="market_hours")

        return RiskDecision(True, "Order approved by risk manager.", rule="approved")

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

    def record_event(self, symbol: Optional[str], reason: str, details: Optional[str] = None) -> None:
        event_payload = {
            "symbol": symbol,
            "reason": reason,
            "details": details,
        }
        self.portfolio.risk_events.append(event_payload)
        try:
            with SessionLocal() as session:
                session.add(
                    RiskEventRecord(
                        symbol=symbol,
                        reason=reason,
                        details=details,
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
            self.record_event(
                symbol,
                decision.reason,
                f"side={side}, qty={quantity}, price={price}, stop_price={stop_price}, extras={kwargs}",
            )
        return decision
