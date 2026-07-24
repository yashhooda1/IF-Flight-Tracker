/* Infinite Flight Tracker — frontend.
   Talks only to this app's /api/* routes; the API key stays on the server. */

const REFRESH_MS = 15000;

const state = {
  sessionId: null,
  flights: [],
  atc: [],
  world: [],
  selectedFlightId: null,
  followSelection: false,
  timer: null,
  countdown: REFRESH_MS / 1000,
};

const $ = (sel) => document.querySelector(sel);

// ------------------------------------------------------------------- helpers
async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

const fmt = {
  alt: (v) => (v == null ? '—' : Math.round(v).toLocaleString() + ' ft'),
  spd: (v) => (v == null ? '—' : Math.round(v) + ' kts'),
  vs: (v) => (v == null ? '—' : (v > 0 ? '+' : '') + Math.round(v) + ' fpm'),
  deg: (v) => (v == null ? '—' : Math.round(v) + '°'),
  hrs: (v) => (v == null ? '—' : v.toFixed(1) + ' h'),
  date: (s) => (s ? new Date(s.replace(' ', 'T').replace('Z', 'Z')).toLocaleString() : '—'),
};

function phaseColor(f) {
  if ((f.altitude || 0) < 1000) return '#8b98a8';        // ground / pattern
  if ((f.verticalSpeed || 0) > 300) return '#46d17f';    // climbing
  if ((f.verticalSpeed || 0) < -300) return '#ffb454';   // descending
  return '#4ea1ff';                                      // cruise
}

// ---------------------------------------------------------------------- map
const map = L.map('map', { zoomControl: false, worldCopyJump: true }).setView([25, 5], 3);
L.control.zoom({ position: 'bottomright' }).addTo(map);

// Base layers — dark vector (default) and Esri satellite. Both key-free.
const darkBase = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution: '&copy; OpenStreetMap &copy; CARTO · flight data: Infinite Flight Live API',
  maxZoom: 18,
}).addTo(map);
const satelliteBase = L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
  { attribution: 'Imagery &copy; Esri · flight data: Infinite Flight Live API', maxZoom: 18 }
);

// Weather overlays — served through our backend so the OWM key stays private.
const wxLayer = (layer) => L.tileLayer('/api/wxtile/' + layer + '/{z}/{x}/{y}.png',
  { opacity: 0.6, maxZoom: 18 });
const wxClouds = wxLayer('clouds');
const wxPrecip = wxLayer('precipitation');
const wxWind = wxLayer('wind');
const wxTemp = wxLayer('temp');

L.control.layers(
  { 'Dark map': darkBase, 'Satellite': satelliteBase },
  {
    'Clouds': wxClouds,
    'Precipitation': wxPrecip,
    'Wind': wxWind,
    'Temperature': wxTemp,
  },
  { position: 'bottomleft', collapsed: false }
).addTo(map);

const flightLayer = L.layerGroup().addTo(map);
const atcLayer = L.layerGroup().addTo(map);
const airportLayer = L.layerGroup().addTo(map);
const routeLayer = L.layerGroup().addTo(map);
const markers = new Map(); // flightId -> marker

// --------------------------------------------------------- day / night overlay
// Solar terminator: shade the half of the globe currently in darkness.
const nightLayer = L.layerGroup().addTo(map);

function solarDeclination(d) {
  const start = Date.UTC(d.getUTCFullYear(), 0, 0);
  const day = (d - start) / 864e5;
  return -23.44 * Math.cos(((2 * Math.PI) / 365) * (day + 10));
}

function drawTerminator() {
  nightLayer.clearLayers();
  const now = new Date();
  const dec = solarDeclination(now);
  // Subsolar longitude from UTC time.
  const utcHours = now.getUTCHours() + now.getUTCMinutes() / 60 + now.getUTCSeconds() / 3600;
  const subsolarLon = -15 * (utcHours - 12);
  const decRad = (dec * Math.PI) / 180;

  const pts = [];
  for (let lon = -180; lon <= 180; lon += 2) {
    const hourAngle = ((lon - subsolarLon) * Math.PI) / 180;
    // Latitude where the sun is exactly on the horizon for this longitude.
    let lat = Math.atan(-Math.cos(hourAngle) / Math.tan(decRad)) * 180 / Math.PI;
    pts.push([lat, lon]);
  }
  // Close the polygon over whichever pole is in darkness.
  const nightPole = dec > 0 ? -90 : 90;
  pts.push([nightPole, 180], [nightPole, -180]);

  L.polygon(pts, {
    stroke: false, fillColor: '#000', fillOpacity: 0.32, interactive: false,
  }).addTo(nightLayer);
}
drawTerminator();
setInterval(drawTerminator, 60000); // nudge the line once a minute

function planeIcon(f, selected) {
  const color = selected ? '#ffffff' : phaseColor(f);
  const size = selected ? 26 : 20;
  const svg =
    '<svg width="' + size + '" height="' + size + '" viewBox="0 0 24 24" ' +
    'style="transform: rotate(' + (f.track || 0) + 'deg);">' +
    '<path fill="' + color + '" d="M12 2c.6 0 1 .9 1 2v5.2l8 4.4v2l-8-2.4v4.5l2.6 1.8v1.6L12 20l-3.6.9v-1.6L11 17.7v-4.5L3 15.6v-2l8-4.4V4c0-1.1.4-2 1-2z"/>' +
    '</svg>';
  return L.divIcon({ className: 'plane-icon', html: svg, iconSize: [size, size],
                     iconAnchor: [size / 2, size / 2] });
}

// ------------------------------------------------------------------ loading
async function loadSessions() {
  const sessions = await api('/api/sessions');
  const sel = $('#serverSelect');
  sel.innerHTML = '';
  sessions.forEach((s) => {
    const opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = s.name + ' (' + s.userCount + ')';
    sel.appendChild(opt);
  });
  const expert = sessions.find((s) => /expert/i.test(s.name));
  state.sessionId = (expert || sessions[0]).id;
  sel.value = state.sessionId;
}

async function refresh() {
  if (!state.sessionId) return;
  try {
    const [flights, atc, world] = await Promise.all([
      api('/api/sessions/' + state.sessionId + '/flights'),
      api('/api/sessions/' + state.sessionId + '/atc').catch(() => ({ facilities: [], airports: [] })),
      api('/api/sessions/' + state.sessionId + '/world').catch(() => []),
    ]);
    state.flights = flights.flights || [];
    state.atc = atc.airports || [];
    state.world = Array.isArray(world) ? world : [];
    renderFlights();
    renderAtc();
    renderAirports();
    if (state.selectedFlightId) renderDetail(findFlight(state.selectedFlightId));
    $('#status').textContent = state.flights.length + ' flights · updated ' +
      new Date().toLocaleTimeString();
    $('#status').classList.remove('err', 'warming');
  } catch (err) {
    // A blip (e.g. the instance briefly napping) shouldn't look fatal — say so
    // gently and retry on a short timer instead of the full interval.
    $('#status').textContent = 'reconnecting…';
    $('#status').classList.add('warming');
    $('#status').classList.remove('err');
    state.countdown = 4;
    return;
  }
  state.countdown = REFRESH_MS / 1000;
}

const findFlight = (id) => state.flights.find((f) => f.flightId === id);

function filteredFlights() {
  const q = $('#flightSearch').value.trim().toLowerCase();
  if (!q) return state.flights;
  return state.flights.filter((f) =>
    (f.callsign || '').toLowerCase().includes(q) ||
    (f.username || '').toLowerCase().includes(q) ||
    (f.virtualOrganization || '').toLowerCase().includes(q));
}

// ---------------------------------------------------------------- rendering
function renderFlights() {
  const visible = filteredFlights();
  const seen = new Set();

  visible.forEach((f) => {
    seen.add(f.flightId);
    const selected = f.flightId === state.selectedFlightId;
    let m = markers.get(f.flightId);
    if (m) {
      m.setLatLng([f.latitude, f.longitude]);
      m.setIcon(planeIcon(f, selected));
    } else {
      m = L.marker([f.latitude, f.longitude], { icon: planeIcon(f, selected) });
      m.on('click', () => selectFlight(f.flightId));
      m.addTo(flightLayer);
      markers.set(f.flightId, m);
    }
    m.bindTooltip(
      (f.callsign || 'n/a') + ' · ' + fmt.alt(f.altitude),
      { direction: 'top', offset: [0, -10] }
    );
  });

  for (const [id, m] of markers) {
    if (!seen.has(id)) { flightLayer.removeLayer(m); markers.delete(id); }
  }

  const list = $('#flightList');
  list.innerHTML = '';
  visible.slice(0, 300).forEach((f) => {
    const li = document.createElement('li');
    if (f.flightId === state.selectedFlightId) li.className = 'sel';
    li.innerHTML =
      '<div class="row"><span class="callsign">' + esc(f.callsign || '—') +
      '</span><span class="sub mono">' + fmt.alt(f.altitude) + '</span></div>' +
      '<div class="row sub"><span>' + esc(f.username || 'unlinked account') +
      '</span><span>' + esc(f.aircraftName || '') + '</span></div>';
    li.onclick = () => { selectFlight(f.flightId); panTo(f); };
    list.appendChild(li);
  });
  $('#flightCount').textContent = visible.length + ' of ' + state.flights.length + ' flights';
}

function renderAtc() {
  atcLayer.clearLayers();
  const list = $('#atcList');
  list.innerHTML = '';
  let total = 0;

  state.atc.forEach((apt) => {
    total += apt.frequencies.length;
    const types = apt.frequencies.map((f) => f.typeName).join(' · ');
    if (apt.latitude != null) {
      L.marker([apt.latitude, apt.longitude], {
        icon: L.divIcon({ className: '', html: '<div class="atc-badge">' + esc(apt.icao) + '</div>',
                          iconSize: [null, null] }),
      }).bindPopup('<strong>' + esc(apt.icao) + '</strong><br>' +
        apt.frequencies.map((f) => esc(f.typeName) + ' — ' + esc(f.username || 'unknown')).join('<br>'))
        .addTo(atcLayer);
    }
    const li = document.createElement('li');
    li.innerHTML =
      '<div class="row"><span class="callsign">' + esc(apt.icao) + '</span>' +
      '<span class="tag atc">' + apt.frequencies.length + '</span></div>' +
      '<div class="sub">' + esc(types) + '</div>' +
      '<div class="sub">' + esc([...new Set(apt.frequencies.map((f) => f.username).filter(Boolean))].join(', ')) + '</div>';
    li.onclick = () => { map.setView([apt.latitude, apt.longitude], 9); showAtis(apt.icao); };
    list.appendChild(li);
  });

  $('#atcCount').textContent = total + ' open frequencies · ' + state.atc.length + ' airports';
}

function renderAirports() {
  airportLayer.clearLayers();
  const busy = state.world.filter((a) => (a.traffic || 0) > 0);

  // Map badges: the Live API only gives coordinates for ATC-staffed airports.
  busy.forEach((a) => {
    if (a.latitude == null || a.longitude == null) return;
    L.marker([a.latitude, a.longitude], {
      icon: L.divIcon({
        className: '',
        html: '<div class="apt-badge">' + esc(a.airportIcao) + ' &#8595;' +
              (a.inboundFlightsCount || 0) + ' &#8593;' + (a.outboundFlightsCount || 0) + '</div>',
        iconSize: [null, null],
      }),
    }).on('click', () => showAirport(a)).addTo(airportLayer);
  });

  // Everything else is reachable from the list.
  const list = $('#aptList');
  list.innerHTML = '';
  busy.slice(0, 40).forEach((a) => {
    const li = document.createElement('li');
    li.innerHTML =
      '<div class="row"><span class="callsign">' + esc(a.airportIcao || '—') + '</span>' +
      '<span class="sub mono">&#8595;' + (a.inboundFlightsCount || 0) +
      ' &#8593;' + (a.outboundFlightsCount || 0) + '</span></div>' +
      (a.atcFacilities.length
        ? '<div class="sub">' + esc(a.atcFacilities.map((f) => f.typeName).join(' · ')) + '</div>'
        : '<div class="sub">unstaffed</div>');
    li.onclick = () => showAirport(a);
    list.appendChild(li);
  });
}

async function showAirport(a) {
  if (a.latitude != null) map.setView([a.latitude, a.longitude], 9);
  openDetail(
    '<h2>' + esc(a.airportIcao) + '</h2>' +
    '<dl class="kv">' +
    kv('Inbound', a.inboundFlightsCount || 0) +
    kv('Outbound', a.outboundFlightsCount || 0) +
    kv('Open frequencies', a.atcFacilities.length) +
    '</dl>' +
    (a.atcFacilities.length
      ? '<div class="sub" style="margin-top:8px">' +
        a.atcFacilities.map((f) => esc(f.typeName) + ' — ' + esc(f.username || 'unknown')).join('<br>') +
        '</div>'
      : '') +
    '<div id="atisBox" class="sub" style="margin-top:10px">loading ATIS…</div>'
  );
  try {
    const r = await api('/api/sessions/' + state.sessionId + '/atis/' + a.airportIcao);
    const box = $('#atisBox');
    if (box) box.innerHTML = '<strong>ATIS</strong><br>' + esc(r.atis || 'No ATIS available.');
  } catch (_) {
    const box = $('#atisBox');
    if (box) box.textContent = 'No ATIS available.';
  }
}

async function showAtis(icao) {
  try {
    const r = await api('/api/sessions/' + state.sessionId + '/atis/' + icao);
    openDetail('<h2>' + esc(icao) + '</h2><div class="sub">ATIS</div><p>' +
      esc(r.atis || 'No ATIS available.') + '</p>');
  } catch (err) {
    openDetail('<h2>' + esc(icao) + '</h2><p class="err">' + esc(err.message) + '</p>');
  }
}

// ------------------------------------------------------------------ detail
function selectFlight(id) {
  state.selectedFlightId = id;
  renderFlights();
  const f = findFlight(id);
  if (f) { renderDetail(f); loadRoute(f); loadDestination(f); }
}

function panTo(f) { map.setView([f.latitude, f.longitude], Math.max(map.getZoom(), 6)); }

function renderDetail(f) {
  if (!f) return;
  openDetail(
    '<h2>' + esc(f.callsign || 'Unknown callsign') + '</h2>' +
    '<div class="sub">' + esc(f.username || 'unlinked account') +
    (f.virtualOrganization ? ' · <span class="tag">' + esc(f.virtualOrganization) + '</span>' : '') +
    '</div>' +
    '<dl class="kv">' +
    kv('Aircraft', f.aircraftName || '—') +
    kv('Livery', f.liveryName || '—') +
    kv('Altitude', fmt.alt(f.altitude)) +
    kv('Ground speed', fmt.spd(f.speed)) +
    kv('Vertical speed', fmt.vs(f.verticalSpeed)) +
    kv('Track / heading', fmt.deg(f.track) + ' / ' + fmt.deg(f.heading)) +
    kv('Position', f.latitude.toFixed(3) + ', ' + f.longitude.toFixed(3)) +
    kv('Last report', fmt.date(f.lastReport)) +
    '</dl>' +
    '<div id="destBox" class="destbox">loading destination…</div>' +
    '<div id="fpBox" class="sub" style="margin-top:10px">loading flight plan…</div>' +
    '<div class="pager"><button id="pilotFromFlight">Pilot logbook</button>' +
    '<button id="centreBtn">Centre map</button></div>'
  );
  $('#centreBtn').onclick = () => panTo(f);
  $('#pilotFromFlight').onclick = () => {
    switchTab('pilot');
    if (f.username) { $('#pilotName').value = f.username; $('#pilotForm').requestSubmit(); }
    else { $('#pilotResult').innerHTML = '<p class="err">This flight has no linked forum account.</p>'; }
  };
}

async function loadRoute(f) {
  routeLayer.clearLayers();
  const base = '/api/sessions/' + state.sessionId + '/flights/' + f.flightId;

  api(base + '/route').then((points) => {
    if (!Array.isArray(points) || !points.length) return;
    const latlngs = points.map((p) => [p.latitude, p.longitude]);
    L.polyline(latlngs, { color: '#4ea1ff', weight: 2, opacity: 0.85 }).addTo(routeLayer);
  }).catch(() => {});

  api(base + '/flightplan').then((fp) => {
    const items = (fp && fp.flightPlanItems) || [];
    const pts = [];
    const walk = (nodes) => nodes.forEach((n) => {
      if (n.children && n.children.length) walk(n.children);
      else if (n.location && n.location.latitude) pts.push(n);
    });
    walk(items);
    if (pts.length > 1) {
      L.polyline(pts.map((p) => [p.location.latitude, p.location.longitude]),
        { color: '#ffb454', weight: 1.5, dashArray: '5,6', opacity: 0.9 }).addTo(routeLayer);
      pts.forEach((p) => L.circleMarker([p.location.latitude, p.location.longitude],
        { radius: 2.5, color: '#ffb454', fillOpacity: 1 })
        .bindTooltip(esc(p.identifier || p.name || '')).addTo(routeLayer));
    }
    const box = $('#fpBox');
    if (box) {
      box.innerHTML = pts.length
        ? '<strong>Flight plan</strong><br>' + esc(pts.map((p) => p.identifier || p.name).join(' → '))
        : 'No flight plan filed.';
    }
  }).catch(() => { const box = $('#fpBox'); if (box) box.textContent = 'No flight plan filed.'; });
}

// ------------------------------------------------------- destination + weather
const owmIcon = (code) =>
  code ? 'https://openweathermap.org/img/wn/' + code + '@2x.png' : null;

function localTime(tz) {
  if (!tz) return null;
  try {
    return new Intl.DateTimeFormat('en-GB',
      { timeZone: tz, hour: '2-digit', minute: '2-digit' }).format(new Date());
  } catch (_) { return null; }
}

function etaText(d) {
  if (d.etaMinutes == null) {
    return d.distanceNm != null ? d.distanceNm.toLocaleString() + ' nm to run' : '—';
  }
  const h = Math.floor(d.etaMinutes / 60);
  const m = d.etaMinutes % 60;
  const arrive = d.destination && d.destination.tz && d.etaUtc
    ? new Intl.DateTimeFormat('en-GB',
        { timeZone: d.destination.tz, hour: '2-digit', minute: '2-digit' })
        .format(new Date(d.etaUtc.replace(' ', 'T')))
    : null;
  return (h ? h + 'h ' : '') + m + 'm' +
    (d.distanceNm != null ? ' · ' + d.distanceNm.toLocaleString() + ' nm' : '') +
    (arrive ? ' · arr ' + arrive + ' local' : '');
}

async function loadDestination(f) {
  const box = () => $('#destBox');
  let dest;
  try {
    dest = await api('/api/sessions/' + state.sessionId + '/flights/' + f.flightId + '/destination');
  } catch (_) { if (box()) box().style.display = 'none'; return; }

  if (f.flightId !== state.selectedFlightId) return;      // selection moved on
  if (!dest.hasDestination || !dest.destination) {
    if (box()) box().innerHTML = '<div class="hint">Destination</div>' +
      '<div class="sub">No arrival airport in the filed plan.</div>';
    return;
  }

  const a = dest.destination;
  const lt = localTime(a.tz);
  if (box()) {
    box().innerHTML =
      '<div class="hint">Destination</div>' +
      '<div class="desthead">' +
      '<div><div class="callsign">' + esc(a.icao) + (a.iata ? ' / ' + esc(a.iata) : '') + '</div>' +
      '<div class="sub">' + esc([a.city, a.country].filter(Boolean).join(', ')) + '</div></div>' +
      (lt ? '<div class="localtime">' + esc(lt) + '<div class="sub">local</div></div>' : '') +
      '</div>' +
      '<div class="sub">' + esc(a.name) + '</div>' +
      '<dl class="kv">' +
      kv('ETA', etaText(dest)) +
      kv('Field elevation', a.elevation != null ? Math.round(a.elevation).toLocaleString() + ' ft' : '—') +
      '</dl>' +
      '<div id="wxBox" class="sub">loading weather…</div>' +
      '<div id="photoBox"></div>';
  }

  // Draw destination marker on the plan overlay.
  if (a.latitude != null) {
    L.marker([a.latitude, a.longitude], {
      icon: L.divIcon({ className: '',
        html: '<div class="dest-badge">&#9992; ' + esc(a.icao) + '</div>', iconSize: [null, null] }),
    }).addTo(routeLayer);
  }

  loadWeather(a);
  loadPhoto(a.city, f.flightId);
}

async function loadWeather(a) {
  let wx;
  try {
    wx = await api('/api/wx?lat=' + a.latitude + '&lon=' + a.longitude);
  } catch (_) { const b = $('#wxBox'); if (b) b.textContent = 'Weather unavailable.'; return; }
  const b = $('#wxBox');
  if (!b) return;

  if (wx.provider === 'unavailable') {
    b.innerHTML = '<span class="sub">Add an OpenWeatherMap key (IF_OWM_API_KEY) for live weather.</span>';
    return;
  }
  const c = wx.current;
  const icon = owmIcon(c.icon);
  b.innerHTML =
    '<div class="hint" style="margin-top:6px">Weather at arrival</div>' +
    '<div class="wxnow">' +
    (icon ? '<img src="' + icon + '" width="48" height="48" alt="">' : '') +
    '<div class="wxtemp">' + Math.round(c.temp) + '°C</div>' +
    '<div class="sub">' + esc(c.description || '') +
    '<br>wind ' + Math.round(c.windSpeedKts) + ' kt' +
    (c.windDir != null ? ' @ ' + Math.round(c.windDir) + '°' : '') +
    '<br>cloud ' + (c.cloudCover != null ? c.cloudCover + '%' : '—') +
    ' · vis ' + (c.visibilityKm != null ? c.visibilityKm + ' km' : '—') +
    '</div></div>' +
    (wx.forecast && wx.forecast.length
      ? '<div class="fcstrip">' + wx.forecast.map((d) => {
          const wd = new Date(d.date).toLocaleDateString('en-GB', { weekday: 'short' });
          const fi = owmIcon(d.icon);
          return '<div class="fcday"><div class="sub">' + esc(wd) + '</div>' +
            (fi ? '<img src="' + fi + '" width="34" height="34" alt="">' : '') +
            '<div class="mono">' + Math.round(d.tempMax) + '°</div>' +
            '<div class="sub mono">' + Math.round(d.tempMin) + '°</div></div>';
        }).join('') + '</div>'
      : '');
}

async function loadPhoto(city, flightId) {
  if (!city) return;
  let p;
  try { p = await api('/api/photo?title=' + encodeURIComponent(city)); }
  catch (_) { return; }
  if (flightId !== state.selectedFlightId) return;
  const box = $('#photoBox');
  if (!box || !p || !p.thumbnail) return;
  box.innerHTML =
    '<div class="hint" style="margin-top:8px">' + esc(p.title || city) + '</div>' +
    '<img class="cityphoto" src="' + esc(p.originalImage || p.thumbnail) + '" alt="' + esc(city) + '">' +
    (p.extract ? '<div class="sub photocap">' + esc(p.extract.slice(0, 160)) +
      (p.extract.length > 160 ? '…' : '') + '</div>' : '');
}

function openDetail(html) {
  $('#detailBody').innerHTML = html;
  $('#detail').classList.remove('hidden');
}
const kv = (k, v) => '<dt>' + esc(k) + '</dt><dd>' + esc(String(v)) + '</dd>';
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ------------------------------------------------------------------- pilot
async function lookupPilot(name) {
  const out = $('#pilotResult');
  out.innerHTML = '<p class="sub">searching…</p>';
  try {
    const r = await api('/api/users/stats', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ discourseNames: [name] }),
    });
    const u = (r.users || [])[0];
    if (!u) { out.innerHTML = '<p class="err">No user found.</p>'; return; }

    const live = state.flights.find((f) => (f.username || '').toLowerCase() === name.toLowerCase());
    out.innerHTML =
      '<h2 style="margin:6px 0 0">' + esc(u.discourseUsername || name) + '</h2>' +
      '<div class="sub">' + esc(u.virtualOrganization || 'no virtual organisation') + '</div>' +
      '<dl class="kv">' +
      kv('Grade', u.gradeName || '—') +
      kv('XP', (u.xp || 0).toLocaleString()) +
      kv('Online flights', (u.onlineFlights || 0).toLocaleString()) +
      kv('Flight time', fmt.hrs(u.flightTimeHours)) +
      kv('Landings', (u.landingCount || 0).toLocaleString()) +
      kv('Violations', u.violations ?? 0) +
      kv('ATC ops', (u.atcOperations || 0).toLocaleString()) +
      (u.atcRankName ? kv('ATC rank', u.atcRankName) : '') +
      '</dl>' +
      (live
        ? '<p><button id="jumpLive">Airborne now as ' + esc(live.callsign) + ' — track</button></p>'
        : '<p class="sub">Not currently flying on this server.</p>') +
      '<div id="logbook"></div>';

    if (live) $('#jumpLive').onclick = () => { switchTab('flights'); selectFlight(live.flightId); panTo(live); };
    loadLogbook(u.userId, 1);
  } catch (err) {
    out.innerHTML = '<p class="err">' + esc(err.message) + '</p>';
  }
}

async function loadLogbook(userId, page) {
  const box = $('#logbook');
  box.innerHTML = '<p class="sub">loading logbook…</p>';
  try {
    const r = await api('/api/users/' + userId + '/flights?page=' + page);
    const rows = (r && r.data) || [];
    if (!rows.length) { box.innerHTML = '<p class="sub">No logged flights.</p>'; return; }
    box.innerHTML =
      '<div class="hint" style="margin-top:10px">Logbook</div>' +
      '<table class="log"><thead><tr><th>Date</th><th>Route</th><th>Callsign</th><th>Time</th></tr></thead><tbody>' +
      rows.map((f) =>
        '<tr><td>' + esc(new Date(f.created).toLocaleDateString()) + '</td>' +
        '<td>' + esc((f.originAirport || '???') + '→' + (f.destinationAirport || '???')) + '</td>' +
        '<td>' + esc(f.callsign || '—') + '</td>' +
        '<td>' + fmt.hrs(f.totalTime) + '</td></tr>').join('') +
      '</tbody></table>' +
      '<div class="pager">' +
      '<button ' + (r.hasPreviousPage ? '' : 'disabled') + ' id="prevPage">Prev</button>' +
      '<span class="sub">page ' + r.pageIndex + ' / ' + r.totalPages + '</span>' +
      '<button ' + (r.hasNextPage ? '' : 'disabled') + ' id="nextPage">Next</button></div>';
    const prev = $('#prevPage'), next = $('#nextPage');
    if (prev && !prev.disabled) prev.onclick = () => loadLogbook(userId, page - 1);
    if (next && !next.disabled) next.onclick = () => loadLogbook(userId, page + 1);
  } catch (err) {
    box.innerHTML = '<p class="err">' + esc(err.message) + '</p>';
  }
}

// -------------------------------------------------------------------- wiring
function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tabpane').forEach((p) => p.classList.toggle('active', p.id === 'pane-' + name));
}
document.querySelectorAll('.tab').forEach((t) => (t.onclick = () => switchTab(t.dataset.tab)));

$('#serverSelect').onchange = (e) => {
  state.sessionId = e.target.value;
  state.selectedFlightId = null;
  routeLayer.clearLayers();
  flightLayer.clearLayers();
  markers.clear();
  refresh();
};
$('#flightSearch').oninput = renderFlights;
$('#refreshBtn').onclick = refresh;
$('#detailClose').onclick = () => {
  $('#detail').classList.add('hidden');
  state.selectedFlightId = null;
  routeLayer.clearLayers();
  renderFlights();
};
$('#showAtc').onchange = (e) => (e.target.checked ? map.addLayer(atcLayer) : map.removeLayer(atcLayer));
$('#showAirports').onchange = (e) =>
  (e.target.checked ? map.addLayer(airportLayer) : map.removeLayer(airportLayer));
$('#pilotForm').onsubmit = (e) => {
  e.preventDefault();
  const name = $('#pilotName').value.trim();
  if (name) lookupPilot(name);
};

setInterval(() => {
  if (!$('#autoRefresh').checked) { $('#countdown').textContent = '–'; return; }
  state.countdown -= 1;
  $('#countdown').textContent = Math.max(0, state.countdown);
  if (state.countdown <= 0) refresh();
}, 1000);

(async function init() {
  const banner = $('#wakeBanner');
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  try {
    // First contact may land on a sleeping free-tier instance (~50s to wake).
    // Retry patiently behind a friendly banner instead of failing hard.
    let health;
    for (let i = 0; ; i++) {
      try { health = await api('/api/health'); break; }
      catch (err) {
        if (i === 0) banner.classList.remove('hidden');
        if (i >= 20) throw err;                 // ~80s of retries, then give up
        $('#wakeMsg').textContent = 'Waking the server…';
        $('#status').textContent = 'waking up…';
        $('#status').classList.add('warming');
        await sleep(4000);
      }
    }
    banner.classList.add('hidden');
    $('#status').classList.remove('warming');
    if (health.mode === 'mock') $('#status').textContent = 'sample data (IF_MOCK=true)';
    await loadSessions();
    await refresh();
  } catch (err) {
    banner.classList.add('hidden');
    $('#status').textContent = err.message;
    $('#status').classList.add('err');
  }
})();
