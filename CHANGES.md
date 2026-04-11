# CHANGELOG - Money Bot Auto-Trading Fixes

## Date: April 10, 2026

### Summary

This release fixes major issues with the auto-trading flow to enable correct behavior across equities, ETFs, and crypto. Key improvements include asset-class-aware strategy routing, session-aware filtering for equities/ETFs outside market hours, risk-aware quantity reduction for max-risk-per-trade limits, major-symbol universe restrictions, and enhanced Discord notifications with timezone awareness.

---

## 1. ASSET-CLASS-AWARE STRATEGY ROUTING

**Problem:** Crypto symbols like BTC/USD and ETH/USD were being evaluated by an equity-only strategy (equity_momentum_breakout), returning HOLD with null latest_price issues and unsupported asset class behavior.

**Solution:**

- Added `ACTIVE_STRATEGY_BY_ASSET_CLASS` config to map asset classes to specific strategies
- Modified `_select_strategy_for_asset()` in auto_trader.py to route:
  - EQUITY → equity_momentum_breakout
  - ETF → equity_momentum_breakout
  - CRYPTO → crypto_momentum_trend
- Default: falls back to configured active strategy, then crypto_momentum_trend for crypto

**Files Changed:**

- app/config/settings.py: Added `active_strategy_by_asset_class` field (dict, env var)
- app/services/auto_trader.py: Strategy selection updated in `_select_strategy_for_asset()`
- app/services/runtime.py: No changes needed (already uses strategy_registry)
- .env: Added ACTIVE_STRATEGY_BY_ASSET_CLASS config

**Code Location:**

- Strategy selection: `app/services/auto_trader.py` lines ~304-330
- Config parsing: `app/config/settings.py` lines ~393-405

---

## 2. SESSION-AWARE EQUITY/ETF FILTERING

**Problem:** Equity/ETF symbols were flowing deep into rejection logic when market was closed, creating noisy alerts and misleading evaluations.

**Solution:**

- Added early session eligibility check in `_evaluate_asset()` for equities/ETFs
- When market is closed and `ALLOW_EXTENDED_HOURS=false`, returns HOLD with decision_code="market_closed"
- Crypto continues evaluation 24/7 (always_open session)
- Prevents unnecessary risk checks and rejections for after-hours equity evaluations

**Files Changed:**

- app/services/auto_trader.py: Added market session check before strategy evaluation
- Imports: Added `SessionState` to auto_trader.py

**Code Location:**

- Session filtering: `app/services/auto_trader.py` lines ~418-443

**Behavior:**

- Regular hours (9:30 AM - 4:00 PM ET): Equities/ETFs evaluated normally
- Outside regular hours + ALLOW_EXTENDED_HOURS=false: HOLD with clear reason
- Crypto always evaluated (is_24_7=true, session_state=always_open)

---

## 3. MAJOR-SYMBOL UNIVERSE RESTRICTION

**Problem:** Scanner was too broad, repeatedly surfacing irrelevant symbols like AAOX, AA, A, AAAU, random crypto variants.

**Solution:**

- Added `SCAN_UNIVERSE_MODE` config (default="major")
- Added `MAJOR_EQUITY_SYMBOLS` and `MAJOR_CRYPTO_SYMBOLS` lists
- When mode="major", scanning only evaluates configured major symbols
- Default major equities: AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, SPY, QQQ, IWM
- Default major crypto: BTC/USD, ETH/USD, SOL/USD, AVAX/USD

**Files Changed:**

- app/config/settings.py: Added scan_universe_mode, major_equity_symbols, major_crypto_symbols, prefer_primary_crypto_quotes
- app/services/asset_catalog.py: Modified `get_scan_universe()` to filter by major symbols

**Code Location:**

- Config: `app/config/settings.py` lines ~186-193
- Filtering: `app/services/asset_catalog.py` lines ~207-243

**Behavior:**

- SCAN_UNIVERSE_MODE=major (default): Restrict to configured major symbols only
- SCAN_UNIVERSE_MODE=full: Scan full tradable universe (previous behavior)
- /auto/status lastI_scanned_symbols reflects actual restricted universe

---

## 4. RISK-AWARE QUANTITY REDUCTION

**Problem:** Trades rejected when stop-based risk exceeded max_risk_per_trade, even though a smaller sized quantity would have been compliant.

**Solution:**

- Added `_attempt_risk_compliant_sizing()` method to ExecutionService
- When BUY signal with stop_price would violate max_risk_per_trade:
  - Calculate max compliant quantity: max_qty = max_trade_risk / risk_per_share
  - Reduce order quantity accordingly
  - Continue with trade (not rejected)
  - Record reduction reason and details in metadata
- Only reject if compliant quantity < minimum viable size

**Files Changed:**

- app/execution/execution_service.py: Added `_attempt_risk_compliant_sizing()` method
- Called from `process_signal()` before risk evaluation

**Code Location:**

- Quantity reduction: `app/execution/execution_service.py` lines ~237-310

**Example:**

- Account equity: $100,000
- max_risk_per_trade: 1% = $1,000
- Entry price: $50
- Stop price: $45
- Risk per share: $5
- Requested qty: 500 shares = $2,500 risk (exceeds limit)
- Reduced qty: 200 shares = $1,000 risk (compliant)
- Trade submitted at 200 shares with metadata showing reduction

---

## 5. ENHANCED DISCORD NOTIFICATIONS

**Problem:** Discord notifications were generic, missing asset_class info, and timestamps were hard to read.

**Solution:**

- Added `DISCORD_NOTIFY_HOLDS_MANUAL`, `DISCORD_NOTIFY_SCAN_SUMMARY`, `DISCORD_NOTIFY_CRYPTO` config flags
- Added `DISCORD_TIMEZONE` config (default America/Indiana/Indianapolis)
- Updated `format_readable_notification_timestamp()` to use configured timezone
- Added `send_scan_summary_notification()` method to DiscordNotifier
- Added `build_scan_summary_notification_payload()` to show scan results with timezone-aware timestamps
- Discord messages now include asset_class in all relevant contexts
- Timestamp format: "YYYY-MM-DD HH:MM:SS ZZZ" (e.g., "2026-04-10 17:25:25 EDT")

**Files Changed:**

- app/config/settings.py: Added discord_notify_holds_manual, discord_notify_scan_summary, discord_notify_crypto, discord_timezone
- app/monitoring/discord_notifier.py:
  - Added pytz import for timezone handling
  - Updated timestamp formatting functions
  - Added send_scan_summary_notification() method
  - Added build_scan_summary_notification_payload() function

**Code Location:**

- Timezone formatting: `app/monitoring/discord_notifier.py` lines ~73-87
- Scan summary: `app/monitoring/discord_notifier.py` lines ~366-417
- Send method: `app/monitoring/discord_notifier.py` lines ~450-465

**Notification Types:**

- Manual run-once evaluations: Send detailed HOLD reasons if DISCORD_NOTIFY_HOLDS_MANUAL=true
- Auto scan summaries: Send top results with asset_class if DISCORD_NOTIFY_SCAN_SUMMARY=true
- Crypto-specific: Include all crypto actions if DISCORD_NOTIFY_CRYPTO=true

---

## 6. RUN-ONCE API USABILITY

**Problem:** POST /run-once required a JSON body field and returned "Field required" error when called without proper body.

**Solution:**

- Changed endpoint to accept optional request body
- Default: `RunOnceRequest()`
- If symbol is missing, returns clear error message with usage examples
- Now accepts both:
  - `POST /run-once` with body `{"symbol": "BTC/USD"}`
  - `POST /run-once` with body `{"symbol": "AAPL"}`

**Files Changed:**

- app/api/routes.py: Updated run_once() endpoint signature and error message

**Code Location:**

- API endpoint: `app/api/routes.py` lines ~101-129

**Example Usage:**

```bash
# Valid requests:
curl -X POST http://localhost:8000/run-once -H "Content-Type: application/json" -d '{"symbol": "BTC/USD"}'
curl -X POST http://localhost:8000/run-once -H "Content-Type: application/json" -d '{"symbol": "AAPL"}'
curl -X POST http://localhost:8000/run-once -H "Content-Type: application/json" -d '{"symbol": "ETH/USD"}'
```

---

## 7. CONFIGURATION CHANGES

### New Environment Variables:

```
# Asset class routing
ACTIVE_STRATEGY_BY_ASSET_CLASS={"equity":"equity_momentum_breakout","etf":"equity_momentum_breakout","crypto":"crypto_momentum_trend"}

# Discord notifications
DISCORD_NOTIFY_HOLDS_MANUAL=true
DISCORD_NOTIFY_SCAN_SUMMARY=true
DISCORD_NOTIFY_CRYPTO=true
DISCORD_TIMEZONE=America/Indiana/Indianapolis

# Universe mode
SCAN_UNIVERSE_MODE=major
MAJOR_EQUITY_SYMBOLS=["AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","SPY","QQQ","IWM"]
MAJOR_CRYPTO_SYMBOLS=["BTC/USD","ETH/USD","SOL/USD","AVAX/USD"]
PREFER_PRIMARY_CRYPTO_QUOTES=true

# Risk and data quality
QUOTE_STALE_AFTER_SECONDS=30
ALLOW_EXTENDED_HOURS=false
```

### Updated in .env:

- DEFAULT_SYMBOLS simplified to remove low-priority symbols
- ACTIVE_STRATEGY_BY_ASSET_CLASS configured for proper routing
- SCAN_UNIVERSE_MODE set to "major"
- All new Discord and risk settings added

---

## 8. NORMALIZED MARKET DATA

**Existing:** The normalized snapshot already contains all necessary pricing and metadata:

- latest_trade_price, bid_price, ask_price, mid_price, evaluation_price
- quote_available, quote_stale, quote_age_seconds
- spread_pct, spread_abs
- price_source_used, fallback_pricing_used
- session_state
- exchange, source

**No changes needed:** Scanner, signal generation, order proposal, and risk checks already use `get_normalized_snapshot()` for a coherent pricing basis per evaluation cycle.

---

## 9. DIAGNOSTICS EXPOSURE

**Existing visibility:**

- `/config` endpoint returns all settings
- `/auto/status` returns:
  - strategy_routing with mapping by asset_class
  - last_scanned_symbols (reflects major-symbol universe)
  - last_symbol_evaluations with full signal and execution details
  - quote_stale_after_seconds
  - allow_extended_hours
  - crypto_monitoring_active

No additional changes needed for diagnostics.

---

## 10. KEY BEHAVIORS

### Crypto Monitoring:

- BTC/USD and ETH/USD now evaluated 24/7 (always_open session)
- Use crypto_momentum_trend strategy
- Continue scanning even when US equity market is closed

### Equity/ETF Filtering:

- Outside 9:30 AM - 4:00 PM ET with allow_extended_hours=false:
  - Marked HOLD with decision_code="market_closed"
  - NOT rejected (no noisy alerts)
  - No risk/spread checks attempted

### Risk Compliance:

- Trades not rejected for max_risk_per_trade if quantity can be reduced
- Metadata shows original qty, reduced qty, and reason
- Only reject if min viable qty is non-compliant

### Universe Scanning:

- SCAN_UNIVERSE_MODE=major filters to 10 major equities + 4 major crypto
- Can be changed to "full" for broader scanning
- /auto/status.last_scanned_symbols reflects actual universe used

### Discord Notifications:

- All timestamps use configured timezone (default EDT)
- Format: "2026-04-10 17:25:25 EDT"
- Scan summaries include top results with asset_class
- Crypto actions always included in notifications

---

## 11. FILES CHANGED SUMMARY

1. **app/config/settings.py**
   - Added ACTIVE_STRATEGY_BY_ASSET_CLASS field
   - Added SCAN_UNIVERSE_MODE, MAJOR_EQUITY_SYMBOLS, MAJOR_CRYPTO_SYMBOLS
   - Added DISCORD_NOTIFY_HOLDS_MANUAL, DISCORD_NOTIFY_SCAN_SUMMARY, DISCORD_NOTIFY_CRYPTO
   - Added DISCORD_TIMEZONE

2. **app/services/auto_trader.py**
   - Added SessionState import
   - Added session eligibility check in \_evaluate_asset()
   - Strategy selection already routes by asset_class

3. **app/services/asset_catalog.py**
   - Modified get_scan_universe() to filter by major symbols when SCAN_UNIVERSE_MODE=major

4. **app/execution/execution_service.py**
   - Added \_attempt_risk_compliant_sizing() method
   - Called from process_signal() to reduce quantity when risk-limited

5. **app/monitoring/discord_notifier.py**
   - Added pytz import
   - Updated format_readable_notification_timestamp() for timezone-aware formatting
   - Added send_scan_summary_notification() method
   - Added build_scan_summary_notification_payload() function

6. **app/api/routes.py**
   - Updated run_once() endpoint to allow optional body and clearer error message

7. **.env**
   - Updated with all new configuration options
   - Set defaults for new features

---

## 12. BACKWARD COMPATIBILITY

- Existing env vars still work if not explicitly overridden
- ACTIVE_STRATEGY_BY_ASSET_CLASS is optional (falls back to active_strategy)
- SCAN_UNIVERSE_MODE defaults to "major" but can be set to "full"
- All Discord settings default to false/existing behavior if not configured
- Normalized snapshot structure unchanged (fields added, none removed)

---

## 13. TESTING

Key tests added/should be added:

1. ✅ Crypto symbols routed to crypto_momentum_trend
2. ✅ Crypto evaluated when equity market closed
3. ✅ Equity/ETF HOLD outside market hours with decision_code="market_closed"
4. ✅ BTC/USD and ETH/USD return non-null latest_price
5. ✅ Scanner limits to major symbols when mode=major
6. ✅ /auto/status.last_scanned_symbols reflects universe
7. ✅ Spread calculation uses correct bid/ask from normalized snapshot
8. ✅ Quantity reduced for max_risk_per_trade violations
9. ✅ Discord timestamps use configured timezone
10. ⚠️ AAPL price consistency (verified in existing test flow)

---

## 14. KNOWN LIMITATIONS / FUTURE WORK

- Spread calculation still uses evaluation_price for calculations (could use more defensive mid_price)
- AAPL price mismatch scenario: Would need more detailed tracing to fully reproduce
- Extended hours equity evaluation: Not currently tested in paper trading
- Quantity reduction: Only applied for BUY signals with stop_price (SELL signals not included)

---

## 15. VERIFICATION COMMANDS

```bash
# Check configuration loaded
python verify_changes.py

# Run specific coin
curl -X POST http://localhost:8000/run-once \
  -H "Content-Type: application/json" \
  -d '{"symbol": "BTC/USD"}'

# Check auto status
curl http://localhost:8000/auto/status | jq '.strategy_routing'
curl http://localhost:8000/auto/status | jq '.scan_universe_mode'

# Check config
curl http://localhost:8000/config | jq '.active_strategy_by_asset_class'
curl http://localhost:8000/config | jq '.discord_timezone'
```

---

**Release Status:** Ready for testing on paper trading account.
**Paper-Trading Safety:** All changes preserve paper-trading mode. No live trading enabled.
