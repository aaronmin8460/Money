from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pandas as pd
import pytest

from app.config.settings import Settings
from app.domain.models import AssetClass, NormalizedMarketSnapshot, QuoteSnapshot, TradeSnapshot
from app.services.market_data import (
    AlpacaMarketDataProvider,
    AlpacaMarketDataService,
    CoinGeckoMarketDataProvider,
    CompositeMarketDataProvider,
    CSVMarketDataService,
    TTLCache,
    YahooFinanceMarketDataProvider,
    coingecko_coin_id_for_symbol,
    infer_asset_class,
)
from app.strategies.base import Signal


class TestAlpacaMarketDataService:
    """Test suite for AlpacaMarketDataService with Alpaca API mocking."""

    @pytest.fixture
    def settings(self):
        """Create test settings with Alpaca credentials (no env file)."""
        # Create settings with explicit test values to avoid .env leakage
        s = Settings(
            _env_file=None,
            alpaca_api_key="test_key",
            alpaca_secret_key="test_secret",
            alpaca_data_base_url="https://data.alpaca.markets",
            default_timeframe="1D",
        )
        return s

    @pytest.fixture
    def service(self, settings):
        """Create AlpacaMarketDataService instance."""
        return AlpacaMarketDataService(settings)

    def test_compute_historical_window_daily(self, service):
        """Test historical window calculation for daily bars."""
        start, end = service._compute_historical_window("1D", 260)
        
        # Parse the ISO strings
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        
        # For 260 daily bars with 2.5x multiplier, expect ~650+ calendar days lookback
        days_back = (end_dt - start_dt).days
        assert days_back > 600, f"Expected >600 days lookback for 260 limit, got {days_back}"

    def test_compute_historical_window_hourly(self, service):
        """Test historical window calculation for hourly bars."""
        start, end = service._compute_historical_window("1H", 100)
        
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        
        # For 100 hourly bars, expect reasonable lookback
        days_back = (end_dt - start_dt).days
        assert 10 < days_back < 30, f"Expected ~15 days lookback for 100 hourly bars, got {days_back}"

    @patch("httpx.Client.get")
    def test_fetch_bars_sends_proper_params(self, mock_get, service, settings):
        """Verify fetch_bars sends start, end, timeframe, limit, sort, and feed=iex to Alpaca."""
        # Mock response with sample bars
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "bars": [
                {"t": "2024-01-01T00:00:00Z", "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 1000},
                {"t": "2024-01-02T00:00:00Z", "o": 100.5, "h": 102, "l": 99.5, "c": 101, "v": 1100},
            ]
        }
        mock_get.return_value = mock_response

        # Call fetch_bars
        df = service.fetch_bars("AAPL", timeframe="1D", limit=50)

        # Verify the call was made with correct params
        mock_get.assert_called_once()
        call_args = mock_get.call_args
        assert "/v2/stocks/AAPL/bars" in call_args[0][0]
        
        params = call_args[1]["params"]
        assert params["timeframe"] == "1D"
        assert params["limit"] == 50
        assert params["sort"] == "asc"
        assert params["feed"] == "iex"  # ✅ Verify free-tier IEX feed is used
        assert "start" in params
        assert "end" in params
        assert params["start"].endswith("Z")
        assert params["end"].endswith("Z")

    @patch("httpx.Client.get")
    def test_fetch_bars_converts_alpaca_format(self, mock_get, service):
        """Verify fetch_bars correctly converts Alpaca bar format to DataFrame."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "bars": [
                {"t": "2024-01-01T00:00:00Z", "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 1000},
                {"t": "2024-01-02T00:00:00Z", "o": 100.5, "h": 102.0, "l": 99.5, "c": 101.0, "v": 1100},
            ]
        }
        mock_get.return_value = mock_response

        df = service.fetch_bars("AAPL", limit=2)

        assert "Date" in df.columns
        assert "Open" in df.columns
        assert "High" in df.columns
        assert "Low" in df.columns
        assert "Close" in df.columns
        assert "Volume" in df.columns

        assert len(df) == 2
        assert df.iloc[0]["Close"] == 100.5
        assert df.iloc[1]["Close"] == 101.0

    @patch("httpx.Client.get")
    def test_fetch_bars_sorted_ascending(self, mock_get, service):
        """Verify returned DataFrame is sorted ascending by Date."""
        # Return bars in descending order to test sorting
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "bars": [
                {"t": "2024-01-03T00:00:00Z", "o": 101, "h": 102, "l": 100, "c": 101.5, "v": 1300},
                {"t": "2024-01-02T00:00:00Z", "o": 100.5, "h": 102, "l": 99.5, "c": 101, "v": 1100},
                {"t": "2024-01-01T00:00:00Z", "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 1000},
            ]
        }
        mock_get.return_value = mock_response

        df = service.fetch_bars("AAPL", limit=3)

        # Verify Date is parsed and sorted
        assert df.iloc[0]["Date"] == pd.Timestamp("2024-01-01T00:00:00Z")
        assert df.iloc[1]["Date"] == pd.Timestamp("2024-01-02T00:00:00Z")
        assert df.iloc[2]["Date"] == pd.Timestamp("2024-01-03T00:00:00Z")

    @patch("httpx.Client.get")
    def test_fetch_bars_empty_response_raises_error(self, mock_get, service):
        """Verify empty bar response raises RuntimeError."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"bars": []}
        mock_get.return_value = mock_response

        with pytest.raises(RuntimeError, match="No bar data returned"):
            service.fetch_bars("AAPL", limit=50)

    @patch("httpx.Client.get")
    def test_fetch_bars_returns_sufficient_data_for_strategy(self, mock_get, service):
        """Verify fetch_bars returns enough rows for strategy (>200 for daily regime filter)."""
        # Generate 250 mock bars
        bars = [
            {
                "t": f"2023-{(i // 250)%12 + 1:02d}-{(i % 250) // 10 + 1:02d}T00:00:00Z",
                "o": 100 + i * 0.1,
                "h": 101 + i * 0.1,
                "l": 99 + i * 0.1,
                "c": 100.5 + i * 0.1,
                "v": 1000 + i * 10,
            }
            for i in range(250)
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = {"bars": bars}
        mock_get.return_value = mock_response

        df = service.fetch_bars("AAPL", limit=250)

        assert len(df) == 250
        assert len(df) >= 200  # Minimum for strategy

    @patch("httpx.Client.get")
    def test_strategy_no_longer_fails_on_insufficient_data(self, mock_get, service, settings):
        """Verify strategy receives sufficient data and doesn't fail solely due to 1-row limitation."""
        from app.strategies.regime_momentum_breakout import RegimeMomentumBreakoutStrategy

        # Generate 250 bars for both symbol and benchmark with proper dates
        bars = []
        base_date = datetime(2024, 1, 1)
        for i in range(250):
            date = base_date + timedelta(days=i)
            bars.append({
                "t": date.strftime("%Y-%m-%dT00:00:00Z"),
                "o": 100 + i * 0.1,
                "h": 101 + i * 0.1,
                "l": 99 + i * 0.1,
                "c": 100.5 + i * 0.1,
                "v": 1000 + i * 10,
            })
        
        mock_response = MagicMock()
        mock_response.json.return_value = {"bars": bars}
        mock_get.return_value = mock_response

        # Fetch bars as the auto-trader would
        symbol_df = service.fetch_bars("AAPL", limit=260)
        benchmark_df = service.fetch_bars("SPY", limit=220)

        assert len(symbol_df) == 250
        assert len(benchmark_df) == 250

        # Verify strategy doesn't fail on insufficient data
        strategy = RegimeMomentumBreakoutStrategy()
        input_data = {"symbol": symbol_df, "benchmark": benchmark_df}
        signals = strategy.generate_signals("AAPL", input_data)

        assert signals
        # Should not be "Insufficient data" failure; may be HOLD for other reasons
        assert signals[-1].reason != "Insufficient data", "Strategy still failing on insufficient data"

    @patch("httpx.Client.get")
    def test_get_latest_price(self, mock_get, service):
        """Verify get_latest_price still works correctly."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "bars": [
                {"t": "2024-01-01T00:00:00Z", "o": 100, "h": 101, "l": 99, "c": 150.75, "v": 1000},
            ]
        }
        mock_get.return_value = mock_response

        price = service.get_latest_price("AAPL")
        assert price == 150.75

    def test_get_normalized_snapshot_parses_nanosecond_stock_timestamps(self, service):
        service._request_json = MagicMock(
            side_effect=[
                {"trade": {"t": "2026-04-11T17:24:46.344345659+00:00", "p": 150.75, "s": 10}},
                {"quote": {"t": "2026-04-11T17:24:46.344345659+00:00", "bp": 150.5, "bs": 12, "ap": 151.0, "as": 8}},
                {
                    "bars": [
                        {
                            "t": "2026-04-11T17:24:46.344345659+00:00",
                            "o": 150.0,
                            "h": 151.5,
                            "l": 149.8,
                            "c": 150.9,
                            "v": 1000,
                        }
                    ]
                },
            ]
        )

        snapshot = service.get_normalized_snapshot("AAPL", AssetClass.EQUITY)

        expected_timestamp = datetime(2026, 4, 11, 17, 24, 46, 344345, tzinfo=timezone.utc)
        assert snapshot.trade_timestamp == expected_timestamp
        assert snapshot.quote_timestamp == expected_timestamp
        assert snapshot.source_timestamp == expected_timestamp

    def test_get_normalized_snapshot_parses_nanosecond_crypto_timestamps(self, service):
        service._request_json = MagicMock(
            side_effect=[
                {"trades": {"BTC/USD": {"t": "2026-04-10T19:59:56.248717591+00:00", "p": 84000.0, "s": 0.25}}},
                {"orderbooks": {"BTC/USD": {"a": [{"p": 84010.0, "s": 0.5}], "b": [{"p": 83990.0, "s": 0.4}]}}},
                {
                    "bars": {
                        "BTC/USD": [
                            {
                                "t": "2026-04-10T19:59:56.248717591+00:00",
                                "o": 83800.0,
                                "h": 84200.0,
                                "l": 83750.0,
                                "c": 84050.0,
                                "v": 12.5,
                            }
                        ]
                    }
                },
            ]
        )

        snapshot = service.get_normalized_snapshot("BTC/USD", AssetClass.CRYPTO)

        expected_trade_timestamp = datetime(2026, 4, 10, 19, 59, 56, 248717, tzinfo=timezone.utc)
        assert snapshot.trade_timestamp == expected_trade_timestamp
        assert snapshot.last_trade_price == 84000.0
        assert snapshot.evaluation_price == 84000.0


class TestCSVMarketDataService:
    """Test suite for CSVMarketDataService."""

    def test_csv_service_fetch_bars(self):
        """Verify CSVMarketDataService loads symbol-specific files."""
        service = CSVMarketDataService()
        df = service.fetch_bars("AAPL", limit=10)
        
        # Should return a DataFrame with expected columns
        assert "Date" in df.columns or "Close" in df.columns
        assert len(df) <= 10

    def test_csv_service_uses_symbol_specific_files(self, tmp_path):
        aapl_path = tmp_path / "AAPL.csv"
        spy_path = tmp_path / "SPY.csv"
        aapl_path.write_text(
            "Date,Open,High,Low,Close,Volume\n2024-01-01,1,2,0,10,100\n",
            encoding="utf-8",
        )
        spy_path.write_text(
            "Date,Open,High,Low,Close,Volume\n2024-01-01,1,2,0,20,100\n",
            encoding="utf-8",
        )

        service = CSVMarketDataService(data_dir=tmp_path)

        aapl = service.fetch_bars("AAPL", limit=1)
        spy = service.fetch_bars("SPY", limit=1)

        assert float(aapl.iloc[-1]["Close"]) == 10.0
        assert float(spy.iloc[-1]["Close"]) == 20.0
        assert service.get_latest_price("SPY") == 20.0

    def test_csv_service_rejects_unsupported_symbols(self, tmp_path):
        service = CSVMarketDataService(data_dir=tmp_path)

        with pytest.raises(FileNotFoundError, match="MSFT"):
            service.fetch_bars("MSFT", limit=1)


def test_infer_asset_class_recognizes_generic_crypto_pairs() -> None:
    assert infer_asset_class("AVAX/USD") == AssetClass.CRYPTO
    assert infer_asset_class("LINK/USD") == AssetClass.CRYPTO
    assert infer_asset_class("DOGEUSD") == AssetClass.CRYPTO


class TestMarketDataProtocol:
    """Test that both services conform to MarketDataService protocol."""

    def test_csv_service_implements_protocol(self):
        """Verify CSVMarketDataService implements the protocol."""
        service = CSVMarketDataService()
        assert hasattr(service, "fetch_bars")
        assert hasattr(service, "get_latest_price")
        assert hasattr(service, "load_historical")

    def test_alpaca_service_implements_protocol(self):
        """Verify AlpacaMarketDataService implements the protocol."""
        settings = Settings(
            _env_file=None,
            alpaca_api_key="test",
            alpaca_secret_key="test",
        )
        service = AlpacaMarketDataService(settings)
        assert hasattr(service, "fetch_bars")
        assert hasattr(service, "get_latest_price")


def test_composite_provider_routes_by_asset_class() -> None:
    settings = Settings(_env_file=None, broker_mode="mock", trading_enabled=False)

    class StubProvider:
        def __init__(self, name: str, price: float):
            self.provider_name = name
            self.price = price

        def get_latest_price(self, symbol, asset_class=None):
            return self.price

    composite = CompositeMarketDataProvider(
        settings,
        providers={
            "yfinance": StubProvider("yfinance", 101.0),
            "coingecko": StubProvider("coingecko", 60000.0),
        },
        route_by_asset_class={
            AssetClass.EQUITY: "yfinance",
            AssetClass.ETF: "yfinance",
            AssetClass.CRYPTO: "coingecko",
            AssetClass.OPTION: "yfinance",
        },
        fallback_provider_names=[],
    )

    assert composite.get_latest_price("AAPL", AssetClass.EQUITY) == 101.0
    assert composite.get_latest_price("BTC/USD", AssetClass.CRYPTO) == 60000.0


def test_composite_provider_falls_back_when_primary_fails() -> None:
    settings = Settings(_env_file=None, broker_mode="mock", trading_enabled=False)

    class FailingProvider:
        provider_name = "primary"

        def get_latest_price(self, symbol, asset_class=None):
            raise RuntimeError("primary unavailable")

    class FallbackProvider:
        provider_name = "fallback"

        def get_latest_price(self, symbol, asset_class=None):
            return 42.0

    composite = CompositeMarketDataProvider(
        settings,
        providers={"primary": FailingProvider(), "fallback": FallbackProvider()},
        route_by_asset_class={AssetClass.EQUITY: "primary"},
        fallback_provider_names=["fallback"],
    )

    assert composite.get_latest_price("AAPL", AssetClass.EQUITY) == 42.0
    assert composite.diagnostics()["recent_fallback_count"] == 1


def test_composite_provider_prefers_cached_live_crypto_quotes() -> None:
    settings = Settings(_env_file=None, broker_mode="mock", trading_enabled=False)
    live_quote = QuoteSnapshot(
        symbol="BTC/USD",
        asset_class=AssetClass.CRYPTO,
        bid_price=64000.0,
        bid_size=0.5,
        ask_price=64010.0,
        ask_size=0.4,
        timestamp=datetime(2026, 4, 16, 10, 0, tzinfo=timezone.utc),
    )

    class StubCryptoProvider:
        provider_name = "coingecko"

        def get_latest_quote(self, symbol, asset_class=None):
            return QuoteSnapshot(symbol="BTC/USD", asset_class=AssetClass.CRYPTO, timestamp=live_quote.timestamp)

        def get_session_status(self, asset_class=None):
            return MagicMock(session_state=MagicMock(value="always_open"))

        def get_latest_trade(self, symbol, asset_class=None):
            return TradeSnapshot(
                symbol="BTC/USD",
                asset_class=AssetClass.CRYPTO,
                price=63990.0,
                size=123.0,
                timestamp=live_quote.timestamp,
            )

        def get_normalized_snapshot(self, symbol, asset_class=None):
            return NormalizedMarketSnapshot(
                symbol="BTC/USD",
                asset_class=AssetClass.CRYPTO,
                last_trade_price=63990.0,
                evaluation_price=63990.0,
                quote_available=False,
                quote_stale=False,
                price_source_used="last_trade",
                trade_timestamp=live_quote.timestamp,
                source_timestamp=live_quote.timestamp,
                session_state="always_open",
                exchange="COINGECKO",
                source="coingecko",
            )

        def batch_snapshot(self, symbols, asset_class=None):
            return {
                "BTC/USD": {
                    "symbol": "BTC/USD",
                    "asset_class": AssetClass.CRYPTO.value,
                    "quote": QuoteSnapshot(
                        symbol="BTC/USD",
                        asset_class=AssetClass.CRYPTO,
                        timestamp=live_quote.timestamp,
                    ).to_dict(),
                    "trade": self.get_latest_trade("BTC/USD", asset_class).to_dict(),
                    "normalized": self.get_normalized_snapshot("BTC/USD", asset_class).to_dict(),
                    "source": "coingecko",
                }
            }

    class FakeQuoteFeed:
        def __init__(self):
            self.subscriptions: list[list[str]] = []

        def subscribe(self, symbols):
            self.subscriptions.append(list(symbols))
            return list(symbols)

        def get_latest_quote(self, symbol, *, max_age_seconds=None):
            assert max_age_seconds == settings.quote_stale_after_seconds
            if symbol == "BTC/USD":
                return live_quote
            return None

        def diagnostics(self):
            return {"enabled": True}

        def close(self):
            return None

    quote_feed = FakeQuoteFeed()
    composite = CompositeMarketDataProvider(
        settings,
        providers={"coingecko": StubCryptoProvider()},
        route_by_asset_class={AssetClass.CRYPTO: "coingecko"},
        fallback_provider_names=[],
        crypto_quote_feed=quote_feed,
    )

    quote = composite.get_latest_quote("BTC/USD", AssetClass.CRYPTO)
    normalized = composite.get_normalized_snapshot("BTC/USD", AssetClass.CRYPTO)
    batch = composite.batch_snapshot(["BTC/USD"], AssetClass.CRYPTO)

    assert quote.bid_price == 64000.0
    assert quote.ask_price == 64010.0
    assert normalized.quote_available is True
    assert normalized.bid_price == 64000.0
    assert normalized.ask_price == 64010.0
    assert normalized.last_trade_price == 63990.0
    assert normalized.source == "coingecko+alpaca_ws"
    assert batch["BTC/USD"]["quote"]["bid_price"] == 64000.0
    assert batch["BTC/USD"]["normalized"]["quote_available"] is True
    assert quote_feed.subscriptions
    assert all("BTC/USD" in symbols for symbols in quote_feed.subscriptions)


def test_composite_provider_preserves_crypto_fallback_without_live_quote() -> None:
    settings = Settings(_env_file=None, broker_mode="mock", trading_enabled=False)

    class StubCryptoProvider:
        provider_name = "coingecko"

        def get_latest_quote(self, symbol, asset_class=None):
            return QuoteSnapshot(
                symbol="BTC/USD",
                asset_class=AssetClass.CRYPTO,
                bid_price=60000.0,
                ask_price=60020.0,
                timestamp=datetime(2026, 4, 16, 10, 0, tzinfo=timezone.utc),
            )

        def get_session_status(self, asset_class=None):
            return MagicMock(session_state=MagicMock(value="always_open"))

    class EmptyQuoteFeed:
        def subscribe(self, symbols):
            return list(symbols)

        def get_latest_quote(self, symbol, *, max_age_seconds=None):
            return None

        def diagnostics(self):
            return {"enabled": True}

        def close(self):
            return None

    composite = CompositeMarketDataProvider(
        settings,
        providers={"coingecko": StubCryptoProvider()},
        route_by_asset_class={AssetClass.CRYPTO: "coingecko"},
        fallback_provider_names=[],
        crypto_quote_feed=EmptyQuoteFeed(),
    )

    quote = composite.get_latest_quote("BTC/USD", AssetClass.CRYPTO)

    assert quote.bid_price == 60000.0
    assert quote.ask_price == 60020.0


def test_alpaca_429_retry_after_backoff_is_respected() -> None:
    sleeps: list[float] = []
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        alpaca_api_key="test",
        alpaca_secret_key="test",
        market_data_max_retries=1,
    )
    service = AlpacaMarketDataProvider(settings, sleeper=sleeps.append)
    request = httpx.Request("GET", "https://data.alpaca.markets/v2/test")
    rate_limited = httpx.Response(429, headers={"Retry-After": "2"}, text="slow down", request=request)
    ok = httpx.Response(200, json={"ok": True}, request=request)
    service.client.get = MagicMock(side_effect=[rate_limited, ok])

    assert service._request_json("/v2/test") == {"ok": True}
    assert sleeps == [2.0]
    assert service.diagnostics()["recent_429_count"] == 1


def test_ttl_cache_hits_and_expires() -> None:
    now = [100.0]
    cache = TTLCache(clock=lambda: now[0])
    key = ("provider", "snapshot", "AAPL")

    cache.set(key, {"price": 1}, ttl_seconds=5)
    assert cache.get(key) == {"price": 1}
    now[0] = 106.0
    assert cache.get(key) is None
    stats = cache.stats()
    assert stats["hits"] == 1
    assert stats["expirations"] == 1


def test_coingecko_symbol_mapping_and_unmapped_failure() -> None:
    assert coingecko_coin_id_for_symbol("BTC/USD") == "bitcoin"
    assert coingecko_coin_id_for_symbol("ETHUSD") == "ethereum"
    assert coingecko_coin_id_for_symbol("ETHUSDT") == "ethereum"
    with pytest.raises(ValueError, match="No CoinGecko symbol mapping"):
        coingecko_coin_id_for_symbol("NOPE/USD")


def test_coingecko_batch_snapshot_normalizes_without_real_network() -> None:
    settings = Settings(_env_file=None, broker_mode="mock", trading_enabled=False)
    provider = CoinGeckoMarketDataProvider(settings)
    provider._request_json = MagicMock(
        return_value={
            "bitcoin": {
                "usd": 60000.0,
                "usd_24h_vol": 123456.0,
                "usd_24h_change": 2.5,
                "last_updated_at": 1710000000,
            }
        }
    )

    snapshots = provider.batch_snapshot(["BTC/USD"], AssetClass.CRYPTO)

    normalized = NormalizedMarketSnapshot.from_dict(snapshots["BTC/USD"]["normalized"])
    assert normalized.symbol == "BTC/USD"
    assert normalized.asset_class == AssetClass.CRYPTO
    assert normalized.evaluation_price == 60000.0
    assert normalized.source == "coingecko"


def test_yfinance_normalized_snapshot_contract_with_partial_quote(monkeypatch) -> None:
    settings = Settings(_env_file=None, broker_mode="mock", trading_enabled=False)
    provider = YahooFinanceMarketDataProvider(settings)
    timestamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(
        provider,
        "get_latest_trade",
        lambda symbol, asset_class: TradeSnapshot(
            symbol=symbol,
            asset_class=AssetClass.EQUITY,
            price=150.0,
            timestamp=timestamp,
        ),
    )
    monkeypatch.setattr(
        provider,
        "get_latest_quote",
        lambda symbol, asset_class: QuoteSnapshot(
            symbol=symbol,
            asset_class=AssetClass.EQUITY,
            timestamp=timestamp,
        ),
    )

    snapshot = provider.get_normalized_snapshot("AAPL", AssetClass.EQUITY)

    assert snapshot.symbol == "AAPL"
    assert snapshot.asset_class == AssetClass.EQUITY
    assert snapshot.evaluation_price == 150.0
    assert snapshot.quote_available is False
    assert snapshot.source == "yfinance"
