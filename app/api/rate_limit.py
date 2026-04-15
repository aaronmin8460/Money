from __future__ import annotations

from typing import Callable, TypeVar

from fastapi.exception_handlers import http_exception_handler
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config.settings import Settings, get_settings
from app.monitoring.logger import get_logger

logger = get_logger("api.rate_limit")

_settings = get_settings()
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],
    headers_enabled=_settings.rate_limit_headers_enabled,
    storage_uri=_settings.rate_limit_storage_uri,
    enabled=True,
)

F = TypeVar("F", bound=Callable[..., object])


def configure_rate_limiter(app: FastAPI, settings: Settings | None = None) -> None:
    resolved = settings or get_settings()
    limiter.enabled = resolved.rate_limit_enabled
    limiter._headers_enabled = resolved.rate_limit_headers_enabled
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.add_exception_handler(StarletteHTTPException, rate_limit_http_exception_handler)


async def rate_limit_exceeded_handler(request: Request, exc: Exception) -> JSONResponse:
    limit_description = str(getattr(exc, "detail", "") or getattr(exc, "limit", "") or "rate limit exceeded")
    logger.warning(
        "API request rejected by rate limit",
        extra={
            "path": request.url.path,
            "method": request.method,
            "client": get_remote_address(request),
            "limit": limit_description,
        },
    )
    response = JSONResponse(
        status_code=429,
        content={
            "error": "rate_limit_exceeded",
            "detail": "Too many requests. Please retry later.",
            "limit": limit_description,
            "path": request.url.path,
        },
    )
    current_limit = getattr(getattr(request, "state", None), "view_rate_limit", None)
    return request.app.state.limiter._inject_headers(response, current_limit)


async def rate_limit_http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 429:
        return await rate_limit_exceeded_handler(request, exc)
    return await http_exception_handler(request, exc)


def rate_limit_default() -> Callable[[F], F]:
    return limiter.limit(lambda: get_settings().rate_limit_default, override_defaults=False)


def rate_limit_scanner() -> Callable[[F], F]:
    return limiter.limit(lambda: get_settings().rate_limit_scanner)


def rate_limit_admin() -> Callable[[F], F]:
    return limiter.limit(lambda: get_settings().rate_limit_admin)


def rate_limit_market() -> Callable[[F], F]:
    return limiter.limit(lambda: get_settings().rate_limit_market)


def rate_limit_signals() -> Callable[[F], F]:
    return limiter.limit(lambda: get_settings().rate_limit_signals)


def rate_limit_health() -> Callable[[F], F]:
    return limiter.limit(
        lambda: get_settings().rate_limit_default,
        override_defaults=False,
        exempt_when=lambda: get_settings().rate_limit_health_exempt,
    )


def rate_limit_exempt() -> Callable[[F], F]:
    return limiter.exempt
