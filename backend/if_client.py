"""Thin async client for the Infinite Flight Live API v2.

Responsibilities:
  * hold the API key server-side (never shipped to the browser)
  * translate the {errorCode, result} envelope into data or an exception
  * cache responses with a per-endpoint TTL
  * collapse concurrent requests for the same path into one upstream call
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .config import settings

ERROR_CODES = {
    0: "Ok",
    1: "UserNotFound",
    2: "MissingRequestParameters",
    3: "EndpointError",
    4: "NotAuthorized",
    5: "ServerNotFound",
    6: "FlightNotFound",
    7: "NoAtisAvailable",
}


class IFError(Exception):
    def __init__(self, message: str, status: int = 502, code: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class IFClient:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=settings.base_url,
                timeout=httpx.Timeout(15.0),
                headers={
                    "Authorization": f"Bearer {settings.api_key}",
                    "Accept": "application/json",
                    "User-Agent": "if-tracker/1.0",
                },
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ cache
    def _cached(self, key: str, ttl: int) -> Any | None:
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        return None

    def _store(self, key: str, value: Any) -> None:
        self._cache[key] = (time.monotonic(), value)

    def cache_stats(self) -> dict[str, int]:
        return {"entries": len(self._cache)}

    # ----------------------------------------------------------------- request
    async def get(self, path: str, ttl: int = 0, params: dict | None = None) -> Any:
        return await self._request("GET", path, ttl=ttl, params=params)

    async def post(self, path: str, json: dict, ttl: int = 0) -> Any:
        return await self._request("POST", path, ttl=ttl, json=json)

    async def _request(
        self,
        method: str,
        path: str,
        ttl: int = 0,
        params: dict | None = None,
        json: dict | None = None,
    ) -> Any:
        key = f"{method}:{path}:{sorted((params or {}).items())}:{json}"

        if ttl:
            cached = self._cached(key, ttl)
            if cached is not None:
                return cached

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Another coroutine may have filled the cache while we waited.
            if ttl:
                cached = self._cached(key, ttl)
                if cached is not None:
                    return cached

            if settings.mock:
                from . import mock_data

                result = mock_data.respond(method, path, params, json)
            else:
                result = await self._call_upstream(method, path, params, json)

            if ttl:
                self._store(key, result)
            return result

    async def _call_upstream(
        self, method: str, path: str, params: dict | None, json: dict | None
    ) -> Any:
        if not settings.api_key:
            raise IFError(
                "No Infinite Flight API key configured. Set IF_API_KEY in .env, "
                "or set IF_MOCK=true to explore the UI with sample data.",
                status=503,
            )
        try:
            await self.start()
        except Exception as exc:  # e.g. proxy env misconfiguration
            raise IFError(f"Could not create the HTTP client: {exc}", status=500) from exc
        assert self._client is not None
        try:
            resp = await self._client.request(method, path, params=params, json=json)
        except httpx.RequestError as exc:
            raise IFError(f"Could not reach the Live API: {exc}", status=504) from exc

        if resp.status_code == 401:
            raise IFError("API key rejected (401).", status=401)
        if resp.status_code == 429:
            raise IFError("Rate limited by the Live API (429). Back off.", status=429)
        if resp.status_code >= 400:
            raise IFError(f"Live API returned HTTP {resp.status_code}.", status=502)

        try:
            payload = resp.json()
        except ValueError as exc:
            raise IFError("Live API returned a non-JSON body.", status=502) from exc

        # Every v2 endpoint wraps its payload in {errorCode, result}.
        if isinstance(payload, dict) and "errorCode" in payload:
            code = payload.get("errorCode", 0)
            if code != 0:
                name = ERROR_CODES.get(code, f"Unknown({code})")
                status = 404 if code in (1, 5, 6, 7) else 403 if code == 4 else 502
                raise IFError(f"Live API error: {name}", status=status, code=code)
            return payload.get("result")
        return payload


client = IFClient()
