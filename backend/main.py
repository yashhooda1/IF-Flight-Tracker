"""FastAPI backend for the Infinite Flight tracker.

The browser never sees the API key: it talks only to these /api/* routes, which
proxy (and cache, and enrich) the Live API v2.
"""

from __future__ import annotations

import math
import mimetypes
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Some minimal container images ship without /etc/mime.types, which can make
# StaticFiles serve .css/.js with the wrong content type. Register them here so
# the browser always applies the stylesheet and runs the script.
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("image/svg+xml", ".svg")

from . import extras
from .config import settings
from .if_client import IFError, client

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The HTTP client is created lazily on first upstream call, so a missing key
    # or an odd proxy environment can never take the whole app down at boot.
    yield
    await client.close()
    await extras.aclose()


app = FastAPI(title="Infinite Flight Tracker", version="1.0.0", lifespan=lifespan)
app.include_router(extras.router)


@app.exception_handler(IFError)
async def if_error_handler(request: Request, exc: IFError):
    return JSONResponse(status_code=exc.status, content={"detail": exc.message})


# --------------------------------------------------------------- enrichment
async def livery_index() -> dict[str, dict]:
    """liveryId -> {aircraftName, liveryName}. Cached for an hour."""
    try:
        liveries = await client.get("/aircraft/liveries", ttl=settings.ttl_static)
    except IFError:
        return {}
    return {
        l["id"]: {"aircraftName": l.get("aircraftName"), "liveryName": l.get("liveryName")}
        for l in liveries or []
        if isinstance(l, dict) and l.get("id")
    }


# ------------------------------------------------------------------- routes
@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "mode": "mock" if settings.mock else "live",
        "keyConfigured": bool(settings.api_key),
        "cache": client.cache_stats(),
    }


@app.get("/api/sessions")
async def sessions():
    return await client.get("/sessions", ttl=settings.ttl_sessions)


@app.get("/api/sessions/{session_id}/flights")
async def flights(session_id: str, q: str | None = Query(None, description="callsign / username filter")):
    raw = await client.get(f"/sessions/{session_id}/flights", ttl=settings.ttl_flights) or []
    liveries = await livery_index()

    out = []
    for f in raw:
        info = liveries.get(f.get("liveryId"), {})
        out.append({
            **f,
            "aircraftName": info.get("aircraftName"),
            "liveryName": info.get("liveryName"),
        })

    if q:
        needle = q.strip().lower()
        out = [
            f for f in out
            if needle in (f.get("callsign") or "").lower()
            or needle in (f.get("username") or "").lower()
            or needle in (f.get("virtualOrganization") or "").lower()
        ]

    out.sort(key=lambda f: f.get("altitude") or 0, reverse=True)
    return {"count": len(out), "flights": out}


@app.get("/api/sessions/{session_id}/flights/{flight_id}/route")
async def flight_route(session_id: str, flight_id: str):
    return await client.get(
        f"/sessions/{session_id}/flights/{flight_id}/route", ttl=settings.ttl_route
    )


@app.get("/api/sessions/{session_id}/flights/{flight_id}/flightplan")
async def flight_plan(session_id: str, flight_id: str):
    return await client.get(
        f"/sessions/{session_id}/flights/{flight_id}/flightplan", ttl=settings.ttl_route
    )


def _haversine_nm(lat1, lon1, lat2, lon2) -> float:
    r = 3440.065  # nautical miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(min(1.0, math.sqrt(a)))


def _endpoints_from_plan(fp: dict) -> tuple[str | None, str | None]:
    """Best-effort origin/destination ICAO from a flight plan.

    Picks the first and last waypoint names that are 4-letter codes present in
    the airport catalogue — that skips SIDs, STARs and fixes."""
    waypoints = (fp or {}).get("waypoints") or []
    icaos = [w for w in waypoints if isinstance(w, str) and len(w) == 4
             and w.isalpha() and extras.airport_record(w)]
    if not icaos:
        return None, None
    return icaos[0], icaos[-1]


@app.get("/api/sessions/{session_id}/flights/{flight_id}/destination")
async def flight_destination(session_id: str, flight_id: str):
    """Ties the filed plan to the airport catalogue and computes a live ETA."""
    fp = await client.get(
        f"/sessions/{session_id}/flights/{flight_id}/flightplan", ttl=settings.ttl_route
    )
    origin_icao, dest_icao = _endpoints_from_plan(fp or {})
    if not dest_icao:
        return {"hasDestination": False, "originIcao": origin_icao}

    dest = extras.airport_record(dest_icao)
    result: dict = {
        "hasDestination": True,
        "originIcao": origin_icao,
        "origin": extras.airport_record(origin_icao) if origin_icao else None,
        "destinationIcao": dest_icao,
        "destination": dest,
        "distanceNm": None,
        "etaMinutes": None,
        "etaUtc": None,
    }

    # Current position + speed come from the (cached) flights list.
    flights = await client.get(f"/sessions/{session_id}/flights", ttl=settings.ttl_flights) or []
    me = next((f for f in flights if f.get("flightId") == flight_id), None)
    if me and dest and dest["latitude"] is not None:
        dist = _haversine_nm(me["latitude"], me["longitude"], dest["latitude"], dest["longitude"])
        result["distanceNm"] = round(dist)
        gs = me.get("speed") or 0
        if gs > 40:  # airborne enough for an ETA to mean anything
            minutes = dist / gs * 60
            result["etaMinutes"] = round(minutes)
            result["etaUtc"] = (
                datetime.now(timezone.utc) + timedelta(minutes=minutes)
            ).strftime("%Y-%m-%d %H:%M:%SZ")
    return result


ATC_TYPES = {
    0: "Ground", 1: "Tower", 2: "Unicom", 3: "Clearance", 4: "Approach",
    5: "Departure", 6: "Center", 7: "ATIS", 8: "Aircraft", 9: "Recorded",
    10: "Unknown", 11: "Unused",
}


@app.get("/api/sessions/{session_id}/atc")
async def atc(session_id: str):
    raw = await client.get(f"/sessions/{session_id}/atc", ttl=settings.ttl_atc) or []
    facilities = [{**f, "typeName": ATC_TYPES.get(f.get("type"), "Unknown")} for f in raw]

    airports: dict[str, dict] = {}
    for f in facilities:
        icao = f.get("airportName") or "CENTER"
        entry = airports.setdefault(
            icao,
            {"icao": icao, "latitude": f.get("latitude"), "longitude": f.get("longitude"),
             "frequencies": []},
        )
        entry["frequencies"].append(f)

    return {
        "count": len(facilities),
        "facilities": facilities,
        "airports": sorted(airports.values(), key=lambda a: a["icao"]),
    }


@app.get("/api/sessions/{session_id}/world")
async def world(session_id: str):
    """Airport traffic. The upstream payload has no coordinates, so we borrow
    them from any ATC facility open at that airport (the rest are list-only)."""
    raw = await client.get(f"/sessions/{session_id}/world", ttl=settings.ttl_world) or []
    out = []
    for a in raw:
        if not isinstance(a, dict):
            continue
        facilities = a.get("atcFacilities") or []
        lat = a.get("latitude") or (facilities[0].get("latitude") if facilities else None)
        lon = a.get("longitude") or (facilities[0].get("longitude") if facilities else None)
        inbound = a.get("inboundFlightsCount") or 0
        outbound = a.get("outboundFlightsCount") or 0
        out.append({
            "airportIcao": a.get("airportIcao"),
            "airportName": a.get("airportName"),
            "inboundFlightsCount": inbound,
            "outboundFlightsCount": outbound,
            "traffic": inbound + outbound,
            "latitude": lat,
            "longitude": lon,
            "atcFacilities": [
                {**f, "typeName": ATC_TYPES.get(f.get("type"), "Unknown")} for f in facilities
            ],
        })
    out.sort(key=lambda a: a["traffic"], reverse=True)
    return out


@app.get("/api/sessions/{session_id}/atis/{icao}")
async def atis(session_id: str, icao: str):
    return {"icao": icao.upper(),
            "atis": await client.get(f"/sessions/{session_id}/atis/{icao.upper()}", ttl=settings.ttl_atc)}


@app.get("/api/sessions/{session_id}/airport/{icao}/status")
async def airport_status(session_id: str, icao: str):
    return await client.get(
        f"/sessions/{session_id}/airport/{icao.upper()}/status", ttl=settings.ttl_world
    )


class UserLookup(BaseModel):
    discourseNames: list[str] = []
    userIds: list[str] = []
    userHashes: list[str] = []


GRADES = {1: "Grade 1", 2: "Grade 2", 3: "Grade 3", 4: "Grade 4", 5: "Grade 5"}
ATC_RANKS = {0: "Observer", 1: "ATC Trainee", 2: "ATC Apprentice", 3: "ATC Specialist",
             4: "ATC Officer", 5: "ATC Supervisor", 6: "ATC Recruiter", 7: "ATC Manager"}


@app.post("/api/users/stats")
async def user_stats(body: UserLookup):
    payload = {k: v for k, v in body.model_dump().items() if v}
    if not payload:
        return {"users": []}
    raw = await client.post("/users", json=payload, ttl=60) or []
    users = []
    for u in raw:
        ft = u.get("flightTime") or 0  # minutes
        users.append({
            **u,
            "gradeName": GRADES.get(u.get("grade"), f"Grade {u.get('grade')}"),
            "atcRankName": ATC_RANKS.get(u.get("atcRank")) if u.get("atcRank") is not None else None,
            "flightTimeHours": round(ft / 60, 1),
        })
    return {"users": users}


@app.get("/api/users/{user_id}/flights")
async def user_flights(user_id: str, page: int = 1):
    raw = await client.get(f"/users/{user_id}/flights", ttl=60, params={"page": page})
    liveries = await livery_index()
    if isinstance(raw, dict) and "data" in raw:
        for row in raw["data"]:
            info = liveries.get(row.get("liveryId"), {})
            row["aircraftName"] = info.get("aircraftName")
            row["liveryName"] = info.get("liveryName")
    return raw


@app.get("/api/aircraft")
async def aircraft():
    return await client.get("/aircraft", ttl=settings.ttl_static)


# The browser and some platform health probes request /favicon.ico; answer with
# the same plane icon the page uses instead of logging a 404.
_FAVICON = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'>"
    "<path fill='#4ea1ff' d='M12 2c.6 0 1 .9 1 2v5.2l8 4.4v2l-8-2.4v4.5l2.6 "
    "1.8v1.6L12 20l-3.6.9v-1.6L11 17.7v-4.5L3 15.6v-2l8-4.4V4c0-1.1.4-2 1-2z'/></svg>"
)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(content=_FAVICON, media_type="image/svg+xml")


# ------------------------------------------------------------------ frontend
if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

    # Allow HEAD as well as GET so platform port probes (which use HEAD /) get a
    # 200 rather than a noisy 405.
    @app.get("/")
    @app.head("/")
    async def index():
        return FileResponse(FRONTEND / "index.html")
