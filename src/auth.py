from __future__ import annotations

from fastapi import Depends, HTTPException, Query, Request, Security
from fastapi.security import APIKeyHeader

from src.config import settings

_apikey_header = APIKeyHeader(name="apikey", auto_error=False)


async def verify_api_key(
    request: Request,
    apikey_header: str | None = Security(_apikey_header),
    apikey_query: str | None = Query(None, alias="apikey"),
) -> str:
    # Try Bearer token from Authorization header
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
        if token == settings.api_key:
            return token

    # Try apikey header
    if apikey_header and apikey_header == settings.api_key:
        return apikey_header

    # Try query param
    if apikey_query and apikey_query == settings.api_key:
        return apikey_query

    raise HTTPException(
        status_code=401,
        detail={
            "success": False,
            "error": {
                "code": "UNAUTHORIZED",
                "message": "Invalid or missing API key",
                "details": {},
            },
        },
    )
