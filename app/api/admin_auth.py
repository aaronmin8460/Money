from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from app.config.settings import Settings, get_settings


def _raise_unauthorized(detail: str) -> None:
    raise HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _extract_presented_token(authorization: str | None, x_admin_token: str | None) -> str | None:
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
        if not x_admin_token or not x_admin_token.strip():
            _raise_unauthorized("Authorization header must use the Bearer scheme.")
    if x_admin_token and x_admin_token.strip():
        return x_admin_token.strip()
    return None


def require_admin_auth(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
    x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
) -> None:
    presented_token = _extract_presented_token(authorization, x_admin_token)
    if not presented_token:
        _raise_unauthorized("Admin token required.")

    configured_token = settings.api_admin_token
    if not configured_token:
        raise HTTPException(
            status_code=403,
            detail="Admin API auth is not configured. Set API_ADMIN_TOKEN to enable protected routes.",
        )

    if not secrets.compare_digest(presented_token, configured_token):
        raise HTTPException(status_code=403, detail="Invalid admin token.")
