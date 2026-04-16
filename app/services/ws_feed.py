from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from app.config.settings import Settings, get_settings
from app.domain.models import AssetClass, QuoteSnapshot
from app.monitoring.logger import get_logger
from app.utils.datetime_parser import parse_iso_datetime

logger = get_logger("ws_feed")


def canonicalize_crypto_symbol(symbol: str) -> str:
    normalized = str(symbol).strip().upper()
    if not normalized:
        return normalized
    if "/" in normalized:
        base, quote = normalized.split("/", 1)
        return f"{base}/{quote}"
    for suffix in ("USD", "USDT", "USDC", "BTC", "ETH"):
        if normalized.endswith(suffix) and len(normalized) > len(suffix):
            return f"{normalized[:-len(suffix)]}/{suffix}"
    return normalized


def _looks_like_crypto_symbol(symbol: str) -> bool:
    normalized = canonicalize_crypto_symbol(symbol)
    if not normalized:
        return False
    if "/" not in normalized:
        return False
    base, quote = normalized.split("/", 1)
    return bool(base) and quote in {"USD", "USDT", "USDC", "BTC", "ETH"}


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class WebSocketConnection(Protocol):
    def send_text(self, payload: str) -> None:
        ...

    def recv_text(self, timeout: float | None = None) -> str:
        ...

    def close(self) -> None:
        ...


class _StdlibWebSocketConnection:
    def __init__(
        self,
        url: str,
        *,
        timeout: float = 10.0,
        extra_headers: dict[str, str] | None = None,
    ):
        self.url = url
        self.timeout = timeout
        self._extra_headers = extra_headers or {}
        self._socket = self._connect()
        self._send_lock = threading.Lock()
        self._closed = False

    def _connect(self) -> socket.socket:
        parsed = urlparse(self.url)
        if parsed.scheme not in {"ws", "wss"}:
            raise ValueError(f"Unsupported websocket scheme: {parsed.scheme!r}")
        host = parsed.hostname
        if not host:
            raise ValueError(f"Websocket URL is missing a hostname: {self.url!r}")
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        raw_socket = socket.create_connection((host, port), timeout=self.timeout)
        if parsed.scheme == "wss":
            context = ssl.create_default_context()
            stream = context.wrap_socket(raw_socket, server_hostname=host)
        else:
            stream = raw_socket
        stream.settimeout(self.timeout)

        websocket_key = base64.b64encode(os.urandom(16)).decode("ascii")
        headers = {
            "Host": host if parsed.port is None else f"{host}:{port}",
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": websocket_key,
            "Sec-WebSocket-Version": "13",
            "User-Agent": "money-ws-feed/1.0",
        }
        headers.update(self._extra_headers)
        request_lines = [f"GET {path} HTTP/1.1", *[f"{name}: {value}" for name, value in headers.items()], "", ""]
        stream.sendall("\r\n".join(request_lines).encode("ascii"))

        response_headers = self._read_http_headers(stream)
        status_line = response_headers.pop("__status__", "")
        if " 101 " not in status_line:
            raise RuntimeError(f"Websocket upgrade failed: {status_line or 'missing status line'}")

        expected_accept = base64.b64encode(
            hashlib.sha1(f"{websocket_key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11".encode("ascii")).digest()
        ).decode("ascii")
        actual_accept = response_headers.get("sec-websocket-accept")
        if actual_accept != expected_accept:
            raise RuntimeError("Websocket handshake validation failed.")
        return stream

    def _read_http_headers(self, stream: socket.socket) -> dict[str, str]:
        buffer = bytearray()
        while b"\r\n\r\n" not in buffer:
            chunk = stream.recv(4096)
            if not chunk:
                raise ConnectionError("Socket closed during websocket handshake.")
            buffer.extend(chunk)
            if len(buffer) > 64 * 1024:
                raise RuntimeError("Websocket handshake headers exceeded 64KB.")
        header_bytes, _separator, _remainder = bytes(buffer).partition(b"\r\n\r\n")
        lines = header_bytes.decode("latin-1").split("\r\n")
        headers = {"__status__": lines[0] if lines else ""}
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
        return headers

    def _read_exact(self, size: int) -> bytes:
        buffer = bytearray()
        while len(buffer) < size:
            chunk = self._socket.recv(size - len(buffer))
            if not chunk:
                raise ConnectionError("Websocket socket closed unexpectedly.")
            buffer.extend(chunk)
        return bytes(buffer)

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        if self._closed:
            return
        first_byte = 0x80 | (opcode & 0x0F)
        mask_bit = 0x80
        payload_length = len(payload)
        header = bytearray([first_byte])
        if payload_length < 126:
            header.append(mask_bit | payload_length)
        elif payload_length < (1 << 16):
            header.append(mask_bit | 126)
            header.extend(payload_length.to_bytes(2, byteorder="big"))
        else:
            header.append(mask_bit | 127)
            header.extend(payload_length.to_bytes(8, byteorder="big"))
        mask = os.urandom(4)
        masked_payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        frame = bytes(header) + mask + masked_payload
        with self._send_lock:
            self._socket.sendall(frame)

    def send_text(self, payload: str) -> None:
        self._send_frame(0x1, payload.encode("utf-8"))

    def recv_text(self, timeout: float | None = None) -> str:
        previous_timeout = self._socket.gettimeout()
        self._socket.settimeout(timeout if timeout is not None else previous_timeout)
        fragments: list[bytes] = []
        receiving_text = False
        try:
            while True:
                header = self._read_exact(2)
                first_byte, second_byte = header
                fin = bool(first_byte & 0x80)
                opcode = first_byte & 0x0F
                masked = bool(second_byte & 0x80)
                payload_length = second_byte & 0x7F
                if payload_length == 126:
                    payload_length = int.from_bytes(self._read_exact(2), byteorder="big")
                elif payload_length == 127:
                    payload_length = int.from_bytes(self._read_exact(8), byteorder="big")
                mask = self._read_exact(4) if masked else None
                payload = bytearray(self._read_exact(payload_length))
                if mask is not None:
                    for index in range(payload_length):
                        payload[index] ^= mask[index % 4]
                message = bytes(payload)

                if opcode == 0x8:
                    self.close()
                    raise ConnectionError("Websocket closed by peer.")
                if opcode == 0x9:
                    self._send_frame(0xA, message)
                    continue
                if opcode == 0xA:
                    continue
                if opcode == 0x1:
                    fragments = [message]
                    if fin:
                        return message.decode("utf-8")
                    receiving_text = True
                    continue
                if opcode == 0x0 and receiving_text:
                    fragments.append(message)
                    if fin:
                        return b"".join(fragments).decode("utf-8")
                    continue
                raise ConnectionError(f"Unsupported websocket opcode: {opcode}")
        except socket.timeout as exc:
            raise TimeoutError("Timed out waiting for websocket message.") from exc
        finally:
            self._socket.settimeout(previous_timeout)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._send_frame(0x8)
        except OSError:
            pass
        self._closed = True
        try:
            self._socket.close()
        except OSError:
            pass


class AlpacaCryptoQuoteFeed:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        connection_factory: Callable[..., WebSocketConnection] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        self.settings = settings or get_settings()
        self.connection_factory = connection_factory or self._default_connection_factory
        self.sleeper = sleeper or time.sleep
        self._latest_quotes: dict[str, QuoteSnapshot] = {}
        self._subscriptions: set[str] = set()
        self._remote_subscriptions: set[str] = set()
        self._lock = threading.RLock()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._connection: WebSocketConnection | None = None
        self._connected = False
        self._authenticated = False
        self._reconnect_attempts = 0
        self._last_connect_at: datetime | None = None
        self._last_message_at: datetime | None = None
        self._last_error: str | None = None

    @staticmethod
    def _default_connection_factory(url: str, *, timeout: float = 10.0) -> WebSocketConnection:
        return _StdlibWebSocketConnection(url, timeout=timeout)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.has_alpaca_credentials)

    @property
    def stream_url(self) -> str:
        base_url = urlparse(str(self.settings.alpaca_data_base_url))
        host = (base_url.hostname or "data.alpaca.markets").lower()
        if host.startswith("data."):
            stream_host = f"stream.{host}"
        elif host.startswith("stream."):
            stream_host = host
        else:
            stream_host = "stream.data.sandbox.alpaca.markets" if "sandbox" in host else "stream.data.alpaca.markets"
        location = str(self.settings.alpaca_crypto_location or "us").strip().lower() or "us"
        return f"wss://{stream_host}/v1beta3/crypto/{location}"

    def subscribe(self, symbols: list[str]) -> list[str]:
        if not self.enabled:
            return []
        normalized_symbols = sorted(
            {
                canonicalize_crypto_symbol(symbol)
                for symbol in symbols
                if _looks_like_crypto_symbol(symbol)
            }
        )
        if not normalized_symbols:
            return []
        with self._lock:
            before = set(self._subscriptions)
            self._subscriptions.update(normalized_symbols)
            changed = sorted(self._subscriptions - before)
        if changed:
            self._ensure_worker_started()
            self._wake_event.set()
        return normalized_symbols

    def get_latest_quote(
        self,
        symbol: str,
        *,
        max_age_seconds: float | None = None,
    ) -> QuoteSnapshot | None:
        resolved_symbol = canonicalize_crypto_symbol(symbol)
        with self._lock:
            quote = self._latest_quotes.get(resolved_symbol)
        if quote is None:
            return None
        if max_age_seconds is not None and max_age_seconds > 0 and quote.timestamp is not None:
            age_seconds = (datetime.now(timezone.utc) - quote.timestamp.astimezone(timezone.utc)).total_seconds()
            if age_seconds > max_age_seconds:
                return None
        return quote

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "stream_url": self.stream_url if self.enabled else None,
                "connected": self._connected,
                "authenticated": self._authenticated,
                "subscribed_symbols": sorted(self._subscriptions),
                "cached_quote_count": len(self._latest_quotes),
                "reconnect_attempts": self._reconnect_attempts,
                "last_connect_at": self._last_connect_at.isoformat() if self._last_connect_at else None,
                "last_message_at": self._last_message_at.isoformat() if self._last_message_at else None,
                "last_error": self._last_error,
            }

    def close(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        with self._lock:
            connection = self._connection
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=2.0)
        with self._lock:
            self._connection = None
            self._connected = False
            self._authenticated = False
            self._worker = None
            self._remote_subscriptions = set()

    def _ensure_worker_started(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop_event.clear()
            self._worker = threading.Thread(
                target=self._run,
                name="alpaca-crypto-quote-feed",
                daemon=True,
            )
            self._worker.start()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if not self.enabled:
                return
            with self._lock:
                has_subscriptions = bool(self._subscriptions)
            if not has_subscriptions:
                self._wake_event.wait(timeout=0.25)
                self._wake_event.clear()
                continue
            try:
                self._stream_once()
                self._reconnect_attempts = 0
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                self._reconnect_attempts += 1
                self._last_error = str(exc)
                logger.warning(
                    "Crypto quote websocket disconnected",
                    extra={
                        "provider": "alpaca_ws",
                        "error": str(exc),
                        "attempt": self._reconnect_attempts,
                    },
                )
                delay = min(10.0, float(self._reconnect_attempts))
                self._wait(delay)

    def _wait(self, delay_seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, delay_seconds)
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            sleep_for = min(0.25, remaining)
            self.sleeper(sleep_for)
            if self._wake_event.is_set():
                self._wake_event.clear()

    def _stream_once(self) -> None:
        connection = self.connection_factory(self.stream_url, timeout=10.0)
        with self._lock:
            self._connection = connection
            self._connected = True
            self._authenticated = False
            self._remote_subscriptions = set()
            self._last_connect_at = datetime.now(timezone.utc)
            self._last_error = None
        try:
            welcome = connection.recv_text(timeout=5.0)
            self._handle_raw_message(welcome)
            connection.send_text(
                json.dumps(
                    {
                        "action": "auth",
                        "key": self.settings.alpaca_api_key,
                        "secret": self.settings.alpaca_secret_key,
                    }
                )
            )
            auth_deadline = time.monotonic() + 5.0
            while not self._stop_event.is_set() and time.monotonic() < auth_deadline:
                if self._authenticated:
                    break
                payload = connection.recv_text(timeout=1.0)
                self._handle_raw_message(payload)
            if not self._authenticated:
                raise RuntimeError("Timed out waiting for websocket authentication.")

            while not self._stop_event.is_set():
                self._sync_subscriptions(connection)
                try:
                    payload = connection.recv_text(timeout=1.0)
                except TimeoutError:
                    continue
                self._handle_raw_message(payload)
        finally:
            with self._lock:
                self._connection = None
                self._connected = False
                self._authenticated = False
                self._remote_subscriptions = set()
            try:
                connection.close()
            except Exception:
                pass

    def _sync_subscriptions(self, connection: WebSocketConnection) -> None:
        if not self._authenticated:
            return
        with self._lock:
            pending = sorted(self._subscriptions - self._remote_subscriptions)
        if not pending:
            return
        connection.send_text(json.dumps({"action": "subscribe", "quotes": pending}))

    def _handle_raw_message(self, payload: str) -> None:
        parsed = json.loads(payload)
        messages = parsed if isinstance(parsed, list) else [parsed]
        for message in messages:
            if not isinstance(message, dict):
                continue
            self._last_message_at = datetime.now(timezone.utc)
            message_type = str(message.get("T") or "")
            if message_type == "success":
                if str(message.get("msg") or "").lower() == "authenticated":
                    with self._lock:
                        self._authenticated = True
                continue
            if message_type == "subscription":
                quotes = {
                    canonicalize_crypto_symbol(symbol)
                    for symbol in (message.get("quotes") or [])
                    if _looks_like_crypto_symbol(symbol)
                }
                with self._lock:
                    self._remote_subscriptions = quotes
                continue
            if message_type == "q":
                self._update_quote(message)
                continue
            if message_type == "error":
                code = message.get("code")
                detail = message.get("msg") or "unknown websocket error"
                raise RuntimeError(f"Alpaca websocket error {code}: {detail}")

    def _update_quote(self, message: dict[str, Any]) -> None:
        symbol = canonicalize_crypto_symbol(str(message.get("S") or ""))
        if not _looks_like_crypto_symbol(symbol):
            return
        timestamp_value = message.get("t")
        timestamp = parse_iso_datetime(timestamp_value) if timestamp_value else datetime.now(timezone.utc)
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        quote = QuoteSnapshot(
            symbol=symbol,
            asset_class=AssetClass.CRYPTO,
            bid_price=_safe_float(message.get("bp")),
            bid_size=_safe_float(message.get("bs")),
            ask_price=_safe_float(message.get("ap")),
            ask_size=_safe_float(message.get("as")),
            timestamp=timestamp,
        )
        with self._lock:
            self._latest_quotes[symbol] = quote
