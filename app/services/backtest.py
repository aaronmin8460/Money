from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from app.config.settings import Settings, get_settings
from app.domain.models import AssetClass, AssetMetadata, MarketSessionStatus, SessionState
from app.ml.evaluation import compute_trade_metrics
from app.ml.inference import SignalScorer
from app.portfolio.portfolio import Portfolio
from app.services.exit_manager import ExitManager
from app.strategies.base import Signal, StrategyContext, TradeSignal
from app.strategies.regime_momentum_breakout import RegimeMomentumBreakoutStrategy


@dataclass
class ReplayTrade:
    symbol: str
    strategy_variant: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    quantity: float
    exit_stage: str
    bars_held: int
    pnl: float
    return_pct: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "strategy_variant": self.strategy_variant,
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "quantity": self.quantity,
            "exit_stage": self.exit_stage,
            "bars_held": self.bars_held,
            "pnl": self.pnl,
            "return_pct": self.return_pct,
        }


def _baseline_signal(symbol: str, history: pd.DataFrame, asset_class: AssetClass) -> TradeSignal | None:
    if len(history) < 25:
        return None
    latest = history.iloc[-1]
    breakout_level = history["High"].rolling(20, min_periods=1).max().shift(1).iloc[-1]
    avg_volume = history["Volume"].rolling(20, min_periods=1).mean().iloc[-1]
    if pd.isna(breakout_level) or pd.isna(avg_volume):
        return None
    close = float(latest["Close"])
    atr = float((history["High"] - history["Low"]).rolling(14, min_periods=1).mean().iloc[-1])
    if close <= float(breakout_level) or float(latest["Volume"]) < float(avg_volume):
        return None
    return TradeSignal(
        symbol=symbol,
        signal=Signal.BUY,
        asset_class=asset_class,
        strategy_name="baseline_momentum_breakout",
        price=close,
        entry_price=close,
        atr=atr,
        stop_price=close - (atr * 2.0),
        target_price=close + (atr * 3.0),
        trailing_stop=close - (atr * 2.4),
        reason="Baseline breakout signal.",
        metrics={"strategy_score": 0.55, "reward_risk_ratio": 1.5},
    )


def _candidate_signal(
    symbol: str,
    history: pd.DataFrame,
    asset_class: AssetClass,
    *,
    strategy: RegimeMomentumBreakoutStrategy,
) -> TradeSignal | None:
    if len(history) < 60:
        return None
    context = StrategyContext(
        asset=AssetMetadata(symbol=symbol, name=symbol, asset_class=asset_class),
        session=MarketSessionStatus(
            asset_class=asset_class,
            is_open=True,
            session_state=SessionState.REGULAR,
            extended_hours=False,
            is_24_7=asset_class == AssetClass.CRYPTO,
        ),
        metadata={"has_sellable_long_position": False},
    )
    signal = strategy.generate_signals(symbol, history, context=context)[-1]
    return signal if signal.signal == Signal.BUY else None


def _apply_slippage(price: float, *, is_entry: bool, bps: float) -> float:
    multiplier = 1.0 + (bps / 10_000.0 if is_entry else -(bps / 10_000.0))
    return price * multiplier


def _entry_quantity(settings: Settings, portfolio: Portfolio, price: float) -> float:
    notional = min(
        settings.max_position_notional,
        portfolio.cash * settings.max_symbol_allocation_pct,
    )
    if price <= 0:
        return 0.0
    quantity = notional / price
    if quantity < 1.0:
        return 0.0
    return float(int(quantity))


def _write_artifacts(
    output_dir: Path,
    *,
    variant: str,
    summary: dict[str, Any],
    trades: list[ReplayTrade],
    equity_curve: list[dict[str, Any]],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"{variant}_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output_dir / f"{variant}_trades.jsonl").write_text(
        "\n".join(json.dumps(trade.to_dict()) for trade in trades),
        encoding="utf-8",
    )
    with (output_dir / f"{variant}_trades.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ReplayTrade.__annotations__.keys()))
        writer.writeheader()
        for trade in trades:
            writer.writerow(trade.to_dict())
    (output_dir / f"{variant}_equity_curve.json").write_text(json.dumps(equity_curve, indent=2), encoding="utf-8")


def run_variant_replay(
    csv_path: Path,
    symbol: str,
    *,
    variant: str,
    settings: Settings | None = None,
    output_dir: Path | None = None,
    use_entry_model: bool = False,
    use_exit_model: bool = False,
    slippage_bps: float = 5.0,
    fee_bps: float = 1.0,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV path not found: {csv_path}")

    df = pd.read_csv(csv_path, parse_dates=["Date"]).set_index("Date")
    if df.empty:
        raise ValueError(f"No historical rows found in {csv_path}")

    asset_class = AssetClass.CRYPTO if "/" in symbol else AssetClass.EQUITY
    portfolio = Portfolio(cash=100_000.0)
    scorer = SignalScorer(resolved_settings)
    exit_manager = ExitManager(portfolio, settings=resolved_settings)
    strategy = RegimeMomentumBreakoutStrategy()
    open_entry: dict[str, Any] | None = None
    trades: list[ReplayTrade] = []
    equity_curve: list[dict[str, Any]] = []

    for index in range(len(df)):
        history = df.iloc[: index + 1].copy()
        row = history.iloc[-1]
        current_price = float(row["Close"])

        if portfolio.is_sellable_long_position(symbol):
            regime_state = None
            if variant == "candidate":
                evaluated = strategy.generate_signals(symbol, history, context=StrategyContext(
                    asset=AssetMetadata(symbol=symbol, name=symbol, asset_class=asset_class),
                    session=MarketSessionStatus(
                        asset_class=asset_class,
                        is_open=True,
                        session_state=SessionState.REGULAR,
                        extended_hours=False,
                        is_24_7=asset_class == AssetClass.CRYPTO,
                    ),
                    metadata={"has_sellable_long_position": True},
                ))[-1]
                regime_state = evaluated.regime_state

            exit_model_score = None
            if use_exit_model and open_entry is not None:
                exit_probe = TradeSignal(
                    symbol=symbol,
                    signal=Signal.SELL,
                    asset_class=asset_class,
                    strategy_name=str(open_entry.get("strategy_name") or variant),
                    signal_type="exit",
                    order_intent="long_exit",
                    reduce_only=True,
                    exit_stage="ml_exit",
                    price=current_price,
                    entry_price=current_price,
                    stop_price=portfolio.get_position(symbol).current_stop,
                    target_price=portfolio.get_position(symbol).entry_signal_metadata.get("target_price"),
                    atr=portfolio.get_position(symbol).entry_signal_metadata.get("atr"),
                    metrics={
                        "holding_duration_bars": index - int(open_entry.get("entry_bar_index", index)),
                        "unrealized_return": (current_price - open_entry["entry_price"]) / open_entry["entry_price"],
                    },
                )
                exit_model_score = scorer.score_exit_signal(exit_probe, latest_price=current_price).score

            evaluation = exit_manager.evaluate_long_position(
                symbol,
                current_price,
                asset_class=asset_class,
                current_bar_index=index,
                regime_state=regime_state,
                exit_model_score=exit_model_score,
            )
            exit_signal = evaluation.signal
            if exit_signal is not None:
                position = portfolio.get_position(symbol)
                quantity = min(position.quantity, exit_signal.position_size or position.quantity)
                fill_price = _apply_slippage(current_price, is_entry=False, bps=slippage_bps)
                portfolio.update_position(
                    symbol,
                    "SELL",
                    quantity,
                    fill_price,
                    asset_class=asset_class,
                    order_intent="long_exit",
                    reduce_only=True,
                    exit_stage=exit_signal.exit_stage,
                    signal_metadata={
                        "current_stop": exit_signal.metrics.get("current_stop") if exit_signal.metrics else None,
                        "next_stop": exit_signal.metrics.get("next_stop") if exit_signal.metrics else None,
                        "tp1_price": exit_signal.metrics.get("tp1_price") if exit_signal.metrics else None,
                        "tp2_price": exit_signal.metrics.get("tp2_price") if exit_signal.metrics else None,
                        "trailing_stop": exit_signal.trailing_stop,
                        "hit_target_stages": exit_signal.metrics.get("hit_target_stages") if exit_signal.metrics else [],
                    },
                )
                if open_entry is not None:
                    pnl = (fill_price - open_entry["entry_price"]) * quantity
                    fees = (fill_price * quantity) * (fee_bps / 10_000.0)
                    pnl -= fees
                    trades.append(
                        ReplayTrade(
                            symbol=symbol,
                            strategy_variant=variant,
                            entry_time=open_entry["entry_time"],
                            exit_time=history.index[-1].isoformat(),
                            entry_price=open_entry["entry_price"],
                            exit_price=fill_price,
                            quantity=quantity,
                            exit_stage=str(exit_signal.exit_stage or "exit"),
                            bars_held=index - int(open_entry["entry_bar_index"]),
                            pnl=pnl,
                            return_pct=pnl / max(open_entry["entry_price"] * quantity, 1e-9),
                        )
                    )
                    if not portfolio.is_sellable_long_position(symbol):
                        open_entry = None

        if not portfolio.is_sellable_long_position(symbol):
            signal = (
                _candidate_signal(symbol, history, asset_class, strategy=strategy)
                if variant == "candidate"
                else _baseline_signal(symbol, history, asset_class)
            )
            if signal is not None and signal.signal == Signal.BUY:
                if use_entry_model:
                    entry_score = scorer.score_signal(signal, latest_price=current_price).score
                    if entry_score is not None and entry_score < resolved_settings.ml_min_score_threshold:
                        signal = None
                if signal is not None:
                    entry_price = _apply_slippage(current_price, is_entry=True, bps=slippage_bps)
                    quantity = _entry_quantity(resolved_settings, portfolio, entry_price)
                    if quantity > 0:
                        portfolio.update_position(
                            symbol,
                            "BUY",
                            quantity,
                            entry_price,
                            asset_class=asset_class,
                            order_intent="long_entry",
                            signal_metadata={
                                "strategy_name": signal.strategy_name,
                                "stop_price": signal.stop_price,
                                "target_price": signal.target_price,
                                "trailing_stop": signal.trailing_stop,
                                "atr": signal.atr,
                                "entry_scan_bar_index": index,
                            },
                        )
                        open_entry = {
                            "entry_time": history.index[-1].isoformat(),
                            "entry_price": entry_price,
                            "entry_bar_index": index,
                            "strategy_name": signal.strategy_name,
                        }

        portfolio.mark_to_market({symbol: current_price})
        equity_curve.append({"timestamp": history.index[-1].isoformat(), "equity": portfolio.calculate_equity()})

    trade_returns = [trade.return_pct for trade in trades]
    trade_metrics = compute_trade_metrics(trade_returns)
    summary = {
        "variant": variant,
        "symbol": symbol,
        "csv_path": str(csv_path),
        "trades": len(trades),
        "return_pct": ((portfolio.calculate_equity() - portfolio.initial_equity) / portfolio.initial_equity) * 100.0,
        "final_equity": portfolio.calculate_equity(),
        "profit_factor": trade_metrics["profit_factor"],
        "expectancy": trade_metrics["expectancy"],
        "average_trade_return": trade_metrics["average_trade_return"],
        "max_drawdown": trade_metrics["max_drawdown"],
        "win_rate": trade_metrics["win_rate"],
        "turnover": trade_metrics["turnover"],
    }
    if output_dir is not None:
        _write_artifacts(output_dir, variant=variant, summary=summary, trades=trades, equity_curve=equity_curve)
    return summary


def run_backtest(
    csv_path: Path,
    symbol: str | None = None,
    *,
    mode: str = "candidate",
    compare: bool = False,
    output_dir: Path | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved_symbol = symbol or csv_path.stem
    resolved_settings = settings or get_settings()
    research_dir = output_dir or (Path(resolved_settings.log_dir) / "research" / resolved_symbol.replace("/", "_"))
    if compare or mode == "compare":
        baseline = run_variant_replay(csv_path, resolved_symbol, variant="baseline", settings=resolved_settings, output_dir=research_dir)
        candidate = run_variant_replay(csv_path, resolved_symbol, variant="candidate", settings=resolved_settings, output_dir=research_dir)
        comparison = {"baseline": baseline, "candidate": candidate}
        (research_dir / "comparison_summary.json").write_text(json.dumps(comparison, indent=2), encoding="utf-8")
        return comparison
    return run_variant_replay(csv_path, resolved_symbol, variant=mode, settings=resolved_settings, output_dir=research_dir)
