// ── CONFIG ───────────────────────────────────────────────────────────────

// No LINZ key here anymore — it lives server-side.
const API_BASE =
  'https://k3w7aj90ok.execute-api.ap-southeast-2.amazonaws.com';

const LINZ_ORIGIN =
  'https://basemaps.linz.govt.nz/v1/';

// Route generation Lambda Function URL
const ROUTE_API_URL =
  'https://5ppc3q6n5l2575pollyhodeysm0skake.lambda-url.ap-southeast-2.on.aws/';


// ── LINZ MAP URLS ────────────────────────────────────────────────────────

const AERIAL_URL =
  `${API_BASE}/proxy/tiles/aerial/WebMercatorQuad/{z}/{x}/{y}.webp`;

const TOPO_STYLE_URL =
  `${API_BASE}/proxy/tiles/topographic/WebMercatorQuad/style/topographic.json`;


// ── LINZ REQUEST PROXY ──────────────────────────────────────────────────

function transformRequest(url, resourceType) {

  if (url.startsWith(LINZ_ORIGIN)) {

    const fixedUrl = url.replace(
      '.png&pipeline=',
      '.png?pipeline='
    );

    const parsed = new URL(fixedUrl);

    // Remove LINZ API key if present
    parsed.searchParams.delete('api');

    const path = parsed.pathname.replace(/^\/v1\//, '');

    return {
      url: API_BASE + '/proxy/' + path + parsed.search
    };
  }

  return { url };
}


// Reduce simultaneous image requests if supported
if (
  typeof maplibregl.setMaxParallelImageRequests ===
  'function') {
  maplibregl.setMaxParallelImageRequests(4);
  }


// ========================================================================
// MAP
// ========================================================================

const map = new maplibregl.Map({
  container: 'map',

  style: {
    version: 8,
    sprite: `${API_BASE}/proxy/sprites/topographic`,

    glyphs:`${API_BASE}/proxy/fonts/{fontstack}/{range}.pbf`,

    sources: {
      aerial: {
        type: 'raster',
        tiles: [AERIAL_URL],

        tileSize: 256,
        attribution:'© LINZ CC BY 4.0'}
    },

    layers: [
      {
        id: 'aerial-layer',
        type: 'raster',
        source: 'aerial',
        paint: {'raster-opacity': 1}
      }
    ]
  },

  center: [172.5,-41.0],
  zoom: 5,
  minZoom: 4,
  maxZoom: 19,

  // Disabled here and added manually below, so it can be explicitly
  // anchored to bottom-left instead of MapLibre's default bottom-right —
  // that corner was colliding with the coordinate display bubble once
  // the attribution text grew to include OSM's credit alongside LINZ's.
  attributionControl: false,

  transformRequest:
    transformRequest
});

function resizeMap() {
  map.resize();
}

map.on("load", () => {
  requestAnimationFrame(resizeMap);
  setTimeout(resizeMap, 250);
});

window.addEventListener("resize", resizeMap);
window.addEventListener("orientationchange", () => {
  setTimeout(resizeMap, 250);
});

// Attribution is presented in the single combined notice in RidgeWalkerHome.html.


// ========================================================================
// LAMBDA WARM-UP
// ========================================================================
// Fired once, immediately — well before the user places any waypoints or
// clicks Generate Route. Lets the backend's expensive one-time cold-start
// work (loading the national water/stream/bridge dataset, JIT-compiling
// the pathfinding core) happen while they're just looking at the map,
// rather than after they click Generate and are actively watching a
// spinner. Same total backend cost either way — this only moves WHEN
// it's paid to a point in the experience where it's invisible.
//
// Genuine fire-and-forget: nothing here should ever block, delay, or
// visibly affect anything else on the page, so failures are swallowed
// silently — worst case, the first real route request just pays the
// cold-start cost itself, exactly as it would without this at all.
fetch(ROUTE_API_URL, {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({warm: true}),
}).catch(() => {});


// ========================================================================
// TOPOGRAPHIC MAP
// ========================================================================

let topoLayerIds = [];
let topoVisible = true;
let topoOpacity = 0.6;


map.on('load', async () => {

  try {
    const resp = await fetch(TOPO_STYLE_URL);
    const style = await resp.json();


    // Add topo sources
    for (
      const [id, src]
      of Object.entries(style.sources || {})
    ) {

      if (!map.getSource(id)) {
        map.addSource(id, src);
      }
    }


    // Add topo layers
    for (
      const layer
      of (style.layers || [])
    ) {

      if (!map.getLayer(layer.id)) {
        map.addLayer(layer);
        topoLayerIds.push(
          layer.id
        );
      }
    }


    // Set starting opacity
    applyTopoOpacity(topoOpacity);


  } catch (err) {

    console.error('Failed to load LINZ topo style:',err);
  }

});


// ── Topo opacity helper ──────────────────────────────────────────────────

function applyTopoOpacity(opacity) {
  for (const id of topoLayerIds) {
    if (!map.getLayer(id)) {
      continue;}
    const type = map.getLayer(id).type;
    try {
      if (type === 'fill') {
        map.setPaintProperty(id, 'fill-opacity', opacity);
      }
      if (type === 'background') {
        map.setPaintProperty(id, 'background-opacity', opacity);
      }
      if (type === 'line') {
        map.setPaintProperty(id,'line-opacity',opacity);
      }
      if (type === 'symbol') {
        map.setPaintProperty(id,'icon-opacity', opacity);

        map.setPaintProperty(id,'text-opacity',opacity);
      }
      if (type === 'circle') {
        map.setPaintProperty(id,'circle-opacity',opacity);
      }
      if (type === 'raster') {map.setPaintProperty(id,'raster-opacity',opacity);
      }

    } catch (_) {
      // Ignore unsupported opacity properties
    }
  }
}


// ========================================================================
// COORDINATE DISPLAY
// ========================================================================

const coordsEl = document.getElementById('coords');

map.on('mousemove', event => {const {lng,lat} = event.lngLat;
  coordsEl.textContent =`Longitude: ${lng.toFixed(5)}° Latitude: ${lat.toFixed(5)}° `;
});


// ========================================================================
// MAP LAYER CONTROLS
// ========================================================================

// Aerial imagery
document
  .getElementById('toggle-aerial')
  .addEventListener('change',event => {
      map.setLayoutProperty('aerial-layer','visibility', event.target.checked? 'visible': 'none');
    }
  );


// Topographic map
document
  .getElementById('toggle-topo')
  .addEventListener('change',
    event => {topoVisible = event.target.checked;
      const visibility =topoVisible? 'visible': 'none';

      for (const id of topoLayerIds) {
        if (map.getLayer(id)) {
          map.setLayoutProperty(id, 'visibility', visibility);
        }
      }
    }
  );

  // ── Opacity slider ───────────────────────────────────────────────────────
  const opacitySlider = document.getElementById('topo-opacity');
  const opacityVal    = document.getElementById('opacity-val');
  opacitySlider.addEventListener('input', () => {
    topoOpacity = opacitySlider.value / 100;
    applyTopoOpacity(topoOpacity);
    opacityVal.textContent = `${opacitySlider.value}%`;
  });


  // ── Route planner ────────────────────────────────────────────────────────
  // Add the ability to have multiple waypoints, waypoints will now be stored in an array
  // Each point will have a role assigned to it (start, end, via) and the role will be assigned based
  // on the position of the waypoint on the array. this will make adding and removing waypoints more seamlessly
  // Add waypoint button will add waypoints on the map and the points will be displayed on the side panel 
  // Each pin owns its own drag gesture
  // (MapLibre disables map panning while a marker drag is in progress), so
  // this sidesteps any conflict with the map's own click-and-drag panning.

  const START_COLOR = getComputedStyle(document.documentElement).getPropertyValue('--accent'); //'#e2660a'
  const END_COLOR   = getComputedStyle(document.documentElement).getPropertyValue('--accent2'); //'#178f66'
  const VIA_COLOR   = getComputedStyle(document.documentElement).getPropertyValue('--via'); //'#2f6fed'
  const PREVIEW_LINE_COLOR = '#7a7266'; // matches --muted — marks it as a straight preview, not a real route

  const MAX_WAYPOINTS = 7; // start + up to 5 via points + end

  const routeHintEl      = document.getElementById('route-hint');
  const routePointsEl    = document.getElementById('route-points');
  const addWaypointBtn   = document.getElementById('add-waypoint');
  const generateRouteBtn = document.getElementById('generate-route');
  const clearRouteBtn    = document.getElementById('clear-route');
  const routeStatsEl     = document.getElementById('route-stats');
  const statDistanceEl   = document.getElementById('stat-distance');
  const statTimeEl       = document.getElementById('stat-time');
  const statClimbEl      = document.getElementById('stat-climb');
  const routeErrorEl     = document.getElementById('route-error');
  const exportGPXBtn     = document.getElementById('export-gpx');

  function resetRouteStats() {
    statDistanceEl.textContent = '—';
    statTimeEl.textContent = '—';
    statClimbEl.textContent = '—';
    routeStatsEl.hidden = true;
  }

  // main array to store all waypoints 
  // instead of having a fixed start and end point, the waypoints will be assigned a role (start, end, via)
  // the roles will be assigned based on the point's position in the array 
  let waypoints = [];  
  let routeLoading = false;
  let currentRouteFeature = null;
  let currentRouteGPX = null;

  // ── Route mode ────────────────────────────────────────────────────────
  // 'fixed' = classic start/end pin placement (click-to-place, max 2 points)
  // 'multi' = existing waypoint-list flow (add-at-center, up to MAX_WAYPOINTS)
  let routeMode = 'fixed';
  let fixedStart = null; // { lngLat, marker }
  let fixedEnd = null;

  const modeSwitchEl   = document.getElementById('mode-switch');
  const modeFixedEl    = document.getElementById('mode-fixed');
  const modeMultiEl    = document.getElementById('mode-multi');
  const placeStartBtn  = document.getElementById('place-start');
  const placeEndBtn    = document.getElementById('place-end');
  const pointADotEl    = document.getElementById('point-a-dot');
  const pointBDotEl    = document.getElementById('point-b-dot');
  const pointACoordsEl = document.getElementById('point-a-coords');
  const pointBCoordsEl = document.getElementById('point-b-coords');
  const pointARowEl    = document.getElementById('point-a-row');
  const pointBRowEl    = document.getElementById('point-b-row');
  const fixedPointsListEl = document.getElementById('fixed-points-list');

  function formatCoords(lon, lat) {
    return `${lat.toFixed(4)}°, ${lon.toFixed(4)}°`;
  }

  function formatDuration(hours) {
    const h = Math.floor(hours);
    const m = Math.round((hours - h) * 60);
    return `${h}h ${m}m`;
  }

  function roleForIndex(i, total) {
    if (i === 0) return 'start';
    if (i === total - 1) return 'end';
    return 'via';
  }

  function labelForIndex(i, total) {
    const role = roleForIndex(i, total);
    if (role === 'start') return 'Start';
    if (role === 'end') return 'End';
    return `Waypoint ${i}`;
  }

  function colorForRole(role) {
    if (role === 'start') return START_COLOR;
    if (role === 'end') return END_COLOR;
    return VIA_COLOR;
  }

  // displays the error message in a more readable way for users
  // instead of coordinates, display the name of the points that have errors instead
  function friendlyRouteError(message) {
    if (typeof message !== 'string') return message;

    const match = message.match(/^Leg (\d+) \(.+\) failed: (.+)$/s);
    if (!match) return message;

    const routeIndex = parseInt(match[1], 10) - 1;
    const reason = match[2];
    const fromLabel = labelForIndex(routeIndex, waypoints.length);
    const toLabel = labelForIndex(routeIndex + 1, waypoints.length);

    return `${fromLabel} → ${toLabel} failed: ${reason}`;
  }


// ========================================================================
// ROUTE HINT
// ========================================================================

function updateRouteHint() {

  if (routeLoading) {return;}

  if (routeMode === 'fixed') {
    if (fixedStart && fixedEnd) {
      routeHintEl.textContent = 'Ready — hit Generate Route';
    } else if (!fixedStart) {
      routeHintEl.textContent = "Press 'Start Pin' to place your starting point";
    } else {
      routeHintEl.textContent = "Press 'End Pin' to place your end point, then drag pins to fine-tune";
    }
    return;
  }

  if (waypoints.length === 0) {
    routeHintEl.textContent = 'Add at least two waypoints to plan a route';
  } else if (waypoints.length === 1) {
    routeHintEl.textContent = 'Add an end point, then drag pins to fine-tune';
  } else if (waypoints.length === 2) {
    routeHintEl.textContent = 'Add a waypoint to your route';
  } else if (waypoints.length === 3) {
    routeHintEl.textContent = 'Ready — hit Generate Route to pass through 1 waypoint, or add waypoints as needed';
  } else {
    routeHintEl.textContent =
      `Ready — hit Generate Route to pass through ${waypoints.length - 2} waypoints.`;
  }
}

function updateGenerateButton() {
  const ready = routeMode === 'fixed'
    ? !!(fixedStart && fixedEnd)
    : waypoints.length >= 3;
  generateRouteBtn.disabled = !ready || routeLoading;
}

function updateAddButton() {
  const atMax = waypoints.length >= MAX_WAYPOINTS;
  addWaypointBtn.disabled = atMax || routeLoading;
  addWaypointBtn.textContent = atMax ? 'Max Waypoints Placed' : '+ Add Waypoint';
}


// ========================================================================
// REMOVE ROUTE FROM MAP
// ========================================================================

function clearRouteLine() {
  if (routeDrawAnimationId !== null) {
    cancelAnimationFrame(routeDrawAnimationId);
    routeDrawAnimationId = null;
  }
  if (map.getLayer('route-line')) {
    map.removeLayer('route-line');
    }
  if (map.getLayer('route-glow')) {
    map.removeLayer('route-glow');
    }
  if (map.getSource('route')) {
    map.removeSource('route');
  }
  currentRouteFeature = null;
  currentRouteGPX = null;

  if (exportGPXBtn) {
  exportGPXBtn.disabled = true;
  exportGPXBtn.classList.remove('is-ready');
}
}


// ========================================================================
// STRAIGHT-LINE PREVIEW (client-side only, no backend call)
// ========================================================================
// Rebuilding this line on every single 'drag' event (which can fire dozens
// of times a second on a fast drag) is what caused it to flicker/disappear.
// Instead we just record that a redraw is needed and do at most one per
// animation frame, so it always draws smoothly no matter how fast you drag.

let previewFrameId = null;

function getPreviewCoordinates() {
  if (routeMode === 'fixed') {
    if (!fixedStart || !fixedEnd) return null;
    return [fixedStart.lngLat, fixedEnd.lngLat];
  }
  if (waypoints.length < 2) return null;
  return waypoints.map(wp => wp.lngLat);
}

function renderPreviewLineNow() {
  previewFrameId = null;

  const coordinates = getPreviewCoordinates();
  if (!coordinates) {
    removeWaypointPreview();
    return;
  }

  const geojson = {
    type: 'Feature',
    geometry: { type: 'LineString', coordinates },
  };

  if (map.getSource('waypoint-preview')) {
    map.getSource('waypoint-preview').setData(geojson);
  } else {
    map.addSource('waypoint-preview', { type: 'geojson', data: geojson });
    map.addLayer({
      id: 'waypoint-preview-line',
      type: 'line',
      source: 'waypoint-preview',
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: {
        'line-color': PREVIEW_LINE_COLOR,
        'line-width': 2.5,
        'line-dasharray': [2, 2],
        'line-opacity': 0.85,
      },
    });
  }
}

function scheduleWaypointPreview() {
  if (previewFrameId !== null) return;
  previewFrameId = requestAnimationFrame(renderPreviewLineNow);
}

function removeWaypointPreview() {
  if (previewFrameId !== null) {
    cancelAnimationFrame(previewFrameId);
    previewFrameId = null;
  }
  if (map.getLayer('waypoint-preview-line')) map.removeLayer('waypoint-preview-line');
  if (map.getSource('waypoint-preview')) map.removeSource('waypoint-preview');
}


// ========================================================================
// INVALIDATE EXISTING ROUTE
// ========================================================================

function invalidateRoute() {
  clearRouteLine();
  scheduleWaypointPreview();
  resetRouteStats();
  routeErrorEl.hidden = true;
}


// ========================================================================
// FIXED (2-POINT) MODE — PIN PLACEMENT
// ========================================================================

function renderFixedPoints() {
  pointARowEl.hidden = !fixedStart;
  pointACoordsEl.textContent = fixedStart ? formatCoords(fixedStart.lngLat[0], fixedStart.lngLat[1]) : 'Not set';
  pointACoordsEl.classList.toggle('is-set', !!fixedStart);
  pointADotEl.classList.toggle('is-set', !!fixedStart);

  pointBRowEl.hidden = !fixedEnd;
  pointBCoordsEl.textContent = fixedEnd ? formatCoords(fixedEnd.lngLat[0], fixedEnd.lngLat[1]) : 'Not set';
  pointBCoordsEl.classList.toggle('is-set', !!fixedEnd);
  pointBDotEl.classList.toggle('is-set', !!fixedEnd);

  fixedPointsListEl.hidden = !fixedStart && !fixedEnd;
}


function setFixedPoint(role, lngLat) {
  const point = { lngLat, marker: null };
  const marker = new maplibregl.Marker({
    color: role === 'start' ? START_COLOR : END_COLOR,
    draggable: true,
  }).setLngLat(lngLat).addTo(map);

  marker.on('drag', () => {
    const ll = marker.getLngLat();
    point.lngLat = [ll.lng, ll.lat];
    renderFixedPoints();
    invalidateRoute();
    updateGenerateButton();
    updateRouteHint();
  });

  point.marker = marker;

  if (role === 'start') {
    if (fixedStart?.marker) fixedStart.marker.remove();
    fixedStart = point;
  } else {
    if (fixedEnd?.marker) fixedEnd.marker.remove();
    fixedEnd = point;
  }
}

function dropFixedPointAtCenter(role) {
  if (routeLoading) return;

  const center = map.getCenter();
  setFixedPoint(role, [center.lng, center.lat]);

  renderFixedPoints();
  invalidateRoute();
  updateGenerateButton();
  updateRouteHint();
}

placeStartBtn.addEventListener('click', () => dropFixedPointAtCenter('start'));
placeEndBtn.addEventListener('click', () => dropFixedPointAtCenter('end'));

function clearFixedPoints() {
  if (fixedStart?.marker) fixedStart.marker.remove();
  if (fixedEnd?.marker) fixedEnd.marker.remove();
  fixedStart = null;
  fixedEnd = null;
  renderFixedPoints();
}

// ── Mode switch ──────────────────────────────────────────────────────────
function setRouteMode(mode) {
  if (mode === routeMode || routeLoading) return;

  // Reset whichever mode's state we're leaving so the two flows never mix
  clearFixedPoints();
  waypoints.forEach(wp => { if (wp.marker) wp.marker.remove(); });
  waypoints = [];
  renderWaypointRows();
  clearRouteLine();
  removeWaypointPreview();
  resetRouteStats();
  routeErrorEl.hidden = true;

  routeMode = mode;

  modeSwitchEl.querySelectorAll('.mode-option').forEach(btn => {
    const active = btn.dataset.mode === mode;
    btn.classList.toggle('is-active', active);
    btn.setAttribute('aria-selected', active ? 'true' : 'false');
  });

  modeFixedEl.hidden = mode !== 'fixed';
  modeMultiEl.hidden = mode !== 'multi';

  updateGenerateButton();
  updateAddButton();
  updateRouteHint();
}

modeSwitchEl.addEventListener('click', (event) => {
  const btn = event.target.closest('.mode-option');
  if (btn) setRouteMode(btn.dataset.mode);
});


// ========================================================================
// WAYPOINT LIST — PANEL ROWS + MAP MARKERS
// ========================================================================

function renderWaypointRows() {
  routePointsEl.innerHTML = '';

  waypoints.forEach((wp, i) => {
    const role = roleForIndex(i, waypoints.length);
    const isVia = role === 'via';

    const row = document.createElement('div');
    row.className = `route-point-row${isVia ? ' is-via' : ''}`;
    row.dataset.index = String(i);

    if (isVia) {
      row.addEventListener('pointerdown', (event) => {
        if (event.target.closest('.point-remove')) return; // let the remove button work normally
        beginWaypointDrag(event, i, row);
      });
    }

    const dot = document.createElement('span');
    dot.className = `point-dot point-dot-${role} is-set`;

    const label = document.createElement('span');
    label.className = 'point-label';
    label.textContent = labelForIndex(i, waypoints.length);

    const coords = document.createElement('span');
    coords.className = 'point-coords is-set';
    coords.textContent = formatCoords(wp.lngLat[0], wp.lngLat[1]);

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'point-remove';
    remove.title = 'Remove waypoint';
    remove.textContent = '×';
    remove.addEventListener('click', () => { if (!routeLoading) removeWaypoint(i); });

    row.append(dot, label, coords, remove);
    routePointsEl.appendChild(row);
  });
}


// ========================================================================
// WAYPOINT LIST — DRAG TO REORDER (via points only)
// ========================================================================
// Start and end never move here — only the via rows in between can be
// dragged past each other. Dropping a via just changes which array slot it
// occupies (and therefore its number and where the route visits it); its
// actual pinned location on the map never changes.

let waypointDrag = null;
let dropIndicatorEl = null;

function getDropIndicatorEl() {
  if (!dropIndicatorEl) {
    dropIndicatorEl = document.createElement('div');
    dropIndicatorEl.className = 'waypoint-drop-indicator';
    document.body.appendChild(dropIndicatorEl);
  }
  return dropIndicatorEl;
}

function beginWaypointDrag(event, index, rowEl) {
  if (routeLoading) return;
  event.preventDefault();

  const viaRows = [];
  routePointsEl.querySelectorAll('.route-point-row.is-via').forEach((el) => {
    const rect = el.getBoundingClientRect();
    viaRows.push({ index: Number(el.dataset.index), el, rect });
  });

  waypointDrag = {
    fromIndex: index,
    dropIndex: index,
    rowEl,
    pointerId: event.pointerId,
    otherRows: viaRows.filter(r => r.index !== index), // sorted top-to-bottom, dragged one excluded
  };

  rowEl.classList.add('is-dragging');
  rowEl.setPointerCapture(event.pointerId);
  document.body.style.cursor = 'grabbing';

  rowEl.addEventListener('pointermove', onWaypointDragMove);
  rowEl.addEventListener('pointerup', onWaypointDragEnd);
  rowEl.addEventListener('pointercancel', onWaypointDragEnd);
}

function onWaypointDragMove(event) {
  if (!waypointDrag) return;

  const { otherRows } = waypointDrag;

  // Rank (1-based) among the OTHER via rows whose midpoint the pointer has
  // passed — this maps directly onto the array index the dragged item
  // should land on once it's spliced out and back in.
  let dropRank = 1;
  otherRows.forEach(({ rect }) => {
    if (event.clientY > rect.top + rect.height / 2) dropRank++;
  });

  waypointDrag.dropIndex = Math.max(1, Math.min(waypoints.length - 2, dropRank));

  const indicator = getDropIndicatorEl();

  if (!otherRows.length) {
    indicator.style.display = 'none';
    return;
  }

  // dropRank is 1-based against otherRows — dropRank-1 is the row we land
  // just above; past the last one, the line sits below it instead.
  const targetRow = otherRows[Math.min(dropRank - 1, otherRows.length - 1)];
  const showAfter = dropRank - 1 >= otherRows.length;
  const lineY = showAfter ? targetRow.rect.bottom + 4 : targetRow.rect.top - 4;

  indicator.style.display = 'block';
  indicator.style.top = `${lineY}px`;
  indicator.style.left = `${targetRow.rect.left}px`;
  indicator.style.width = `${targetRow.rect.width}px`;
}

function onWaypointDragEnd() {
  if (!waypointDrag) return;

  const { fromIndex, dropIndex, rowEl, pointerId } = waypointDrag;

  rowEl.releasePointerCapture(pointerId);
  rowEl.removeEventListener('pointermove', onWaypointDragMove);
  rowEl.removeEventListener('pointerup', onWaypointDragEnd);
  rowEl.removeEventListener('pointercancel', onWaypointDragEnd);
  if (dropIndicatorEl) dropIndicatorEl.style.display = 'none';
  document.body.style.cursor = '';

  waypointDrag = null;

  if (dropIndex !== fromIndex) {
    const [item] = waypoints.splice(fromIndex, 1);
    waypoints.splice(dropIndex, 0, item);

    rebuildMarkers();
    invalidateRoute();
    updateRouteHint();
  }

  renderWaypointRows();
}

// this function is to change the color of pin when its role is changed
// might need to rethink how to do this if where to scale into more waypoints as this is not very efficient
// but works for right now so I will keep it this way
function rebuildMarkers() {
  waypoints.forEach((wp, i) => {
    const role = roleForIndex(i, waypoints.length);
    // Via points also carry a sequence number (1-based, matches the panel's
    // "Waypoint N" label). The signature includes it so that renumbering a
    // via point after a delete still triggers a badge refresh, even though
    // its role ('via') hasn't changed.
    const signature = role === 'via' ? `via-${i}` : role;

    // Signature unchanged — leave this marker exactly where it is. This is
    // what stops an existing pin from ever appearing to "jump" or swap
    // places when a new waypoint is added or removed elsewhere.
    if (wp.marker && wp.signature === signature) return;

    if (wp.marker) wp.marker.remove();

    const marker = new maplibregl.Marker({ color: colorForRole(role), draggable: true })
      .setLngLat(wp.lngLat)
      .addTo(map);

    if (role === 'via') {
      const badge = document.createElement('div');
      badge.className = 'rw-marker-badge';
      badge.textContent = String(i);
      marker.getElement().appendChild(badge);
    }

    marker.on('drag', () => {
      const ll = marker.getLngLat();
      wp.lngLat = [ll.lng, ll.lat];
      renderWaypointRows();
      invalidateRoute();
    });

    wp.marker = marker;
    wp.signature = signature;
  });
}

// add and remove waypoint functions 
function addWaypointAtCenter() {
  if (waypoints.length >= MAX_WAYPOINTS) return;
  const center = map.getCenter();
  const newPoint = { lngLat: [center.lng, center.lat], marker: null };

  if (waypoints.length < 2) {
    // No end pin yet — the first point becomes start, the second becomes end.
    waypoints.push(newPoint);
  } else {
    // An end pin already exists: insert the new point as a via stop just
    // before it, so the existing end never moves or changes role.
    waypoints.splice(waypoints.length - 1, 0, newPoint);
  }

  rebuildMarkers();
  renderWaypointRows();
  invalidateRoute();
  updateGenerateButton();
  updateAddButton();
  updateRouteHint();
}

function removeWaypoint(index) {
  const wp = waypoints[index];
  if (wp.marker) wp.marker.remove();
  waypoints.splice(index, 1);

  rebuildMarkers();
  renderWaypointRows();
  invalidateRoute();
  updateGenerateButton();
  updateAddButton();
  updateRouteHint();
}


// ========================================================================
// RESET ROUTE
// ========================================================================

function resetRoute() {
  if (routeMode === 'fixed') {
    clearFixedPoints();
  } else {
    waypoints.forEach(wp => { if (wp.marker) wp.marker.remove(); });
    waypoints = [];
    renderWaypointRows();
  }

  clearRouteLine();
  removeWaypointPreview();
  resetRouteStats();
  routeErrorEl.hidden = true;
  updateGenerateButton();
  updateAddButton();
  updateRouteHint();
}


// ========================================================================
// DRAW ROUTE
// ========================================================================

// ========================================================================
// ROUTE LINE — DRAWN PROGRESSIVELY, START TO END
// ========================================================================
// Rather than handing MapLibre the whole geometry in one setData() call, we
// walk along the actual route coordinates over ~2 seconds so the line looks
// like it's being traced out live, the same way the backend traced it.

let routeDrawAnimationId = null;

function ensureRouteLayers(initialGeojson) {
  if (map.getSource('route')) {
    map.getSource('route').setData(initialGeojson);
    return;
  }

  map.addSource('route', { type: 'geojson', data: initialGeojson });

  // Route glow
  map.addLayer({
    id: 'route-glow',
    type: 'line',
    source: 'route',
    layout: { 'line-join': 'round', 'line-cap': 'round' },
    paint: {
      'line-color': END_COLOR,
      'line-width': 9,
      'line-blur': 6,
      'line-opacity': 0.35,
    },
  });

  // Main route line
  map.addLayer({
    id: 'route-line',
    type: 'line',
    source: 'route',
    layout: { 'line-join': 'round', 'line-cap': 'round' },
    paint: {
      'line-color': END_COLOR,
      'line-width': 3.5,
      'line-opacity': 0.95,
    },
  });
}

function setRouteLineCoordinates(coordinates) {
  ensureRouteLayers({
    type: 'Feature',
    geometry: { type: 'LineString', coordinates },
  });
}

// Good enough for pacing an animation — not trying to be survey-accurate.
function haversineMeters([lng1, lat1], [lng2, lat2]) {
  const R = 6371000;
  const toRad = (deg) => (deg * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLng = toRad(lng2 - lng1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

function animateRouteLine(coordinates, durationMs) {
  if (routeDrawAnimationId !== null) {
    cancelAnimationFrame(routeDrawAnimationId);
    routeDrawAnimationId = null;
  }

  if (coordinates.length < 2) {
    setRouteLineCoordinates(coordinates);
    return;
  }

  // Cumulative distance along the route so the line advances at a constant
  // speed regardless of how far apart the backend's points happen to be.
  const cumulative = [0];
  for (let i = 1; i < coordinates.length; i++) {
    cumulative.push(cumulative[i - 1] + haversineMeters(coordinates[i - 1], coordinates[i]));
  }
  const totalDistance = cumulative[cumulative.length - 1];

  if (!totalDistance) {
    setRouteLineCoordinates(coordinates);
    return;
  }

  setRouteLineCoordinates([coordinates[0]]);
  const startTime = performance.now();

  function step(now) {
    const t = Math.min(1, (now - startTime) / durationMs);
    const targetDistance = t * totalDistance;

    let segIndex = 1;
    while (segIndex < cumulative.length - 1 && cumulative[segIndex] < targetDistance) {
      segIndex++;
    }

    const segStartDist = cumulative[segIndex - 1];
    const segEndDist = cumulative[segIndex];
    const segFraction = segEndDist > segStartDist
      ? (targetDistance - segStartDist) / (segEndDist - segStartDist)
      : 1;

    const [lngA, latA] = coordinates[segIndex - 1];
    const [lngB, latB] = coordinates[segIndex];
    const currentPoint = [
      lngA + (lngB - lngA) * segFraction,
      latA + (latB - latA) * segFraction,
    ];

    setRouteLineCoordinates([...coordinates.slice(0, segIndex), currentPoint]);

    if (t < 1) {
      routeDrawAnimationId = requestAnimationFrame(step);
    } else {
      routeDrawAnimationId = null;
      setRouteLineCoordinates(coordinates); // exact final geometry, no rounding drift
    }
  }

  routeDrawAnimationId = requestAnimationFrame(step);
}


function drawRoute(routeFeature) {
  currentRouteFeature = routeFeature;

  // The real backend route replaces the straight-line preview.
  removeWaypointPreview();

if (exportGPXBtn) {
  exportGPXBtn.disabled = false;
  exportGPXBtn.classList.add('is-ready');
  exportGPXBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

  const routeCoordinates = routeFeature.geometry.coordinates;

  // Trace the line in from start to end over ~2s instead of snapping it in.
  animateRouteLine(routeCoordinates, 2000);

  // Zoom map to generated route
  const bounds = routeCoordinates.reduce(
      (existingBounds, coordinate) =>
        existingBounds.extend(coordinate),

      new maplibregl.LngLatBounds(
        routeCoordinates[0],
        routeCoordinates[0]
      )
    );

  map.fitBounds(bounds,{
      padding: 80, duration: 800
    }
  );
}


// ========================================================================
// ADD WAYPOINT
// ========================================================================

addWaypointBtn.addEventListener('click',
  () => {
    if (!routeLoading) {
      addWaypointAtCenter();
    }
  }
);


// ========================================================================
// GENERATE ROUTE
// ========================================================================

function getRoutePoints() {
  if (routeMode === 'fixed') {
    if (!fixedStart || !fixedEnd) return null;
    return { a: fixedStart.lngLat, b: fixedEnd.lngLat, via: [] };
  }
  if (waypoints.length < 2) return null;
  return {
    a: waypoints[0].lngLat,
    b: waypoints[waypoints.length - 1].lngLat,
    via: waypoints.slice(1, -1).map(wp => wp.lngLat),
  };
}

generateRouteBtn.addEventListener('click', async () => {
    const points = getRoutePoints();
    if (!points || routeLoading) {
      return;
    }

    const pointA = points.a;
    const pointB = points.b;
    const viaPoints = points.via;

    // --------------------------------------------------
    // SHOW LOADING SCREEN
    // --------------------------------------------------

    showLoadingScreen();
    routeLoading =true;
    updateGenerateButton();
    updateAddButton();
    generateRouteBtn.textContent ='Calculating…';
    clearRouteBtn.disabled = true;
    if (exportGPXBtn) {
      exportGPXBtn.disabled =true;
      exportGPXBtn.classList.remove('is-ready');
    }

    waypoints.forEach(wp => { if (wp.marker) wp.marker.setDraggable(false); });
    if (fixedStart?.marker) fixedStart.marker.setDraggable(false);
    if (fixedEnd?.marker) fixedEnd.marker.setDraggable(false);
    placeStartBtn.disabled = true;
    placeEndBtn.disabled = true;

    routeErrorEl.hidden = true;
    resetRouteStats();
    routeHintEl.textContent ='Crunching terrain data — this can take a little while for longer routes.';


    // --------------------------------------------------
    // TIMEOUT
    // --------------------------------------------------

    const controller =new AbortController();

    // 120 second timeout
    const timeoutId =setTimeout(
        () =>
          controller.abort(), 120000
      );

    try {

      // ------------------------------------------------
      // SEND ROUTE REQUEST TO LAMBDA
      // ------------------------------------------------

      const response =
        await fetch(
          ROUTE_API_URL,
          {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body:
              JSON.stringify({a: pointA, b: pointB, via: viaPoints}),
            signal:controller.signal
          }
        );

      clearTimeout(timeoutId);


      // ------------------------------------------------
      // READ RESPONSE
      // ------------------------------------------------

      let data;

      try {
        data = await response.json();
      } catch (_) {
        throw new Error(
          `Server returned an unreadable response (status ${response.status}).`
        );
      }


      // ------------------------------------------------
      // CHECK FOR SERVER ERROR
      // ------------------------------------------------

      if (!response.ok ||!data.ok) {
        throw new Error(data.error ||`Route request failed (status ${response.status}).`);
      }

      // ------------------------------------------------
      // DRAW ROUTE
      // ------------------------------------------------
      drawRoute(data.route);
      currentRouteGPX = data.gpx || null;

      // ------------------------------------------------
      // DISPLAY ROUTE STATISTICS
      // ------------------------------------------------

      statDistanceEl.textContent =`${data.distance_km.toFixed(2)} km`;
      statTimeEl.textContent = formatDuration(data.estimated_hours);
      statClimbEl.textContent = `${Math.round(data.climb_m)} m`;
      routeStatsEl.hidden = false;
      routeHintEl.textContent = 'Route generated. Drag any pin to plan a new route.';

    } catch (error) {
      clearTimeout(timeoutId);

      const message =error.name ==='AbortError'
          ? 'The route request timed out. Try waypoints that are closer together.'
          : friendlyRouteError(error.message ||'Something went wrong generating the route.');

      routeErrorEl.textContent =message;
      routeErrorEl.hidden =false;
      routeHintEl.textContent = 'Ready — hit Generate Route to try again.';
      console.error('Route generation error:',error);

    } finally {

      // ------------------------------------------------
      // ALWAYS REMOVE LOADING SCREEN
      // ------------------------------------------------
      hideLoadingScreen();
      routeLoading =false;

      updateGenerateButton();
      updateAddButton();

      generateRouteBtn.textContent ='Generate Route';

      clearRouteBtn.disabled = false;

      waypoints.forEach(wp => { if (wp.marker) wp.marker.setDraggable(true); });
      if (fixedStart?.marker) fixedStart.marker.setDraggable(true);
      if (fixedEnd?.marker) fixedEnd.marker.setDraggable(true);
      placeStartBtn.disabled = false;
      placeEndBtn.disabled = false;
    }
  }
);

// ========================================================================
// EXPORT GPX
// ========================================================================
// Downloads the exact GPX file the backend already generated for the
// current route (route_to_gpx() in route_generator.py

function downloadGPX() {
  if (!currentRouteGPX) {
    routeErrorEl.textContent = 'Generate a route before exporting GPX.';
    routeErrorEl.hidden = false;
    return;
  }

  const blob = new Blob([currentRouteGPX], { type: 'application/gpx+xml' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');

  link.href = url;
  link.download = 'ridgewalker-route.gpx';
  link.click();

  URL.revokeObjectURL(url);
  exportGPXBtn.classList.remove('is-ready');
}
if (exportGPXBtn) {
  exportGPXBtn.addEventListener('click', downloadGPX);
}


// ========================================================================
// CLEAR ROUTE
// ========================================================================

clearRouteBtn.addEventListener('click',() => {
    if (routeLoading) {
      return;}
    resetRoute();
  }
);

// ========================================================================
// LOADING SCREEN
// ========================================================================
function showLoadingScreen() {
  const loadingScreen = document.getElementById('loading-screen');

  if (loadingScreen) {
    loadingScreen.style.display = 'flex';
  }
}

function hideLoadingScreen() {
  const loadingScreen = document.getElementById('loading-screen');

  if (loadingScreen) {
    loadingScreen.style.display ='none';
  }
}

// ========================================================================
// INITIAL UI STATE
// ========================================================================

renderFixedPoints();
updateGenerateButton();
updateAddButton();
updateRouteHint();


// ========================================================================
// NEW ZEALAND PLACE SEARCH
// ========================================================================
// Type-ahead search for NZ places, restricted to New Zealand only.
//
// Backend: Photon (photon.komoot.io) — an open-source OpenStreetMap
// geocoder purpose-built for search-as-you-type. Nominatim is the more
// obvious choice but its usage policy explicitly forbids client-side
// autocomplete, so it is not usable here.
//
// "Only New Zealand" is enforced twice:
//   1. countrycode=nz on the request (hard server-side filter).
//   2. isInNewZealand() on every result that comes back, so nothing
//      foreign can slip through if the public instance ever ignores (1).
//
// Everything lives inside an IIFE so none of these names can collide with
// the route planner's globals above.

(() => {

  // ── Config ─────────────────────────────────────────────────────────────
  const GEOCODER_URL       = 'https://photon.komoot.io/api';
  const SEARCH_MIN_CHARS   = 2;
  const SEARCH_DEBOUNCE_MS = 350;   // also keeps us well under 1 req/sec
  const SEARCH_SHOW_LIMIT  = 7;     // results displayed
  const SEARCH_FETCH_LIMIT = 12;    // results requested (spare covers filtering)
  const SEARCH_CACHE_MAX   = 60;
  const SEARCH_MARKER_MS   = 12000; // how long the "you are here" ring lingers
  const MOBILE_BREAKPOINT  = 768;   // matches the CSS bottom-sheet breakpoint

  // Rough zoom to settle on when a result has no extent of its own.
  const SEARCH_ZOOM_BY_KIND = {
    country: 5, state: 6, region: 6, county: 8, island: 9, archipelago: 8,
    city: 11, borough: 12, town: 12, village: 13, hamlet: 14,
    suburb: 13, neighbourhood: 14, quarter: 14, locality: 14,
    farm: 14, isolated_dwelling: 15,
    national_park: 9, protected_area: 10, nature_reserve: 11, forest: 10,
    peak: 13, volcano: 13, ridge: 12, valley: 12, glacier: 12, saddle: 14,
    lake: 11, bay: 11, river: 10, stream: 12, beach: 13, cape: 13,
    hut: 15, alpine_hut: 15, wilderness_hut: 15, camp_site: 15,
    trailhead: 15, viewpoint: 15, parking: 16, information: 15,
  };

  // ── Elements ───────────────────────────────────────────────────────────
  const searchEl        = document.getElementById('search');
  const searchShellEl   = searchEl && searchEl.querySelector('.search-shell');
  const searchToggleEl  = document.getElementById('search-toggle');
  const searchInputEl   = document.getElementById('search-input');
  const searchClearEl   = document.getElementById('search-clear');
  const searchSpinnerEl = document.getElementById('search-spinner');
  const searchResultsEl = document.getElementById('search-results');

  // If the markup isn't on the page, do nothing rather than throw — the
  // route planner above must keep working regardless.
  if (!searchEl || !searchShellEl || !searchToggleEl || !searchInputEl ||
      !searchClearEl || !searchSpinnerEl || !searchResultsEl) {
    return;
  }

  // ── State ──────────────────────────────────────────────────────────────
  let searchOpen        = false;
  let searchDebounceId  = null;
  let searchController  = null;   // AbortController for the in-flight request
  let searchRequestSeq  = 0;      // guards against out-of-order responses
  let searchFeatures    = [];
  let searchActiveIndex = -1;
  let searchMarker      = null;
  let searchMarkerTimer = null;
  const searchCache     = new Map();


  // ========================================================================
  // NZ-ONLY GUARD
  // ========================================================================
  // Second line of defence behind countrycode=nz. The longitude test is
  // split because NZ straddles the antimeridian — the Chatham Islands sit
  // at roughly 176.5° WEST, i.e. a negative longitude.

  function isInNewZealand(feature) {
    const code = feature?.properties?.countrycode;
    if (code) return code.toUpperCase() === 'NZ';

    const coords = feature?.geometry?.coordinates;
    if (!Array.isArray(coords)) return false;

    const [lon, lat] = coords;
    if (typeof lon !== 'number' || typeof lat !== 'number') return false;
    if (lat > -33.0 || lat < -52.7) return false;

    return (lon >= 165.5 && lon <= 180) || (lon >= -180 && lon <= -175.0);
  }


  // ========================================================================
  // RESULT LABELLING
  // ========================================================================

  function primaryLabel(properties) {
    if (properties.name) return properties.name;
    if (properties.street) {
      return properties.housenumber
        ? `${properties.housenumber} ${properties.street}`
        : properties.street;
    }
    return properties.city || properties.county || properties.state || '';
  }

  function secondaryLabel(properties) {
    const name = properties.name;
    const parts = [];

    if (name && properties.street) {
      parts.push(properties.housenumber
        ? `${properties.housenumber} ${properties.street}`
        : properties.street);
    }

    for (const field of ['district', 'city', 'county', 'state']) {
      const value = properties[field];
      if (value && value !== name && !parts.includes(value)) {
        parts.push(value);
      }
    }

    return parts.slice(0, 3).join(', ');
  }

  function kindLabel(properties) {
    const value = properties.osm_value || properties.osm_key || '';
    if (!value || value === 'yes') return '';
    return value.replace(/_/g, ' ');
  }


  // ========================================================================
  // GEOCODER REQUEST
  // ========================================================================

  async function fetchPlaces(query, sequence) {
    const center = map.getCenter();

    const url = new URL(GEOCODER_URL);
    url.searchParams.set('q', query);
    url.searchParams.set('lang', 'en');
    url.searchParams.set('limit', String(SEARCH_FETCH_LIMIT));
    url.searchParams.set('countrycode', 'nz');

    // Bias towards whatever part of the country is currently on screen,
    // without ignoring prominence — "Wellington" still finds Wellington
    // even while you're zoomed into Fiordland.
    url.searchParams.set('lat', center.lat.toFixed(4));
    url.searchParams.set('lon', center.lng.toFixed(4));
    url.searchParams.set('zoom', String(Math.round(map.getZoom())));

    if (searchController) searchController.abort();
    searchController = new AbortController();

    const response = await fetch(url, { signal: searchController.signal });
    if (!response.ok) throw new Error(`Geocoder returned ${response.status}`);

    const data = await response.json();

    // A slower earlier request may land after a newer one — drop it.
    if (sequence !== searchRequestSeq) return null;

    const seen = new Set();
    const features = [];

    for (const feature of (data.features || [])) {
      if (!isInNewZealand(feature)) continue;

      const properties = feature.properties || {};
      const label = primaryLabel(properties);
      if (!label) continue;

      const [lon, lat] = feature.geometry.coordinates;
      const key = `${label}|${lon.toFixed(4)},${lat.toFixed(4)}`;
      if (seen.has(key)) continue;

      seen.add(key);
      features.push(feature);
      if (features.length >= SEARCH_SHOW_LIMIT) break;
    }

    return features;
  }


  // ========================================================================
  // RESULTS LIST
  // ========================================================================

  function setStatus(message, isError) {
    searchFeatures = [];
    searchActiveIndex = -1;
    searchResultsEl.textContent = '';

    const status = document.createElement('div');
    status.className = `search-status${isError ? ' is-error' : ''}`;
    status.textContent = message;

    searchResultsEl.appendChild(status);
    showResults();
  }

  function renderResults(features) {
    searchFeatures = features;
    searchActiveIndex = -1;
    searchResultsEl.textContent = '';

    if (!features.length) {
      setStatus('No matches in New Zealand. Try a different spelling.', false);
      return;
    }

    features.forEach((feature, index) => {
      const properties = feature.properties || {};

      const option = document.createElement('button');
      option.type = 'button';
      option.className = 'search-result';
      option.id = `search-result-${index}`;
      option.setAttribute('role', 'option');
      option.setAttribute('aria-selected', 'false');
      option.tabIndex = -1;

      const name = document.createElement('span');
      name.className = 'search-result-name';
      name.textContent = primaryLabel(properties);

      const kind = kindLabel(properties);
      if (kind) {
        const kindEl = document.createElement('span');
        kindEl.className = 'search-result-kind';
        kindEl.textContent = kind;
        name.appendChild(kindEl);
      }

      option.appendChild(name);

      const meta = secondaryLabel(properties);
      if (meta) {
        const metaEl = document.createElement('span');
        metaEl.className = 'search-result-meta';
        metaEl.textContent = meta;
        option.appendChild(metaEl);
      }

      // pointerdown fires before the input's blur, so the selection isn't
      // lost to the outside-click handler.
      option.addEventListener('pointerdown', (event) => {
        event.preventDefault();
        selectResult(index);
      });

      option.addEventListener('mousemove', () => setActiveIndex(index, false));

      searchResultsEl.appendChild(option);
    });

    const credit = document.createElement('div');
    credit.className = 'search-credit';
    credit.textContent = 'Place data © OpenStreetMap contributors';
    searchResultsEl.appendChild(credit);

    showResults();
  }

  function showResults() {
    searchResultsEl.hidden = false;
    searchInputEl.setAttribute('aria-expanded', 'true');
    searchToggleEl.setAttribute('aria-expanded', 'true');
  }

  function hideResults() {
    searchResultsEl.hidden = true;
    searchInputEl.setAttribute('aria-expanded', 'false');
    searchInputEl.removeAttribute('aria-activedescendant');
    searchActiveIndex = -1;
  }

  function setActiveIndex(index, scrollIntoView) {
    const options = searchResultsEl.querySelectorAll('.search-result');
    if (!options.length) return;

    searchActiveIndex = index;

    options.forEach((option, i) => {
      const active = i === index;
      option.classList.toggle('is-active', active);
      option.setAttribute('aria-selected', active ? 'true' : 'false');
    });

    if (index >= 0) {
      searchInputEl.setAttribute('aria-activedescendant', `search-result-${index}`);
      if (scrollIntoView) {
        options[index].scrollIntoView({ block: 'nearest' });
      }
    } else {
      searchInputEl.removeAttribute('aria-activedescendant');
    }
  }


  // ========================================================================
  // FLY TO A RESULT
  // ========================================================================

  // Nudges the target into the part of the map that isn't covered by the
  // side panel (desktop) or the bottom sheet (mobile). An offset is used
  // rather than camera padding because padding passed to flyTo/fitBounds
  // sticks to the map and would then affect the route's own fitBounds.
  function viewOffset() {
    if (window.innerWidth <= MOBILE_BREAKPOINT) {
      return [0, -Math.round(window.innerHeight * 0.16)];
    }
    return [Math.round(Math.min(200, window.innerWidth * 0.18)), 0];
  }

  function zoomForFeature(properties) {
    if (properties.housenumber) return 17;
    if (properties.street) return 16;

    const kind = properties.osm_value || properties.osm_key;
    return SEARCH_ZOOM_BY_KIND[kind] ?? 13;
  }

  function showSearchMarker(lngLat) {
    clearSearchMarker();

    const element = document.createElement('div');
    element.className = 'search-marker';

    searchMarker = new maplibregl.Marker({ element, anchor: 'center' })
      .setLngLat(lngLat)
      .addTo(map);

    searchMarkerTimer = setTimeout(clearSearchMarker, SEARCH_MARKER_MS);
  }

  function clearSearchMarker() {
    if (searchMarkerTimer) {
      clearTimeout(searchMarkerTimer);
      searchMarkerTimer = null;
    }
    if (searchMarker) {
      searchMarker.remove();
      searchMarker = null;
    }
  }

  function selectResult(index) {
    const feature = searchFeatures[index];
    if (!feature) return;

    const properties = feature.properties || {};
    const [lon, lat] = feature.geometry.coordinates;
    const offset = viewOffset();

    let center = [lon, lat];
    let zoom = zoomForFeature(properties);

    // Photon's extent is [minLon, maxLat, maxLon, minLat] — west, north,
    // east, south. cameraForBounds just computes a camera, it doesn't move
    // the map or leave padding behind.
    const extent = properties.extent;
    if (Array.isArray(extent) && extent.length === 4) {
      const bounds = new maplibregl.LngLatBounds(
        [extent[0], extent[3]],
        [extent[2], extent[1]]
      );
      const camera = map.cameraForBounds(bounds, { padding: 60 });
      if (camera) {
        center = camera.center;
        zoom = Math.min(camera.zoom, 15);
      }
    }

    map.flyTo({
      center,
      zoom: Math.max(map.getMinZoom(), Math.min(zoom, map.getMaxZoom())),
      offset,
      duration: 1200,
    });

    showSearchMarker([lon, lat]);

    searchInputEl.value = primaryLabel(properties);
    searchClearEl.hidden = false;
    hideResults();

    // On a phone, get the keyboard and the pill out of the way so the
    // place you just searched for is actually visible.
    if (window.innerWidth <= MOBILE_BREAKPOINT) {
      searchInputEl.blur();
      closeSearch();
    }
  }


  // ========================================================================
  // QUERY HANDLING
  // ========================================================================

  function cacheGet(key) {
    return searchCache.get(key);
  }

  function cacheSet(key, value) {
    searchCache.set(key, value);
    if (searchCache.size > SEARCH_CACHE_MAX) {
      searchCache.delete(searchCache.keys().next().value);
    }
  }

  async function runQuery(query) {
    const key = query.trim().toLowerCase();

    const cached = cacheGet(key);
    if (cached) {
      renderResults(cached);
      return;
    }

    const sequence = ++searchRequestSeq;
    searchSpinnerEl.hidden = false;

    try {
      const features = await fetchPlaces(query, sequence);
      if (features === null) return;             // superseded by a newer query

      cacheSet(key, features);
      renderResults(features);

    } catch (error) {
      if (error.name === 'AbortError') return;   // replaced mid-flight, not a failure
      console.error('Place search failed:', error);
      setStatus('Search is unavailable right now. Check your connection and try again.', true);

    } finally {
      if (sequence === searchRequestSeq) {
        searchSpinnerEl.hidden = true;
      }
    }
  }

  function scheduleQuery(immediate) {
    if (searchDebounceId) {
      clearTimeout(searchDebounceId);
      searchDebounceId = null;
    }

    const query = searchInputEl.value.trim();
    searchClearEl.hidden = query.length === 0;

    if (query.length < SEARCH_MIN_CHARS) {
      if (searchController) searchController.abort();
      searchSpinnerEl.hidden = true;
      hideResults();
      return;
    }

    if (immediate) {
      runQuery(query);
      return;
    }

    searchDebounceId = setTimeout(() => {
      searchDebounceId = null;
      runQuery(query);
    }, SEARCH_DEBOUNCE_MS);
  }


  // ========================================================================
  // OPEN / CLOSE
  // ========================================================================

  function openSearch() {
    if (searchOpen) return;
    searchOpen = true;

    searchEl.classList.add('is-open');
    searchToggleEl.setAttribute('aria-expanded', 'true');
    searchInputEl.tabIndex = 0;
    searchClearEl.tabIndex = 0;
    searchInputEl.focus();
  }

  function closeSearch() {
    if (!searchOpen) return;
    searchOpen = false;

    searchEl.classList.remove('is-open');
    searchToggleEl.setAttribute('aria-expanded', 'false');
    searchInputEl.tabIndex = -1;
    searchClearEl.tabIndex = -1;
    searchInputEl.blur();
    hideResults();
  }

  function clearSearch() {
    if (searchDebounceId) {
      clearTimeout(searchDebounceId);
      searchDebounceId = null;
    }
    if (searchController) searchController.abort();

    searchInputEl.value = '';
    searchClearEl.hidden = true;
    searchSpinnerEl.hidden = true;
    clearSearchMarker();
    hideResults();
  }


  // ========================================================================
  // EVENTS
  // ========================================================================

  searchToggleEl.addEventListener('click', () => {
    if (!searchOpen) {
      openSearch();
    } else if (searchInputEl.value.trim()) {
      searchInputEl.focus();
      scheduleQuery(true);
    } else {
      closeSearch();
    }
  });

  searchInputEl.addEventListener('input', () => scheduleQuery(false));

  searchInputEl.addEventListener('focus', () => {
    if (searchFeatures.length && searchInputEl.value.trim()) showResults();
  });

  searchInputEl.addEventListener('keydown', (event) => {
    const count = searchFeatures.length;

    if (event.key === 'ArrowDown' && count) {
      event.preventDefault();
      setActiveIndex((searchActiveIndex + 1) % count, true);

    } else if (event.key === 'ArrowUp' && count) {
      event.preventDefault();
      setActiveIndex((searchActiveIndex - 1 + count) % count, true);

    } else if (event.key === 'Enter') {
      event.preventDefault();
      if (count) {
        selectResult(searchActiveIndex >= 0 ? searchActiveIndex : 0);
      } else {
        scheduleQuery(true);
      }

    } else if (event.key === 'Escape') {
      event.preventDefault();
      if (!searchResultsEl.hidden) {
        hideResults();
      } else {
        closeSearch();
      }
    }
  });

  searchClearEl.addEventListener('click', () => {
    clearSearch();
    searchInputEl.focus();
  });

  // Collapse when the map (or anything else) is clicked
  document.addEventListener('pointerdown', (event) => {
    if (searchOpen && !searchEl.contains(event.target)) closeSearch();
  });

})();
