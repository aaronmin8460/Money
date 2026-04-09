from pathlib import Path

from app.config.settings import get_settings
from app.data.historical import load_csv_data
from app.execution.execution_service import ExecutionService
from app.portfolio.portfolio import Portfolio
from app.risk.risk_manager import RiskManager
from app.services.broker import create_broker
from app.services.market_data import AlpacaMarketDataService, CSVMarketDataService
from app.strategies.ema_crossover import EMACrossoverStrategy


def main() -> None:
    settings = get_settings()
    broker = create_broker(settings)
    portfolio = Portfolio()
    risk_manager = RiskManager(portfolio)
    strategy = EMACrossoverStrategy()
    market_data = (
        AlpacaMarketDataService(settings)
        if settings.is_alpaca_mode
        else CSVMarketDataService()
    )
    exec_service = ExecutionService(
        broker=broker,
        portfolio=portfolio,
        risk_manager=risk_manager,
        dry_run=not settings.trading_enabled,
        market_data_service=market_data,
    )

    if settings.is_alpaca_mode:
        print("Retrieving Alpaca market data for a safe dry-run cycle...")
        try:
            data = market_data.fetch_bars(settings.default_symbols[0], limit=50)
        except Exception as exc:
            print(f"Failed to load Alpaca market data: {exc}")
            return
    else:
        sample_path = Path("data/sample.csv")
        if not sample_path.exists():
            print("No sample data found. Please add CSV data to data/sample.csv.")
            return
        data = load_csv_data(sample_path)

    symbol = settings.default_symbols[0] if settings.default_symbols else "AAPL"
    result = exec_service.run_once(symbol, strategy, data)
    print(result)


if __name__ == "__main__":
    main()
