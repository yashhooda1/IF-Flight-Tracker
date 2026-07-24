"""Destination enrichment: airport lookup, weather, weather map tiles, city photo.

All external calls (OpenWeatherMap, Wikipedia) go through here so keys stay
server-side and responses are cached. In mock mode everything returns synthetic
data except airport lookups, which use the offline `airportsdata` catalogue and
are therefore always real.
"""

from __future__ import annotations

import base64
import math
import time
from datetime import datetime, timezone
from typing import Any

import airportsdata
import httpx
from fastapi import APIRouter, HTTPException, Query, Response

from .config import settings

router = APIRouter()

_AIRPORTS = airportsdata.load("ICAO")  # ICAO -> {name, city, country, lat, lon, elevation, tz}

# 1x1 transparent PNG, used for weather tiles in mock mode (invisible overlay).
_BLANK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

_OWM_LAYERS = {
    "clouds": "clouds_new",
    "precipitation": "precipitation_new",
    "wind": "wind_new",
    "temp": "temp_new",
}

_http: httpx.AsyncClient | None = None
_cache: dict[str, tuple[float, Any]] = {}


async def _client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(
            timeout=httpx.Timeout(12.0),
            follow_redirects=True,
            headers={"User-Agent": "if-tracker/1.0 (personal flight tracker)"},
        )
    return _http


async def aclose() -> None:
    global _http
    if _http is not None:
        await _http.aclose()
        _http = None


def _cached(key: str, ttl: int) -> Any | None:
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    return None


def _store(key: str, value: Any) -> None:
    _cache[key] = (time.monotonic(), value)


# --------------------------------------------------------------------- airport
def airport_record(icao: str) -> dict | None:
    a = _AIRPORTS.get(icao.upper())
    if not a:
        return None
    return {
        "icao": a["icao"],
        "iata": a.get("iata") or None,
        "name": a.get("name"),
        "city": a.get("city") or None,
        "country": a.get("country"),
        "latitude": a.get("lat"),
        "longitude": a.get("lon"),
        "elevation": a.get("elevation"),  # feet
        "tz": a.get("tz"),
    }


@router.get("/api/airport/{icao}")
async def airport(icao: str):
    rec = airport_record(icao)
    if not rec:
        raise HTTPException(status_code=404, detail=f"Airport {icao.upper()} not found.")
    return rec


# --------------------------------------------------------------------- weather
def _mock_weather(lat: float, lon: float) -> dict:
    # Deterministic-ish so the same airport looks stable between refreshes.
    seed = int(abs(lat) + abs(lon))
    temp = 15 + (seed % 18) - 4
    conditions = ["Clear", "Few clouds", "Scattered clouds", "Overcast", "Light rain"]
    cond = conditions[seed % len(conditions)]
    now = datetime.now(timezone.utc)
    forecast = []
    for d in range(5):
        forecast.append({
            "date": now.strftime("%Y-%m-%d"),
            "dt": int(now.timestamp()) + d * 86400,
            "tempMin": temp - 5 - (d % 3),
            "tempMax": temp + 4 + (d % 4),
            "icon": ["01d", "02d", "03d", "04d", "10d"][(seed + d) % 5],
            "description": conditions[(seed + d) % len(conditions)],
        })
    return {
        "provider": "mock",
        "current": {
            "temp": temp, "feelsLike": temp - 1, "description": cond,
            "icon": ["01d", "02d", "03d", "04d", "10d"][seed % 5],
            "humidity": 40 + seed % 50, "cloudCover": (seed * 7) % 100,
            "windSpeedKts": round((seed % 25) * 1.0, 1), "windDir": (seed * 13) % 360,
            "visibilityKm": 10, "pressure": 1013,
        },
        "forecast": forecast,
    }


def _condense_forecast(items: list[dict]) -> list[dict]:
    """Turn OWM 3-hourly list into up to 5 daily min/max summaries."""
    by_day: dict[str, dict] = {}
    for it in items:
        day = it["dt_txt"][:10]
        t = it["main"]["temp"]
        w = it["weather"][0]
        d = by_day.setdefault(day, {
            "date": day, "dt": it["dt"], "tempMin": t, "tempMax": t,
            "icon": w["icon"], "description": w["description"], "_noon": 99,
        })
        d["tempMin"] = min(d["tempMin"], t)
        d["tempMax"] = max(d["tempMax"], t)
        # Prefer the icon nearest local noon for a representative daytime symbol.
        hour = int(it["dt_txt"][11:13])
        if abs(hour - 12) < d["_noon"]:
            d["_noon"] = abs(hour - 12)
            d["icon"] = w["icon"]
            d["description"] = w["description"]
    out = []
    for day in sorted(by_day)[:5]:
        d = by_day[day]
        d.pop("_noon", None)
        d["tempMin"] = round(d["tempMin"])
        d["tempMax"] = round(d["tempMax"])
        out.append(d)
    return out


@router.get("/api/wx")
async def weather(lat: float = Query(...), lon: float = Query(...)):
    key = f"wx:{round(lat,2)}:{round(lon,2)}"
    cached = _cached(key, settings.ttl_wx)
    if cached is not None:
        return cached

    if settings.mock or not settings.owm_api_key:
        data = _mock_weather(lat, lon)
        if not settings.mock and not settings.owm_api_key:
            data["provider"] = "unavailable"  # signals UI to show a hint
        _store(key, data)
        return data

    client = await _client()
    params = {"lat": lat, "lon": lon, "units": "metric", "appid": settings.owm_api_key}
    try:
        cur_r, fc_r = None, None
        cur_r = await client.get("https://api.openweathermap.org/data/2.5/weather", params=params)
        fc_r = await client.get("https://api.openweathermap.org/data/2.5/forecast", params=params)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=504, detail=f"Weather service unreachable: {exc}")

    if cur_r.status_code == 401:
        raise HTTPException(status_code=401, detail="OpenWeatherMap key rejected.")
    if cur_r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Weather error HTTP {cur_r.status_code}.")

    c = cur_r.json()
    wind_ms = (c.get("wind") or {}).get("speed", 0) or 0
    data = {
        "provider": "openweathermap",
        "current": {
            "temp": round(c["main"]["temp"]),
            "feelsLike": round(c["main"]["feels_like"]),
            "description": c["weather"][0]["description"].title(),
            "icon": c["weather"][0]["icon"],
            "humidity": c["main"]["humidity"],
            "cloudCover": (c.get("clouds") or {}).get("all"),
            "windSpeedKts": round(wind_ms * 1.94384, 1),
            "windDir": (c.get("wind") or {}).get("deg"),
            "visibilityKm": round((c.get("visibility") or 0) / 1000, 1),
            "pressure": c["main"].get("pressure"),
        },
        "forecast": _condense_forecast((fc_r.json() or {}).get("list", [])) if fc_r.status_code < 400 else [],
    }
    _store(key, data)
    return data


@router.get("/api/wxtile/{layer}/{z}/{x}/{y}.png")
async def weather_tile(layer: str, z: int, x: int, y: int):
    owm_layer = _OWM_LAYERS.get(layer)
    if not owm_layer:
        raise HTTPException(status_code=404, detail="Unknown weather layer.")

    if settings.mock or not settings.owm_api_key:
        return Response(content=_BLANK_PNG, media_type="image/png")

    key = f"tile:{layer}:{z}:{x}:{y}"
    cached = _cached(key, settings.ttl_tile)
    if cached is not None:
        return Response(content=cached, media_type="image/png")

    client = await _client()
    url = f"https://tile.openweathermap.org/map/{owm_layer}/{z}/{x}/{y}.png"
    try:
        r = await client.get(url, params={"appid": settings.owm_api_key})
    except httpx.RequestError:
        return Response(content=_BLANK_PNG, media_type="image/png")
    if r.status_code >= 400:
        return Response(content=_BLANK_PNG, media_type="image/png")
    _store(key, r.content)
    return Response(content=r.content, media_type="image/png")


# ----------------------------------------------------------------------- photo
@router.get("/api/photo")
async def photo(title: str = Query(..., min_length=1)):
    key = f"photo:{title.lower()}"
    cached = _cached(key, settings.ttl_photo)
    if cached is not None:
        return cached

    if settings.mock:
        data = {"title": title, "thumbnail": None, "extract": None, "url": None,
                "note": "Photos are disabled in mock mode."}
        _store(key, data)
        return data

    client = await _client()
    # Wikipedia's REST summary endpoint takes an underscored page title.
    url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + title.replace(" ", "_")
    try:
        r = await client.get(url)
    except httpx.RequestError:
        data = {"title": title, "thumbnail": None, "extract": None, "url": None}
        _store(key, data)
        return data

    if r.status_code >= 400:
        data = {"title": title, "thumbnail": None, "extract": None, "url": None}
        _store(key, data)
        return data

    j = r.json()
    data = {
        "title": j.get("title", title),
        "thumbnail": (j.get("thumbnail") or {}).get("source"),
        "originalImage": (j.get("originalimage") or {}).get("source"),
        "extract": j.get("extract"),
        "url": (j.get("content_urls", {}).get("desktop", {}) or {}).get("page"),
    }
    _store(key, data)
    return data
