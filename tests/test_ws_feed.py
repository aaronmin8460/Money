from __future__ import annotations

import json
import threading
import time
from collections import deque

from app.config.settings import Settings
from app.services.ws_feed import AlpacaCryptoQuoteFeed


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FakeWebSocketConnection:
    def __init__(self, messages: list[object]):
        self._messages = deque(messages)
        self.sent_payloads: list[dict[str, object]] = []
        self.closed = False

    def send_text(self, payload: str) -> None:
        self.sent_payloads.append(json.loads(payload))

    def recv_text(self, timeout: float | None = None) -> str:
        if self.closed:
            raise ConnectionError("connection closed")
        if self._messages:
            item = self._messages.popleft()
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, str):
                return item
            return json.dumps(item)
        raise TimeoutError("no message available")

    def close(self) -> None:
        self.closed = True


def test_alpaca_crypto_quote_feed_caches_live_quotes() -> None:
    connection = FakeWebSocketConnection(
        [
            [{"T": "success", "msg": "connected"}],
            [{"T": "success", "msg": "authenticated"}],
            [{"T": "subscription", "quotes": ["BTC/USD"]}],
            [{"T": "q", "S": "BTC/USD", "bp": 64000.0, "bs": 0.5, "ap": 64010.0, "as": 0.4, "t": "2026-04-16T10:00:00Z"}],
        ]
    )
    created_connections: list[FakeWebSocketConnection] = []

    def connection_factory(_url: str, *, timeout: float = 10.0) -> FakeWebSocketConnection:
        assert timeout == 10.0
        created_connections.append(connection)
        return connection

    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        alpaca_api_key="key",
        alpaca_secret_key="secret",
    )
    feed = AlpacaCryptoQuoteFeed(settings=settings, connection_factory=connection_factory, sleeper=lambda _delay: None)

    try:
        subscribed = feed.subscribe(["BTCUSD"])

        assert subscribed == ["BTC/USD"]
        assert _wait_for(lambda: feed.get_latest_quote("BTC/USD") is not None)
        quote = feed.get_latest_quote("BTC/USD")
        assert quote is not None
        assert quote.bid_price == 64000.0
        assert quote.ask_price == 64010.0
        assert created_connections == [connection]
        assert connection.sent_payloads[0] == {"action": "auth", "key": "key", "secret": "secret"}
        assert connection.sent_payloads[1] == {"action": "subscribe", "quotes": ["BTC/USD"]}
    finally:
        feed.close()


def test_alpaca_crypto_quote_feed_reconnects_after_disconnect() -> None:
    first_connection = FakeWebSocketConnection(
        [
            [{"T": "success", "msg": "connected"}],
            [{"T": "success", "msg": "authenticated"}],
            [{"T": "subscription", "quotes": ["ETH/USD"]}],
            [{"T": "q", "S": "ETH/USD", "bp": 3000.0, "bs": 2.0, "ap": 3001.0, "as": 1.5, "t": "2026-04-16T10:00:00Z"}],
            ConnectionError("stream dropped"),
        ]
    )
    second_connection = FakeWebSocketConnection(
        [
            [{"T": "success", "msg": "connected"}],
            [{"T": "success", "msg": "authenticated"}],
            [{"T": "subscription", "quotes": ["ETH/USD"]}],
            [{"T": "q", "S": "ETH/USD", "bp": 3010.0, "bs": 2.0, "ap": 3011.0, "as": 1.5, "t": "2026-04-16T10:00:05Z"}],
        ]
    )
    connections = deque([first_connection, second_connection])
    created_connections: list[FakeWebSocketConnection] = []
    reconnected = threading.Event()

    def connection_factory(_url: str, *, timeout: float = 10.0) -> FakeWebSocketConnection:
        assert timeout == 10.0
        connection = connections.popleft()
        created_connections.append(connection)
        if len(created_connections) >= 2:
            reconnected.set()
        return connection

    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        alpaca_api_key="key",
        alpaca_secret_key="secret",
    )
    feed = AlpacaCryptoQuoteFeed(settings=settings, connection_factory=connection_factory, sleeper=lambda _delay: None)

    try:
        feed.subscribe(["ETH/USD"])

        assert reconnected.wait(timeout=2.0)
        assert _wait_for(
            lambda: (
                feed.get_latest_quote("ETH/USD") is not None
                and feed.get_latest_quote("ETH/USD").bid_price == 3010.0
            )
        )
        quote = feed.get_latest_quote("ETH/USD")
        assert quote is not None
        assert quote.bid_price == 3010.0
        assert len(created_connections) == 2
        assert second_connection.sent_payloads[1] == {"action": "subscribe", "quotes": ["ETH/USD"]}
    finally:
        feed.close()
