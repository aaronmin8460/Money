#!/usr/bin/env python3
"""
Smoke test to verify AlpacaMarketDataService now fetches sufficient historical bars.
"""
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import _bootstrap  # noqa: F401

from app.config.settings import get_settings
from app.services.market_data import AlpacaMarketDataService


def main():
    """Run smoke test."""
    settings = get_settings()
    
    # Only run if we have Alpaca credentials
    if not settings.has_alpaca_credentials:
        print("❌ Alpaca credentials not configured (ALPACA_API_KEY, ALPACA_SECRET_KEY)")
        return False
    
    # Create service
    service = AlpacaMarketDataService(settings)
    
    print("\n🔍 Testing AlpacaMarketDataService._compute_historical_window()...")
    
    # Test daily 260-bar lookback
    start, end = service._compute_historical_window("1D", 260)
    print(f"  260 daily bars: start={start}, end={end}")
    
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    days_back = (end_dt - start_dt).days
    print(f"  → Lookback window: {days_back} calendar days (expected ~650+)")
    
    if days_back < 600:
        print("  ❌ ERROR: Lookback too short!")
        return False
    print("  ✅ Lookback window looks good")
    
    # Test hourly 100-bar lookback
    start, end = service._compute_historical_window("1H", 100)
    print(f"\n  100 hourly bars: start={start}, end={end}")
    
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    days_back = (end_dt - start_dt).days
    print(f"  → Lookback window: {days_back} calendar days (expected 10-30)")
    
    if not (10 < days_back < 30):
        print("  ❌ ERROR: Hourly lookback out of range!")
        return False
    print("  ✅ Hourly lookback looks good")
    
    # Mock HTTP test to verify request params
    print("\n🔍 Testing that _request sends proper params to Alpaca...")
    
    with patch("httpx.Client.get") as mock_get:
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "bars": [
                {"t": "2024-01-01T00:00:00Z", "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 1000},
                {"t": "2024-01-02T00:00:00Z", "o": 100.5, "h": 102, "l": 99.5, "c": 101, "v": 1100},
            ]
        }
        mock_get.return_value = mock_response
        
        df = service.fetch_bars("AAPL", timeframe="1D", limit=100)
        
        # Check that the call was made with the right params
        call_args = mock_get.call_args
        params = call_args[1]["params"]
        
        print(f"  Request URL: {call_args[0][0]}")
        print(f"  Request params:")
        print(f"    - timeframe: {params['timeframe']}")
        print(f"    - limit: {params['limit']}")
        print(f"    - sort: {params['sort']}")
        print(f"    - start: {params['start']}")
        print(f"    - end: {params['end']}")
        
        if params['sort'] != 'asc':
            print("  ❌ ERROR: sort param not 'asc'!")
            return False
        
        if 'start' not in params or 'end' not in params:
            print("  ❌ ERROR: missing start or end params!")
            return False
        
        print("  ✅ Request params look correct")
        
        # Check response handling
        print(f"\n  DataFrame returned:")
        print(f"    - Shape: {df.shape}")
        print(f"    - Columns: {list(df.columns)}")
        print(f"    - Date range: {df['Date'].min()} to {df['Date'].max()}")
        
        required_cols = {"Date", "Open", "High", "Low", "Close", "Volume"}
        if not required_cols.issubset(set(df.columns)):
            print(f"  ❌ ERROR: missing required columns! Have {set(df.columns)}, need {required_cols}")
            return False
        
        print("  ✅ DataFrame format looks correct")
    
    print("\n✅ All smoke tests passed!")
    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
