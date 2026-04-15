# Market Data Providers

This refactor decouples broker execution from market data. Alpaca can stay configured as the paper broker while market data routes through free-source providers by asset class.

## What Changed

- `app/services/market_data.py` now exposes provider implementations behind the existing normalized market-data contract.
- `CompositeMarketDataProvider` routes requests by `AssetClass` and keeps provider-specific response shapes out of scanner, strategy, ML, and execution code.
- The scanner uses `batch_snapshot(...)` during scan preparation when available, then reuses prepared snapshots and bars during the same scan cycle.
- Provider calls are protected by per-provider outbound rate limits, in-memory TTL caches, and structured diagnostics.
- Alpaca market-data requests now use bounded retries with exponential backoff, jitter, and `Retry-After` support for 429s.

## Default Routing

Free-mode defaults are:

- `equity -> yfinance`
- `etf -> yfinance`
- `crypto -> coingecko`
- `option -> yfinance`

Alpaca remains available as a selectable market-data provider or fallback, but it is no longer required for normal paper-mode market data.

## Free-Tier Configuration

Use paper-safe broker settings with independent market-data providers:

```dotenv
BROKER_MODE=paper
TRADING_ENABLED=false
AUTO_TRADE_ENABLED=false
LIVE_TRADING_ENABLED=false

MARKET_DATA_PROVIDER_DEFAULT=composite
EQUITY_DATA_PROVIDER=yfinance
ETF_DATA_PROVIDER=yfinance
CRYPTO_DATA_PROVIDER=coingecko
OPTION_DATA_PROVIDER=yfinance
MARKET_DATA_FALLBACK_PROVIDERS=alpaca
PROVIDER_RATE_LIMITS_PER_MINUTE={"alpaca":120,"yfinance":30,"coingecko":25,"tradier":60}
SNAPSHOT_CACHE_TTL_SECONDS=5
INTRADAY_BARS_CACHE_TTL_SECONDS=30
DAILY_BARS_CACHE_TTL_SECONDS=300
OPTION_CHAIN_CACHE_TTL_SECONDS=60
```

CoinGecko works without an API key for demo/free usage. Set `COINGECKO_API_KEY` only if you have one. Tradier is optional and only used when `TRADIER_API_TOKEN` is configured and selected.

## 429 Mitigation

The system reduces upstream pressure in four layers:

- Asset-class routing avoids using Alpaca market data by default.
- Provider caches reuse snapshots, quotes/trades, bars, and option chains for short TTL windows.
- Provider rate limiters throttle outbound calls per provider.
- Alpaca 429s sleep using `Retry-After` when present, otherwise exponential backoff plus jitter, bounded by `MARKET_DATA_MAX_RETRIES`.

Diagnostics expose provider routes, cache stats, recent 429 counts, and fallback counts through admin data-feed and auto diagnostics.

## Options Limitations

Options trading remains disabled by default with `OPTION_TRADING_ENABLED=false`. The options data path is pluggable and has a basic Yahoo Finance chain implementation. Tradier chain support is optional, credential-gated, and not required for free-mode operation.
