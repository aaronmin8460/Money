from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.config.settings import Settings
from app.services.market_data import AlpacaMarketDataService, CSVMarketDataService
from app.strategies.base import Signal


class TestAlpacaMarketDataService:
    """Test suite for AlpacaMarketDataService with Alpaca API mocking."""

    @pytest.fixture
    def settings(self):
        """Create test settings with Alpaca credentials (no env file)."""
        # Create settings with explicit test values to avoid .env leakage
        s = Settings(
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


class TestCSVMarketDataService:
    """Test suite for CSVMarketDataService."""

    def test_csv_service_fetch_bars(self):
        """Verify CSVMarketDataService behavior is unchanged."""
        service = CSVMarketDataService()
        df = service.fetch_bars("AAPL", limit=10)
        
        # Should return a DataFrame with expected columns
        assert "Date" in df.columns or "Close" in df.columns
        assert len(df) <= 10


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
        settings = Settings()
        settings.alpaca_api_key = "test"
        settings.alpaca_secret_key = "test"
        service = AlpacaMarketDataService(settings)
        assert hasattr(service, "fetch_bars")
        assert hasattr(service, "get_latest_price")
