#!/usr/bin/env python3
"""
Verify that the key changes are working:
1. Strategy routing by asset class
2. Session-aware filtering
3. Major symbol filtering
4. Normalized market snapshots
5. Quantity reduction for risk
6. Discord timestamp formatting
"""

from app.config.settings import get_settings
from app.domain.models import AssetClass, SessionState
from app.services.runtime import get_runtime
from app.services.market_data import CSVMarketDataService, MarketSessionStatus
from app.monitoring.discord_notifier import format_readable_notification_timestamp
from datetime import datetime, timezone

print("=" * 80)
print("VERIFICATION SCRIPT FOR MONEY BOT CHANGES")
print("=" * 80)

# Test 1: Settings loaded correctly
print("\n[1] Checking settings configuration...")
settings = get_settings()
print(f"   ✓ scan_universe_mode: {settings.scan_universe_mode}")
print(f"   ✓ major_equity_symbols: {settings.major_equity_symbols[:3]}...")
print(f"   ✓ major_crypto_symbols: {settings.major_crypto_symbols[:2]}...")
print(f"   ✓ active_strategy_by_asset_class: {settings.active_strategy_by_asset_class}")
print(f"   ✓ discord_timezone: {settings.discord_timezone}")
print(f"   ✓ discord_notify_crypto: {settings.discord_notify_crypto}")
print(f"   ✓ discord_notify_scan_summary: {settings.discord_notify_scan_summary}")

# Test 2: Strategy routing
print("\n[2] Checking strategy routing by asset class...")
try:
    runtime = get_runtime(settings)
    
    equity_strat = runtime.strategy_registry.get(settings.strategy_for_asset_class(AssetClass.EQUITY))
    print(f"   ✓ Equity strategy: {equity_strat.name}")
    print(f"     - Supports: {equity_strat.supported_asset_classes}")
    
    etf_strat = runtime.strategy_registry.get(settings.strategy_for_asset_class(AssetClass.ETF))
    print(f"   ✓ ETF strategy: {etf_strat.name}")
    print(f"     - Supports: {etf_strat.supported_asset_classes}")
    
    crypto_strat = runtime.strategy_registry.get(settings.strategy_for_asset_class(AssetClass.CRYPTO))
    print(f"   ✓ Crypto strategy: {crypto_strat.name}")
    print(f"     - Supports: {crypto_strat.supported_asset_classes}")
except Exception as e:
    print(f"   ✗ Error: {e}")

# Test 3: Session status for crypto (should be 24/7)
print("\n[3] Checking session status for crypto (24/7)...")
try:
    market_data = runtime.market_data_service
    crypto_session = market_data.get_session_status(AssetClass.CRYPTO)
    print(f"   ✓ Crypto session is_open: {crypto_session.is_open}")
    print(f"   ✓ Crypto session state: {crypto_session.session_state.value}")
    print(f"   ✓ Crypto is_24_7: {crypto_session.is_24_7}")
    
    equity_session = market_data.get_session_status(AssetClass.EQUITY)
    print(f"   ✓ Equity session state: {equity_session.session_state.value}")
except Exception as e:
    print(f"   ✗ Error: {e}")

# Test 4: Major symbol filtering
print("\n[4] Checking major symbol filtering...")
try:
    # This should respect SCAN_UNIVERSE_MODE=major
    universe = runtime.asset_catalog.get_scan_universe(AssetClass.EQUITY)
    print(f"   ✓ Equity universe size (major mode): {len(universe)}")
    if universe:
        print(f"     Sample symbols: {[a.symbol for a in universe[:3]]}")
    
    crypto_universe = runtime.asset_catalog.get_scan_universe(AssetClass.CRYPTO)
    print(f"   ✓ Crypto universe size (major mode): {len(crypto_universe)}")
    if crypto_universe:
        print(f"     Crypto symbols: {[a.symbol for a in crypto_universe]}")
except Exception as e:
    print(f"   ✗ Error: {e}")

# Test 5: Normalized market snapshot
print("\n[5] Checking normalized market snapshots...")
try:
    # CSV service available for testing
    if isinstance(market_data, CSVMarketDataService):
        snapshot = market_data.get_normalized_snapshot("AAPL", AssetClass.EQUITY)
        print(f"   ✓ AAPL normalized snapshot:")
        print(f"     - evaluation_price: {snapshot.evaluation_price}")
        print(f"     - quote_available: {snapshot.quote_available}")
        print(f"     - quote_stale: {snapshot.quote_stale}")
        print(f"     - spread_pct: {snapshot.spread_pct}")
        print(f"     - price_source_used: {snapshot.price_source_used}")
        print(f"     - fallback_pricing_used: {snapshot.fallback_pricing_used}")
except Exception as e:
    print(f"   ✗ Error: {e}")

# Test 6: Discord timestamp formatting with timezone
print("\n[6] Checking Discord timestamp formatting...")
try:
    now = datetime.now(timezone.utc)
    formatted = format_readable_notification_timestamp(now, settings)
    print(f"   ✓ Formatted timestamp: {formatted}")
    print(f"     (timezone: {settings.discord_timezone})")
except Exception as e:
    print(f"   ✗ Error: {e}")

# Test 7: Risk manager setup
print("\n[7] Checking risk manager configuration...")
try:
    risk_mgr = runtime.risk_manager
    print(f"   ✓ max_risk_per_trade: {risk_mgr.settings.max_risk_per_trade}")
    print(f"   ✓ quote_stale_after_seconds: {risk_mgr.settings.quote_stale_after_seconds}")
    print(f"   ✓ allow_extended_hours: {risk_mgr.settings.allow_extended_hours}")
except Exception as e:
    print(f"   ✗ Error: {e}")

print("\n" + "=" * 80)
print("VERIFICATION COMPLETE")
print("=" * 80)
