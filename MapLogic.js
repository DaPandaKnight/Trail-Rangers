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

  transformRequest:
    transformRequest
});


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


  // ── Zoom buttons ─────────────────────────────────────────────────────────
  document.getElementById('zoom-in').addEventListener('click',  () => map.zoomIn());
  document.getElementById('zoom-out').addEventListener('click', () => map.zoomOut());

  // ── Route planner ────────────────────────────────────────────────────────
  // Add the ability to have multiple waypoints, waypoints will now be stored in an array
  // Each point will have a role assigned to it (start, end, via) and the role will be assigned based
  // on the position of the waypoint on the array. this will make adding and removing waypoints more seamlessly
  // Add waypoint button will add waypoints on the map and the points will be displayed on the side panel 
  // Each pin owns its own drag gesture
  // (MapLibre disables map panning while a marker drag is in progress), so
  // this sidesteps any conflict with the map's own click-and-drag panning.

  const START_COLOR = '#e2660a'; // matches --accent
  const END_COLOR   = '#178f66'; // matches --accent2
  const VIA_COLOR   = '#c9832a'; // matches --via
  const PREVIEW_LINE_COLOR = '#7a7266'; // matches --muted — marks it as a straight preview, not a real route

  const MAX_WAYPOINTS = 5;

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

  // main array to store all waypoints 
  // instead of having a fixed start and end point, the waypoints will be assigned a role (start, end, via)
  // the roles will be assigned based on the point's position in the array 
  let waypoints = [];  
  let routeLoading = false;
  let currentRouteFeature = null;
  let currentRouteGPX = null;

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
    return `Via ${i}`;
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

  if (waypoints.length === 0) {
    routeHintEl.textContent = 'Add at least two waypoints to plan a route';
  } else if (waypoints.length === 1) {
    routeHintEl.textContent = 'Add an end waypoint, then drag pins to fine-tune';
  } else if (waypoints.length === 2) {
    routeHintEl.textContent = 'Ready — hit Generate Route (drag pins anytime to adjust)';
  } else {
    routeHintEl.textContent =
      `Ready — hit Generate Route to path through all ${waypoints.length} waypoints.`;
  }
}

function updateGenerateButton() {
  generateRouteBtn.disabled = waypoints.length < 2 || routeLoading;
}

function updateAddButton() {
  const atMax = waypoints.length >= MAX_WAYPOINTS;
  addWaypointBtn.disabled = atMax || routeLoading;
  addWaypointBtn.textContent = atMax ? `Max ${MAX_WAYPOINTS} Waypoints` : '+ Add Waypoint';
}


// ========================================================================
// REMOVE ROUTE FROM MAP
// ========================================================================

function clearRouteLine() {
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
}
}


// ========================================================================
// STRAIGHT-LINE WAYPOINT PREVIEW (client-side only, no backend call)
// ========================================================================

function drawWaypointPreview() {
  if (waypoints.length < 2) {
    removeWaypointPreview();
    return;
  }

  const geojson = {
    type: 'Feature',
    geometry: { type: 'LineString', coordinates: waypoints.map(wp => wp.lngLat) },
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

function removeWaypointPreview() {
  if (map.getLayer('waypoint-preview-line')) map.removeLayer('waypoint-preview-line');
  if (map.getSource('waypoint-preview')) map.removeSource('waypoint-preview');
}


// ========================================================================
// INVALIDATE EXISTING ROUTE
// ========================================================================

function invalidateRoute() {
  clearRouteLine();
  drawWaypointPreview();
  routeStatsEl.hidden = true;
  routeErrorEl.hidden = true;
}


// ========================================================================
// WAYPOINT LIST — PANEL ROWS + MAP MARKERS
// ========================================================================

function renderWaypointRows() {
  routePointsEl.innerHTML = '';

  waypoints.forEach((wp, i) => {
    const role = roleForIndex(i, waypoints.length);

    const row = document.createElement('div');
    row.className = 'route-point-row';

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

// this function is to change the color of pin when its role is changed
// might need to rethink how to do this if where to scale into more waypoints as this is not very efficient
// but works for right now so I will keep it this way
function rebuildMarkers() {
  waypoints.forEach((wp, i) => {
    if (wp.marker) wp.marker.remove();

    const role = roleForIndex(i, waypoints.length);
    const marker = new maplibregl.Marker({ color: colorForRole(role), draggable: true })
      .setLngLat(wp.lngLat)
      .addTo(map);

    marker.on('drag', () => {
      const ll = marker.getLngLat();
      wp.lngLat = [ll.lng, ll.lat];
      renderWaypointRows();
      invalidateRoute();
    });

    wp.marker = marker;
  });
}

// add and remove waypoint functions 
function addWaypointAtCenter() {
  if (waypoints.length >= MAX_WAYPOINTS) return;
  const center = map.getCenter();
  waypoints.push({ lngLat: [center.lng, center.lat], marker: null });

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
  waypoints.forEach(wp => { if (wp.marker) wp.marker.remove(); });
  waypoints = [];

  clearRouteLine();
  removeWaypointPreview();
  renderWaypointRows();
  routeStatsEl.hidden = true;
  routeErrorEl.hidden = true;
  updateGenerateButton();
  updateAddButton();
  updateRouteHint();
}


// ========================================================================
// DRAW ROUTE
// ========================================================================

function drawRoute(routeFeature) {
  const geojson = {
    type:'FeatureCollection',
    features: [routeFeature]
  };

  currentRouteFeature = routeFeature;

  // The real backend route replaces the straight-line preview.
  removeWaypointPreview();

if (exportGPXBtn) {
  exportGPXBtn.disabled = false;
}

  if (map.getSource('route')) {
    map.getSource('route')
       .setData(geojson);
  } else {
    map.addSource('route',{
        type:'geojson',
        data:geojson
      }
    );

    // Route glow
    map.addLayer({
      id:'route-glow',
      type:'line',

      source:'route',

      layout: {
        'line-join': 'round',
        'line-cap':'round'
      },

      paint: {
        'line-color': END_COLOR,
        'line-width': 9,
        'line-blur': 6,
        'line-opacity': 0.35}
    });

    // Main route line
    map.addLayer({
      id:'route-line',
      type:'line',
      source:'route',

      layout: {
        'line-join':'round',
        'line-cap':'round'
      },

      paint: {
        'line-color': END_COLOR,
        'line-width': 3.5,
        'line-opacity': 0.95
      }
    });
  }


  // Zoom map to generated route
  const routeCoordinates = routeFeature.geometry.coordinates;


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

generateRouteBtn.addEventListener('click', async () => {
    if (waypoints.length < 2 || routeLoading) {
      return;
    }

    const pointA = waypoints[0].lngLat;
    const pointB = waypoints[waypoints.length - 1].lngLat;
    const viaPoints = waypoints.slice(1, -1).map(wp => wp.lngLat);

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
    }

    waypoints.forEach(wp => { if (wp.marker) wp.marker.setDraggable(false); });

    routeErrorEl.hidden = true;
    routeStatsEl.hidden =true;
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
