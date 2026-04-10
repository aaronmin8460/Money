from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import httpx
import pandas as pd

from app.config.settings import Settings, get_settings


class MarketDataService(Protocol):
    def get_latest_price(self, symbol: str) -> float:
        ...

    def load_historical(self, csv_path: Path) -> pd.DataFrame:
        ...

    def fetch_bars(self, symbol: str, timeframe: str | None = None, limit: int = 50) -> pd.DataFrame:
        ...


class CSVMarketDataService:
    def __init__(self, data_dir: str | Path = "data"):
        self.data_dir = Path(data_dir)

    def list_supported_symbols(self) -> list[str]:
        symbols: list[str] = []
        if not self.data_dir.exists():
            return symbols

        for csv_path in sorted(self.data_dir.glob("*.csv")):
            stem = csv_path.stem.upper()
            if stem == "SAMPLE":
                continue
            symbols.append(stem)
        return symbols

    def resolve_csv_path(self, symbol: str) -> Path:
        normalized_symbol = symbol.strip().upper()
        candidates = [
            self.data_dir / f"{normalized_symbol}.csv",
            self.data_dir / f"{normalized_symbol.lower()}.csv",
        ]
        for path in candidates:
            if path.exists():
                return path

        supported = self.list_supported_symbols()
        supported_text = ", ".join(supported) if supported else "no symbol CSV files are available"
        raise FileNotFoundError(
            f"Mock market data for symbol '{normalized_symbol}' was not found. "
            f"Add '{self.data_dir / f'{normalized_symbol}.csv'}' or choose one of: {supported_text}."
        )

    def get_latest_price(self, symbol: str) -> float:
        bars = self.fetch_bars(symbol, limit=1)
        return float(bars.iloc[-1]["Close"])

    def load_historical(self, csv_path: Path) -> pd.DataFrame:
        if not csv_path.exists():
            raise FileNotFoundError(f"CSV path not found: {csv_path}")

        df = pd.read_csv(csv_path, parse_dates=["Date"])
        df = df.sort_values("Date").reset_index(drop=True)
        return df

    def fetch_bars(self, symbol: str, timeframe: str | None = None, limit: int = 50) -> pd.DataFrame:
        csv_path = self.resolve_csv_path(symbol)
        df = self.load_historical(csv_path)
        if limit and len(df) > limit:
            df = df.tail(limit)
        return df.reset_index(drop=True)


class AlpacaMarketDataService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        if not self.settings.has_alpaca_credentials:
            raise ValueError(
                "Alpaca market data requires ALPACA_API_KEY and ALPACA_SECRET_KEY."
            )
        self.base_url = str(self.settings.alpaca_data_base_url).rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={
                "APCA-API-KEY-ID": self.settings.alpaca_api_key,
                "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
            },
            timeout=10.0,
        )

    def _compute_historical_window(self, timeframe: str, limit: int) -> tuple[str, str]:
        """
        Compute start and end dates for historical bar retrieval.
        
        For daily bars, use 2.5x lookback to account for non-trading days.
        For intraday, estimate business hours in the lookback.
        
        Returns: (start_iso, end_iso) as ISO format strings.
        """
        end_date = datetime.utcnow()
        
        if timeframe == "1D":
            # For daily bars, assume ~250 trading days/year, or ~5 trading days/week
            # Use 2.5x lookback to ensure we get enough trading days
            calendar_days = int(limit * 2.5) + 10
            start_date = end_date - timedelta(days=calendar_days)
        elif timeframe in ("1H", "4H"):
            # For hourly bars, assume ~8 hours/trading day, add 50% buffer
            trading_days = int((limit / 8) * 1.5) + 5
            start_date = end_date - timedelta(days=trading_days)
        else:
            # For minute-level bars, assume ~390 minutes/trading day
            trading_days = int((limit / 390) * 1.5) + 5
            start_date = end_date - timedelta(days=trading_days)
        
        # ISO format with 'T' separator and 'Z' (UTC)
        start_iso = start_date.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = end_date.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        return start_iso, end_iso

    def _request(self, symbol: str, timeframe: str, limit: int = 1) -> dict[str, Any]:
        """Fetch bars from Alpaca with proper historical window and free-tier feed."""
        start_iso, end_iso = self._compute_historical_window(timeframe, limit)
        
        try:
            response = self.client.get(
                f"/v2/stocks/{symbol}/bars",
                params={
                    "timeframe": timeframe,
                    "start": start_iso,
                    "end": end_iso,
                    "limit": limit,
                    "sort": "asc",
                    "feed": "iex",  # Use IEX feed (free tier, works on basic/paper accounts)
                },
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"Alpaca market data error {exc.response.status_code}: {exc.response.text}"
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"Alpaca market data request failed: {exc}") from exc

    def fetch_bars(self, symbol: str, timeframe: str | None = None, limit: int = 50) -> pd.DataFrame:
        timeframe = timeframe or self.settings.default_timeframe
        result = self._request(symbol, timeframe, limit=limit)
        bars = result.get("bars") or []
        if not bars:
            raise RuntimeError(f"No bar data returned for symbol {symbol}")

        df = pd.DataFrame(bars)
        df = df.rename(columns={"t": "Date", "o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
        df["Date"] = pd.to_datetime(df["Date"])
        return df.sort_values("Date").reset_index(drop=True)

    def get_latest_price(self, symbol: str) -> float:
        bars = self.fetch_bars(symbol, timeframe=self.settings.default_timeframe, limit=1)
        return float(bars.iloc[-1]["Close"])
