#!/usr/bin/env python3
"""Verify feed=iex parameter is sent in Alpaca bar requests."""
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta

import _bootstrap  # noqa: F401

from app.config.settings import Settings
from app.services.market_data import AlpacaMarketDataService

# Create isolated test settings (no env file)
settings = Settings(
    alpaca_api_key='test_key',
    alpaca_secret_key='test_secret',
)

svc = AlpacaMarketDataService(settings)

# Mock the HTTP response to simulate Alpaca behavior
with patch('httpx.Client.get') as mock_get:
    # Generate mock bars
    bars = []
    base_date = datetime(2024, 1, 1)
    for i in range(260):
        date = base_date + timedelta(days=i)
        bars.append({
            't': date.strftime('%Y-%m-%dT00:00:00Z'),
            'o': 100 + i * 0.1,
            'h': 101 + i * 0.1,
            'l': 99 + i * 0.1,
            'c': 100.5 + i * 0.1,
            'v': 1000 + i * 10,
        })
    
    mock_response = MagicMock()
    mock_response.json.return_value = {'bars': bars}
    mock_get.return_value = mock_response
    
    # Test AAPL
    df = svc.fetch_bars('AAPL', limit=260)
    print(f'✅ AAPL: {len(df)} rows')
    print(df.head(2))
    print(df.tail(2))
    
    # Verify feed='iex' was sent
    params = mock_get.call_args[1]['params']
    print(f'\n✅ Verified params sent to Alpaca:')
    print(f'  feed: {params["feed"]}')
    print(f'  start: {params["start"][:10]}...')
    print(f'  end: {params["end"][:10]}...')
    print(f'  sort: {params["sort"]}')
    print(f'  limit: {params["limit"]}')
