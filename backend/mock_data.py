"""Synthetic Live API responses so the app is explorable without a key.

Shapes match the real v2 documentation exactly, so swapping IF_MOCK off is the
only change needed once a real key arrives.
"""

from __future__ import annotations

import math
import random
import re
import time
from datetime import datetime, timedelta, timezone

SESSIONS = [
    {"id": "11111111-1111-1111-1111-111111111111", "name": "Expert Server",
     "maxUsers": 2000, "userCount": 742, "type": 1},
    {"id": "22222222-2222-2222-2222-222222222222", "name": "Training Server",
     "maxUsers": 2000, "userCount": 431, "type": 0},
    {"id": "33333333-3333-3333-3333-333333333333", "name": "Casual Server",
     "maxUsers": 2000, "userCount": 188, "type": 0},
]

AIRPORTS = {
    "KLAX": (33.9425, -118.4081), "KJFK": (40.6413, -73.7781),
    "EGLL": (51.4700, -0.4543), "EHAM": (52.3105, 4.7683),
    "OMDB": (25.2532, 55.3657), "WSSS": (1.3644, 103.9915),
    "RJTT": (35.5494, 139.7798), "YSSY": (-33.9399, 151.1753),
    "KSFO": (37.6213, -122.3790), "LFPG": (49.0097, 2.5479),
    "KATL": (33.6407, -84.4277), "VHHH": (22.3080, 113.9185),
    "KORD": (41.9742, -87.9073), "LEMD": (40.4839, -3.5680),
    "KSEA": (47.4502, -122.3088), "CYYZ": (43.6777, -79.6248),
}

AIRCRAFT = [
    {"id": "a1", "name": "Boeing 777-300ER"}, {"id": "a2", "name": "Airbus A350-900"},
    {"id": "a3", "name": "Boeing 787-9"}, {"id": "a4", "name": "Airbus A321"},
    {"id": "a5", "name": "Boeing 737-800"}, {"id": "a6", "name": "Cessna 172"},
    {"id": "a7", "name": "Airbus A380"}, {"id": "a8", "name": "Boeing 747-8"},
]

LIVERIES = [
    {"id": f"l{i}", "aircraftID": ac["id"], "aircraftName": ac["name"], "liveryName": lv}
    for i, (ac, lv) in enumerate(
        [(a, l) for a in AIRCRAFT for l in ("Generic", "Emirates", "British Airways", "Delta")]
    )
]

AIRLINES = ["BAW", "UAL", "DAL", "AAL", "UAE", "SIA", "QFA", "AFR", "KLM", "ANA", "N", "ACA"]
USERS = ["Cameron", "Yash", "SkyHigh", "Tim747", "IFATC_Mike", "Aviator22", "Jetstream",
         "MaxThrust", "CaptainKay", "FlightLevel410", "Nova", "Zulu"]
VOS = ["IFATC [IFATC]", "Infinite Flight AT [IFAT]", None, "Delta Virtual [DLVA]"]


def _rng(session_id: str) -> random.Random:
    return random.Random(session_id)


def _flights(session_id: str) -> list[dict]:
    """Deterministic fleet whose positions advance smoothly with wall-clock time."""
    rng = _rng(session_id)
    n = {"1": 60, "2": 40, "3": 20}.get(session_id[0], 30)
    t = time.time()
    codes = list(AIRPORTS)
    out = []
    for i in range(n):
        origin, dest = rng.sample(codes, 2)
        o, d = AIRPORTS[origin], AIRPORTS[dest]
        # Progress along the great-circle-ish straight line, looping every ~2h.
        phase = ((t / 7200.0) + rng.random()) % 1.0
        lat = o[0] + (d[0] - o[0]) * phase
        lon = o[1] + (d[1] - o[1]) * phase
        track = (math.degrees(math.atan2(d[1] - o[1], d[0] - o[0]))) % 360
        climbing = phase < 0.15
        descending = phase > 0.85
        alt = 1500 if climbing else (4000 if descending else 34000 + rng.randint(-4000, 4000))
        ac = rng.choice(AIRCRAFT)
        livery = rng.choice([l for l in LIVERIES if l["aircraftID"] == ac["id"]])
        out.append({
            "flightId": f"{session_id[:8]}-flight-{i:03d}",
            "userId": f"user-{i % len(USERS):03d}",
            "aircraftId": ac["id"],
            "liveryId": livery["id"],
            "username": USERS[i % len(USERS)] if i % 7 else None,
            "virtualOrganization": rng.choice(VOS),
            "callsign": f"{rng.choice(AIRLINES)}{rng.randint(1, 999)}",
            "latitude": lat,
            "longitude": lon,
            "altitude": float(alt),
            "speed": 180.0 if climbing or descending else 460.0 + rng.randint(-40, 40),
            "verticalSpeed": 2400.0 if climbing else (-1800.0 if descending else 0.0),
            "track": track,
            "heading": track,
            "lastReport": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
            "_origin": origin,
            "_destination": dest,
        })
    return out


def _atc(session_id: str) -> list[dict]:
    rng = _rng(session_id + "atc")
    facilities = []
    for icao in rng.sample(list(AIRPORTS), 7):
        lat, lon = AIRPORTS[icao]
        for ftype in rng.sample([0, 1, 3, 4, 7], rng.randint(1, 3)):
            facilities.append({
                "frequencyId": f"{icao}-{ftype}",
                "userId": f"user-{rng.randint(0, 11):03d}",
                "username": rng.choice(USERS),
                "virtualOrganization": None,
                "airportName": icao,
                "type": ftype,
                "latitude": lat,
                "longitude": lon,
                "startTime": (datetime.now(timezone.utc) - timedelta(minutes=rng.randint(5, 90)))
                .strftime("%Y-%m-%d %H:%M:%SZ"),
            })
    return facilities


def _world(session_id: str) -> list[dict]:
    flights = _flights(session_id)
    atc_icaos = {f["airportName"] for f in _atc(session_id)}
    out = []
    for icao, (lat, lon) in AIRPORTS.items():
        inbound = [f for f in flights if f["_destination"] == icao]
        outbound = [f for f in flights if f["_origin"] == icao]
        if not inbound and not outbound and icao not in atc_icaos:
            continue
        out.append({
            "airportIcao": icao,
            "airportName": icao,
            "inboundFlightsCount": len(inbound),
            "outboundFlightsCount": len(outbound),
            "atcFacilities": [f for f in _atc(session_id) if f["airportName"] == icao],
        })
    return out


def _route(session_id: str, flight_id: str) -> list[dict]:
    flights = {f["flightId"]: f for f in _flights(session_id)}
    f = flights.get(flight_id)
    if not f:
        return []
    o = AIRPORTS[f["_origin"]]
    now = datetime.now(timezone.utc)
    pts = []
    steps = 40
    for i in range(steps):
        frac = i / steps
        pts.append({
            "latitude": o[0] + (f["latitude"] - o[0]) * frac,
            "longitude": o[1] + (f["longitude"] - o[1]) * frac,
            "altitude": f["altitude"] * min(1.0, frac * 4),
            "track": f["track"],
            "groundSpeed": f["speed"],
            "date": (now - timedelta(minutes=(steps - i) * 3)).strftime("%Y-%m-%d %H:%M:%SZ"),
        })
    return pts


def _flightplan(session_id: str, flight_id: str) -> dict:
    flights = {f["flightId"]: f for f in _flights(session_id)}
    f = flights.get(flight_id)
    if not f:
        return {"flightPlanItems": []}
    o, d = AIRPORTS[f["_origin"]], AIRPORTS[f["_destination"]]
    items = []
    for name, (lat, lon) in [(f["_origin"], o), ("WPT01", ((o[0] + d[0]) / 2, (o[1] + d[1]) / 2)),
                             (f["_destination"], d)]:
        items.append({
            "name": name, "type": 0, "children": None,
            "identifier": name,
            "altitude": 0 if name in (f["_origin"], f["_destination"]) else 34000,
            "location": {"latitude": lat, "longitude": lon, "altitude": 0},
        })
    return {
        "flightPlanId": f"fp-{flight_id}",
        "flightId": flight_id,
        "waypoints": [f["_origin"], "WPT01", f["_destination"]],
        "lastUpdate": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        "flightPlanItems": items,
        "flightPlanType": 0,
    }


def _user_stats(names: list[str]) -> list[dict]:
    out = []
    for name in names or ["Cameron"]:
        rng = random.Random(name.lower())
        out.append({
            "userId": f"user-{abs(hash(name.lower())) % 12:03d}",
            "virtualOrganization": rng.choice(VOS),
            "discourseUsername": name,
            "groups": ["IFATC"] if rng.random() > 0.6 else [],
            "roles": [],
            "errorCode": 0,
            "onlineFlights": rng.randint(50, 2400),
            "violations": rng.randint(0, 25),
            "xp": rng.randint(20000, 900000),
            "landingCount": rng.randint(40, 3000),
            "flightTime": rng.randint(600, 120000),
            "atcOperations": rng.randint(0, 4000),
            "atcRank": rng.randint(0, 6),
            "grade": rng.randint(1, 5),
            "hash": "ABC123",
        })
    return out


def _user_flights(user_id: str, page: int) -> dict:
    rng = random.Random(user_id + str(page))
    codes = list(AIRPORTS)
    data = []
    for i in range(10):
        o, d = rng.sample(codes, 2)
        created = datetime.now(timezone.utc) - timedelta(days=(page - 1) * 10 + i, hours=rng.randint(0, 12))
        data.append({
            "id": f"{user_id}-hist-{page}-{i}",
            "created": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "userId": user_id,
            "aircraftId": rng.choice(AIRCRAFT)["id"],
            "liveryId": rng.choice(LIVERIES)["id"],
            "callsign": f"{rng.choice(AIRLINES)}{rng.randint(1, 999)}",
            "server": rng.choice([s["name"] for s in SESSIONS]),
            "dayTime": round(rng.uniform(0.5, 9.0), 2),
            "nightTime": round(rng.uniform(0.0, 4.0), 2),
            "totalTime": 0.0,
            "landingCount": rng.randint(1, 3),
            "originAirport": o,
            "destinationAirport": d,
            "xp": rng.randint(200, 4000),
            "worldType": rng.randint(0, 3),
            "violations": [],
        })
        data[-1]["totalTime"] = round(data[-1]["dayTime"] + data[-1]["nightTime"], 2)
    return {"pageIndex": page, "totalPages": 5, "totalCount": 50,
            "hasPreviousPage": page > 1, "hasNextPage": page < 5, "data": data}


def respond(method: str, path: str, params: dict | None, json: dict | None):
    path = path.rstrip("/")

    if path == "/sessions":
        return SESSIONS
    if path == "/aircraft":
        return AIRCRAFT
    if path == "/aircraft/liveries":
        return LIVERIES

    m = re.fullmatch(r"/sessions/([^/]+)/flights", path)
    if m:
        return _flights(m.group(1))

    m = re.fullmatch(r"/sessions/([^/]+)/flights/([^/]+)/route", path)
    if m:
        return _route(m.group(1), m.group(2))

    m = re.fullmatch(r"/sessions/([^/]+)/flights/([^/]+)/flightplan", path)
    if m:
        return _flightplan(m.group(1), m.group(2))

    m = re.fullmatch(r"/sessions/([^/]+)/atc", path)
    if m:
        return _atc(m.group(1))

    m = re.fullmatch(r"/sessions/([^/]+)/world", path)
    if m:
        return _world(m.group(1))

    m = re.fullmatch(r"/sessions/([^/]+)/atis/([^/]+)", path)
    if m:
        icao = m.group(2).upper()
        return (f"{icao} INFORMATION ALPHA. WIND 270 AT 8 KNOTS. VISIBILITY 10 KILOMETRES. "
                f"CLEAR SKIES. TEMPERATURE 21, DEW POINT 12. LANDING AND DEPARTING RUNWAY 27. "
                f"ADVISE ON INITIAL CONTACT YOU HAVE INFORMATION ALPHA.")

    m = re.fullmatch(r"/sessions/([^/]+)/airport/([^/]+)/status", path)
    if m:
        sid, icao = m.group(1), m.group(2).upper()
        for a in _world(sid):
            if a["airportIcao"] == icao:
                return a
        return {"airportIcao": icao, "airportName": icao, "inboundFlightsCount": 0,
                "outboundFlightsCount": 0, "atcFacilities": []}

    if path == "/users" and method == "POST":
        return _user_stats((json or {}).get("discourseNames") or [])

    m = re.fullmatch(r"/users/([^/]+)/flights", path)
    if m:
        return _user_flights(m.group(1), int((params or {}).get("page", 1)))

    return []
