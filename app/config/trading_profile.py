from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from app.domain.models import AssetClass

if TYPE_CHECKING:
    from app.config.settings import Settings


PROFILE_NAMES = frozenset({"conservative", "balanced", "aggressive"})

DEFAULT_AGGRESSIVE_SCAN_INTERVAL_SECONDS_BY_ASSET_CLASS: dict[str, int] = {
    AssetClass.EQUITY.value: 60,
    AssetClass.ETF.value: 60,
    AssetClass.CRYPTO.value: 30,
}
DEFAULT_AGGRESSIVE_UNIVERSE_PREFILTER_LIMIT_BY_ASSET_CLASS: dict[str, int] = {
    AssetClass.EQUITY.value: 90,
    AssetClass.ETF.value: 75,
    AssetClass.CRYPTO.value: 80,
}
DEFAULT_AGGRESSIVE_FINAL_EVALUATION_LIMIT_BY_ASSET_CLASS: dict[str, int] = {
    AssetClass.EQUITY.value: 24,
    AssetClass.ETF.value: 20,
    AssetClass.CRYPTO.value: 24,
}


@dataclass(frozen=True)
class ResolvedTradingProfile:
    name: str
    version: str
    aggressive_mode_enabled: bool
    risk_per_trade_pct: float
    max_positions_total: int
    max_positions_per_asset_class: dict[str, int]
    max_symbol_allocation_pct: float
    scan_interval_seconds_by_asset_class: dict[str, int]
    universe_prefilter_limit_by_asset_class: dict[str, int]
    final_evaluation_limit_by_asset_class: dict[str, int]
    allow_extended_hours: bool
    short_selling_enabled: bool
    scale_in_mode: str
    min_bars_between_tranches: int
    minutes_between_tranches: int
    add_on_favorable_move_pct: float
    ml_min_score_threshold: float
    entry_threshold_adjustment: float
    news_catalyst_weight: float
    candidate_strategies_by_asset_class: dict[str, tuple[str, ...]]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["candidate_strategies_by_asset_class"] = {
            key: list(value) for key, value in self.candidate_strategies_by_asset_class.items()
        }
        return payload

    def strategies_for_asset_class(self, asset_class: AssetClass | str) -> list[str]:
        key = asset_class.value if isinstance(asset_class, AssetClass) else str(asset_class).strip().lower()
        return list(self.candidate_strategies_by_asset_class.get(key, ()))


def _blend_int(base: int, target: int) -> int:
    if target == base:
        return base
    step = max(1, round(abs(target - base) / 2))
    return base + step if target > base else max(1, base - step)


def _blend_float(base: float, target: float) -> float:
    return base + ((target - base) / 2.0)


def _merge_asset_class_int_map(
    base: dict[str, int],
    overrides: dict[str, int],
    *,
    minimum: int = 1,
) -> dict[str, int]:
    merged = {str(key).strip().lower(): max(minimum, int(value)) for key, value in base.items() if str(key).strip()}
    for key, value in overrides.items():
        normalized_key = str(key).strip().lower()
        if not normalized_key:
            continue
        merged[normalized_key] = max(minimum, int(value))
    return merged


def _blend_asset_class_int_map(base: dict[str, int], target: dict[str, int]) -> dict[str, int]:
    keys = set(base) | set(target)
    return {
        key: _blend_int(int(base.get(key, target.get(key, 1))), int(target.get(key, base.get(key, 1))))
        for key in keys
    }


def _dedupe_names(names: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        normalized = str(name).strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return tuple(ordered)


def _candidate_strategies(settings: "Settings", *, include_pullback: bool, include_shorts: bool) -> dict[str, tuple[str, ...]]:
    strategies: dict[str, tuple[str, ...]] = {}
    for asset_class in (AssetClass.EQUITY, AssetClass.ETF, AssetClass.CRYPTO):
        primary = settings.strategy_for_asset_class(asset_class)
        names = [primary]
        if include_pullback and asset_class in {AssetClass.EQUITY, AssetClass.ETF}:
            names.append("equity_trend_pullback")
        if include_shorts and asset_class in {AssetClass.EQUITY, AssetClass.ETF}:
            names.append("ema_crossover")
        strategies[asset_class.value] = _dedupe_names(names)
    return strategies


def resolve_trading_profile(settings: "Settings") -> ResolvedTradingProfile:
    profile_name = str(settings.trading_profile or "conservative").strip().lower()
    if profile_name not in PROFILE_NAMES:
        profile_name = "conservative"

    conservative = ResolvedTradingProfile(
        name="conservative",
        version=str(settings.aggressive_profile_version).strip() or "v1",
        aggressive_mode_enabled=False,
        risk_per_trade_pct=float(settings.risk_per_trade_pct),
        max_positions_total=int(settings.max_positions_total),
        max_positions_per_asset_class={key: int(value) for key, value in settings.max_positions_per_asset_class.items()},
        max_symbol_allocation_pct=float(settings.max_symbol_allocation_pct),
        scan_interval_seconds_by_asset_class={
            key: int(value) for key, value in settings.scan_interval_seconds_by_asset_class.items()
        },
        universe_prefilter_limit_by_asset_class={
            key: int(value) for key, value in settings.universe_prefilter_limit_by_asset_class.items()
        },
        final_evaluation_limit_by_asset_class={
            key: int(value) for key, value in settings.final_evaluation_limit_by_asset_class.items()
        },
        allow_extended_hours=bool(settings.allow_extended_hours),
        short_selling_enabled=bool(settings.short_selling_enabled),
        scale_in_mode=str(settings.scale_in_mode),
        min_bars_between_tranches=int(settings.min_bars_between_tranches),
        minutes_between_tranches=int(settings.minutes_between_tranches),
        add_on_favorable_move_pct=float(settings.add_on_favorable_move_pct),
        ml_min_score_threshold=float(settings.ml_min_score_threshold),
        entry_threshold_adjustment=0.0,
        news_catalyst_weight=0.0,
        candidate_strategies_by_asset_class=_candidate_strategies(
            settings,
            include_pullback=False,
            include_shorts=False,
        ),
    )

    aggressive_targets = ResolvedTradingProfile(
        name="aggressive",
        version=str(settings.aggressive_profile_version).strip() or "v1",
        aggressive_mode_enabled=True,
        risk_per_trade_pct=float(settings.aggressive_risk_per_trade_pct),
        max_positions_total=int(settings.aggressive_max_positions),
        max_positions_per_asset_class=_merge_asset_class_int_map(
            conservative.max_positions_per_asset_class,
            settings.aggressive_max_positions_per_asset_class,
        ),
        max_symbol_allocation_pct=float(settings.aggressive_max_symbol_allocation_pct),
        scan_interval_seconds_by_asset_class=_merge_asset_class_int_map(
            conservative.scan_interval_seconds_by_asset_class,
            settings.aggressive_scan_interval_seconds_by_asset_class,
        ),
        universe_prefilter_limit_by_asset_class=_merge_asset_class_int_map(
            conservative.universe_prefilter_limit_by_asset_class,
            settings.aggressive_universe_prefilter_limit_by_asset_class,
        ),
        final_evaluation_limit_by_asset_class=_merge_asset_class_int_map(
            conservative.final_evaluation_limit_by_asset_class,
            settings.aggressive_final_evaluation_limit_by_asset_class,
        ),
        allow_extended_hours=bool(settings.allow_extended_hours or settings.aggressive_extended_hours_enabled),
        short_selling_enabled=bool(settings.short_selling_enabled or settings.aggressive_shorts_enabled),
        scale_in_mode="momentum",
        min_bars_between_tranches=0,
        minutes_between_tranches=min(int(settings.minutes_between_tranches), 2),
        add_on_favorable_move_pct=min(float(settings.add_on_favorable_move_pct), 0.25),
        ml_min_score_threshold=max(
            0.0,
            min(1.0, float(settings.ml_min_score_threshold) + float(settings.aggressive_entry_threshold_adjustment)),
        ),
        entry_threshold_adjustment=float(settings.aggressive_entry_threshold_adjustment),
        news_catalyst_weight=float(settings.aggressive_news_catalyst_weight),
        candidate_strategies_by_asset_class=_candidate_strategies(
            settings,
            include_pullback=True,
            include_shorts=bool(settings.short_selling_enabled or settings.aggressive_shorts_enabled),
        ),
    )

    if profile_name == "aggressive":
        return aggressive_targets
    if profile_name == "balanced":
        return ResolvedTradingProfile(
            name="balanced",
            version=aggressive_targets.version,
            aggressive_mode_enabled=False,
            risk_per_trade_pct=_blend_float(conservative.risk_per_trade_pct, aggressive_targets.risk_per_trade_pct),
            max_positions_total=_blend_int(conservative.max_positions_total, aggressive_targets.max_positions_total),
            max_positions_per_asset_class=_blend_asset_class_int_map(
                conservative.max_positions_per_asset_class,
                aggressive_targets.max_positions_per_asset_class,
            ),
            max_symbol_allocation_pct=_blend_float(
                conservative.max_symbol_allocation_pct,
                aggressive_targets.max_symbol_allocation_pct,
            ),
            scan_interval_seconds_by_asset_class=_blend_asset_class_int_map(
                conservative.scan_interval_seconds_by_asset_class,
                aggressive_targets.scan_interval_seconds_by_asset_class,
            ),
            universe_prefilter_limit_by_asset_class=_blend_asset_class_int_map(
                conservative.universe_prefilter_limit_by_asset_class,
                aggressive_targets.universe_prefilter_limit_by_asset_class,
            ),
            final_evaluation_limit_by_asset_class=_blend_asset_class_int_map(
                conservative.final_evaluation_limit_by_asset_class,
                aggressive_targets.final_evaluation_limit_by_asset_class,
            ),
            allow_extended_hours=bool(settings.allow_extended_hours),
            short_selling_enabled=bool(settings.short_selling_enabled),
            scale_in_mode="confirmation",
            min_bars_between_tranches=max(0, int(settings.min_bars_between_tranches)),
            minutes_between_tranches=min(int(settings.minutes_between_tranches), 3),
            add_on_favorable_move_pct=min(float(settings.add_on_favorable_move_pct), 0.4),
            ml_min_score_threshold=_blend_float(
                conservative.ml_min_score_threshold,
                aggressive_targets.ml_min_score_threshold,
            ),
            entry_threshold_adjustment=float(settings.aggressive_entry_threshold_adjustment) / 2.0,
            news_catalyst_weight=float(settings.aggressive_news_catalyst_weight) / 2.0,
            candidate_strategies_by_asset_class=_candidate_strategies(
                settings,
                include_pullback=True,
                include_shorts=False,
            ),
        )
    return conservative
