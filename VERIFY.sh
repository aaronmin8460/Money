#!/bin/bash
# VERIFICATION COMMANDS FOR MONEY BOT CHANGES
# Run these commands to verify all changes are working

set -e

echo "========================================================================"
echo "MONEY BOT AUTO-TRADING FIXES - VERIFICATION COMMANDS"
echo "========================================================================"

PROJECT_DIR="/Users/byeongilmin/Desktop/Project/Money"
cd "$PROJECT_DIR"

# Activate virtual environment
source .venv/bin/activate

echo ""
echo "[1] Verifying configuration loads correctly..."
python -c "
from app.config.settings import get_settings
s = get_settings()
print(f'✓ Active strategy: {s.active_strategy}')
print(f'✓ Scan universe mode: {s.scan_universe_mode}')
print(f'✓ Strategy routing: {s.active_strategy_by_asset_class}')
print(f'✓ Discord timezone: {s.discord_timezone}')
print(f'✓ Quote stale after: {s.quote_stale_after_seconds}s')
"

echo ""
echo "[2] Verifying module imports..."
python -c "
from app.services.auto_trader import AutoTrader
from app.execution.execution_service import ExecutionService
from app.monitoring.discord_notifier import DiscordNotifier
print('✓ All modules imported successfully')
"

echo ""
echo "[3] Verifying strategy routing by asset class..."
python -c "
from app.domain.models import AssetClass
from app.services.runtime import get_runtime
runtime = get_runtime()
for ac in [AssetClass.EQUITY, AssetClass.ETF, AssetClass.CRYPTO]:
    strat_name = runtime.settings.strategy_for_asset_class(ac)
    strat = runtime.strategy_registry.get(strat_name)
    print(f'✓ {ac.value:6s} -> {strat.name} (supports: {[a.value for a in strat.supported_asset_classes]})')
"

echo ""
echo "[4] Verifying market session status..."
python -c "
from app.domain.models import AssetClass
from app.services.runtime import get_runtime
runtime = get_runtime()
mds = runtime.market_data_service
for ac in [AssetClass.EQUITY, AssetClass.CRYPTO]:
    status = mds.get_session_status(ac)
    print(f'✓ {ac.value:6s} session: {status.session_state.value:15s} is_open={status.is_open} is_24_7={status.is_24_7}')
"

echo ""
echo "[5] Verifying major symbol filtering..."
python -c "
from app.domain.models import AssetClass
from app.services.runtime import get_runtime
runtime = get_runtime()
for ac in [AssetClass.EQUITY, AssetClass.CRYPTO]:
    universe = runtime.asset_catalog.get_scan_universe(ac)
    symbols = [a.symbol for a in universe][:5]
    print(f'✓ {ac.value:6s} universe: {len(universe)} symbols (sample: {symbols})')
"

echo ""
echo "[6] Verifying Discord timestamp formatting with timezone..."
python -c "
from datetime import datetime, timezone
from app.monitoring.discord_notifier import format_readable_notification_timestamp
from app.config.settings import get_settings
settings = get_settings()
now = datetime.now(timezone.utc)
formatted = format_readable_notification_timestamp(now, settings)
print(f'✓ Timestamp: {formatted} (timezone: {settings.discord_timezone})')
"

echo ""
echo "[7] Verifying ExecutionService with risk reduction capability..."
python -c "
from app.execution.execution_service import ExecutionService
from app.services.runtime import get_runtime
runtime = get_runtime()
exec_service = runtime.execution_service
# Check if _attempt_risk_compliant_sizing method exists
has_method = hasattr(exec_service, '_attempt_risk_compliant_sizing')
print(f'✓ ExecutionService._attempt_risk_compliant_sizing: {\"exists\" if has_method else \"MISSING\"}')
"

echo ""
echo "========================================================================"
echo "LOCAL VERIFICATION COMPLETE"
echo "========================================================================"
echo ""
echo "Next steps for remote testing:"
echo "1. Start the FastAPI server: uvicorn app.api.app:app --reload"
echo "2. Run the verification script: python verify_changes.py"
echo "3. Make sample requests:"
echo ""
echo "   # Manual run-once for BTC/USD"
echo "   curl -X POST http://localhost:8000/run-once \\"
echo "     -H 'Content-Type: application/json' \\"
echo "     -d '{\"symbol\": \"BTC/USD\"}'"
echo ""
echo "   # Manual run-once for AAPL"
echo "   curl -X POST http://localhost:8000/run-once \\"
echo "     -H 'Content-Type: application/json' \\"
echo "     -d '{\"symbol\": \"AAPL\"}'"
echo ""
echo "   # Check auto status and strategy routing"
echo "   curl http://localhost:8000/auto/status | jq '.strategy_routing'"
echo ""
echo "   # Check configuration"
echo "   curl http://localhost:8000/config | jq '.active_strategy_by_asset_class'"
echo ""
echo "4. Check Discord notifications if webhook is configured"
echo ""
