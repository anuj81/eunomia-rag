"""Bearer-token auth. Disabled by ``auth.require_auth = false`` for dev."""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request

from ..config import get_settings

logger = logging.getLogger(__name__)


def verify_token(request: Request) -> None:
    settings = get_settings()
    if not settings.auth.require_auth:
        return
    expected = settings.auth.api_key
    if not expected:
        logger.warning(
            "auth.require_auth=true but RAG_API_KEY is not set — rejecting all requests."
        )
        raise HTTPException(status_code=503, detail="Service auth not configured")

    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    presented = header.split(" ", 1)[1].strip()
    if presented != expected:
        raise HTTPException(status_code=401, detail="Invalid token")
