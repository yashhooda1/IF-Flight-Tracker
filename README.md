# Infinite Flight Tracker

Live map, ATC board, pilot lookup and logbook for the Infinite Flight Live API v2.

FastAPI backend (holds the API key, caches, enriches) + vanilla JS/Leaflet frontend.
No build step, no npm.

---

## 1. Run it right now, no key needed

```bash
pip install -r requirements.txt
cp .env.example .env        # leave IF_API_KEY blank, set IF_MOCK=true
python run.py
```

Open <http://127.0.0.1:8000>. `IF_MOCK=true` serves synthetic data in the exact
shape the real API returns, so the whole UI is explorable while you wait for a key.

## 2. Getting a real API key

The Live API is key-gated and keys are handed out manually:

1. Email **hello@infiniteflight.com** from the address on your IF account.
2. Say who you are and what you're building — a personal, non-commercial flight
   tracker web app, roughly one poll every 15 seconds per server. Being specific
   about your polling rate helps; they care about load.
3. When the key arrives, put it in `.env`:

```
IF_API_KEY=your-key-here
IF_MOCK=false
```

Restart, and the same UI is now live data. Nothing else changes — that's the point
of the mock layer.

## 2b. Weather key (optional but recommended)

Destination weather and the map weather overlays use OpenWeatherMap. Grab a free
key at <https://home.openweathermap.org/users/sign_up> and add it to `.env`:

```
IF_OWM_API_KEY=your-owm-key-here
```

Leave it blank and everything else still works — the weather panel just shows a
"add a key" hint and the map weather layers stay empty. New keys can take an hour
or two to activate on OpenWeatherMap's side.

## 3. What it does

**Flights tab** — every aircraft on the selected server, plotted with heading-rotated
icons coloured by phase (grey ground, green climb, blue cruise, amber descent).
Filter by callsign, username or virtual airline. Click any aircraft for altitude,
ground speed, vertical speed, track, aircraft type and livery, plus its flown track
(solid blue) and filed flight plan (dashed amber).

**Destination panel** (when you click a flight) — arrival airport (ICAO/IATA, name,
city, field elevation, local time), a live **ETA** computed from the aircraft's
position and ground speed, current **weather** at the destination with a 5-day
forecast strip, and a **photo** of the arrival city. Airport data comes from an
offline catalogue (`airportsdata`), so it's instant and needs no network.

**Map layers** (bottom-left control) — switch the base map between the dark vector
style and **satellite** imagery, and toggle **cloud**, **precipitation**, **wind**
and **temperature** overlays. A **day/night** terminator shades the half of the
globe currently in darkness and updates every minute.

**ATC tab** — open frequencies grouped by airport, who's controlling, and the busiest
airports by inbound/outbound count. Click through for airport status and live ATIS.

**Pilot tab** — look up any pilot by community forum username: grade, XP, flight hours,
landings, violations, ATC rank, whether they're airborne right now (one click to track
them), and a paginated logbook of past flights.

## 4. Architecture

```
browser  ──►  FastAPI /api/*  ──►  api.infiniteflight.com/public/v2
             (key + cache)
```

- **`backend/if_client.py`** — async httpx client. Unwraps the `{errorCode, result}`
  envelope, maps error codes to HTTP statuses, caches per endpoint with its own TTL,
  and collapses concurrent identical requests behind an asyncio lock so ten open tabs
  still cost one upstream call.
- **`backend/main.py`** — the `/api/*` routes. Also does the joins the API makes you do
  yourself: `liveryId` → aircraft name + livery name, ATC `type` int → "Tower", grade
  and ATC rank ints → names, `flightTime` minutes → hours.
- **`backend/mock_data.py`** — synthetic responses matching the documented schemas.
- **`frontend/`** — static Leaflet UI. Never sees the key.

**Cache TTLs** (`backend/config.py`): sessions 300s, flights 12s, ATC 30s, world 60s,
routes 30s, aircraft/livery catalogues 3600s. Raise them before you share the app with
anyone else.

## 5. Endpoints exposed

| Route | Upstream |
| --- | --- |
| `GET /api/sessions` | `/sessions` |
| `GET /api/sessions/{id}/flights?q=` | `/sessions/{id}/flights` |
| `GET /api/sessions/{id}/flights/{fid}/route` | flown track |
| `GET /api/sessions/{id}/flights/{fid}/flightplan` | filed plan |
| `GET /api/sessions/{id}/atc` | open frequencies, grouped by airport |
| `GET /api/sessions/{id}/world` | airport traffic |
| `GET /api/sessions/{id}/atis/{icao}` | ATIS text |
| `GET /api/sessions/{id}/airport/{icao}/status` | single airport |
| `GET /api/sessions/{id}/flights/{fid}/destination` | arrival airport + live ETA |
| `POST /api/users/stats` | `/users` (body: `{"discourseNames": ["name"]}`) |
| `GET /api/users/{uid}/flights?page=` | paginated logbook |
| `GET /api/airport/{icao}` | offline airport catalogue (name, city, coords, tz) |
| `GET /api/wx?lat=&lon=` | OpenWeatherMap current + 5-day forecast (proxied) |
| `GET /api/wxtile/{layer}/{z}/{x}/{y}.png` | weather map tiles (key hidden) |
| `GET /api/photo?title=` | Wikipedia summary photo for a city |

Interactive docs at <http://127.0.0.1:8000/docs>.

## 6. Known limitation

The world-status endpoint returns airport traffic without coordinates, so map badges
only appear for airports that have ATC open (those come with lat/lon). Every busy
airport is still listed in the ATC tab. If you want badges everywhere, bundle an
ICAO→coordinate table and join it in `backend/main.py::world`.

## 7. Where to take it next

Each of these is a real step toward AI-engineering work, in rough order of payoff:

1. **Persist positions.** Write every poll to SQLite or Postgres with a timestamp.
   You immediately have a time-series dataset nobody else has.
2. **Then build on it.** ETA prediction from track + flight plan, go-around and
   diversion detection, "which airports are about to get congested" — these are
   supervised learning problems with labels you generate for free.
3. **Natural-language layer.** "Show me every 777 inbound to Heathrow above FL300"
   → an LLM turning that into a filter over `state.flights`. Small, honest use of a
   model where the retrieval is already solved.
4. **WebSockets** instead of polling, so the server polls once and fans out.

## 8. Terms

The Live API is Infinite Flight's, under their terms of use. Keep it non-commercial
unless you've cleared it with them, don't republish the key, and respect the polling
guidance in their best-practices page.
