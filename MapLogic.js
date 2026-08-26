// ── CONFIG ───────────────────────────────────────────────────────────────
  // No LINZ key here anymore — it lives server-side in Secrets Manager.
  const API_BASE = 'https://k3w7aj90ok.execute-api.ap-southeast-2.amazonaws.com';
  const LINZ_ORIGIN = 'https://basemaps.linz.govt.nz/v1/';
  // ─────────────────────────────────────────────────────────────────────────

  // Route lambda — now served via its own Lambda Function URL instead of
  // API Gateway, since API Gateway's HTTP API integration timeout is
  // capped at 30s and can't be raised, which was cutting off longer routes.
  const ROUTE_API_URL = 'https://n2jlhbeb2jdxav7cdb6eyn7jze0tgbqm.lambda-url.ap-southeast-2.on.aws/';

  const AERIAL_URL =
    `${API_BASE}/proxy/tiles/aerial/WebMercatorQuad/{z}/{x}/{y}.webp`;

  // Topo is a vector tile service — loaded via StyleJSON after map init.
  // Requested through the SAME generic proxy route as everything else now.
  const TOPO_STYLE_URL =
    `${API_BASE}/proxy/tiles/topographic/WebMercatorQuad/style/topographic.json`;

  // MapLibre calls this before EVERY request it makes internally — tiles,
  // sprites, glyphs, and any nested TileJSON documents a style references.
  // Whatever LINZ URL shows up here (even one we didn't anticipate), we
  // redirect it through our own proxy instead. This is what actually keeps
  // the key out of the browser, regardless of how deeply LINZ nests things.
  function transformRequest(url, resourceType) {
  if (url.startsWith(LINZ_ORIGIN)) {
    const fixedUrl = url.replace(
      '.png&pipeline=',
      '.png?pipeline='
    );

    const parsed = new URL(fixedUrl);
    parsed.searchParams.delete('api');

    const path = parsed.pathname.replace(/^\/v1\//, '');

    return {
      url: API_BASE + '/proxy/' + path + parsed.search

    };
  }

  return { url };
}

if (typeof maplibregl.setMaxParallelImageRequests === 'function') {
  maplibregl.setMaxParallelImageRequests(4);
}

  // Start with aerial only — topo vector layers added after map loads
  const map = new maplibregl.Map({
    container: 'map',
    style: {
      version: 8,
      sprite: `${API_BASE}/proxy/sprites/topographic`,
      glyphs: `${API_BASE}/proxy/fonts/{fontstack}/{range}.pbf`,
      sources: {
        aerial: {
          type: 'raster',
          tiles: [AERIAL_URL],
          tileSize: 256,
          attribution: '© LINZ CC BY 4.0'
        }
      },
      layers: [
        { id: 'aerial-layer', type: 'raster', source: 'aerial', paint: { 'raster-opacity': 1 } }
      ]
    },
    center: [172.5, -41.0],
    zoom: 5,
    minZoom: 4,
    maxZoom: 19,
    transformRequest: transformRequest,
  });

  // ── Load topo vector style and add its sources/layers on top ─────────────
  let topoLayerIds = [];   // track which layer IDs belong to topo
  let topoVisible  = true;
  let topoOpacity  = 0.6;

  map.on('load', async () => {

    try {
      const resp  = await fetch(TOPO_STYLE_URL);
      const style = await resp.json();
      // Add each topo source into the map
      for (const [id, src] of Object.entries(style.sources || {})) {
        if (!map.getSource(id)) map.addSource(id, src);
      }

      // Add each topo layer on top of aerial, recording its id
      for (const layer of (style.layers || [])) {
        if (!map.getLayer(layer.id)) {
          map.addLayer(layer);
          topoLayerIds.push(layer.id);
        }
      }

      // Apply initial opacity to all topo layers
      applyTopoOpacity(topoOpacity);

    } catch (err) {
      console.error('Failed to load LINZ topo style:', err);
    }

  });

  // Helper — set opacity on every topo layer based on its type
  function applyTopoOpacity(opacity) {
    for (const id of topoLayerIds) {
      if (!map.getLayer(id)) continue;
      const type = map.getLayer(id).type;
      try {
        if (type === 'fill')   map.setPaintProperty(id, 'fill-opacity',    opacity);
        if (type === 'background') map.setPaintProperty(id, 'background-opacity', opacity);
        if (type === 'line')   map.setPaintProperty(id, 'line-opacity',    opacity);
        if (type === 'symbol') map.setPaintProperty(id, 'icon-opacity',    opacity);
        if (type === 'symbol') map.setPaintProperty(id, 'text-opacity',    opacity);
        if (type === 'circle') map.setPaintProperty(id, 'circle-opacity',  opacity);
        if (type === 'raster') map.setPaintProperty(id, 'raster-opacity',  opacity);
      } catch(_) {}
    }
  }

  // ── Coordinate display ───────────────────────────────────────────────────
  const coordsEl = document.getElementById('coords');
  map.on('mousemove', e => {
    const { lng, lat } = e.lngLat;
    coordsEl.textContent = `Longitude: ${lng.toFixed(5)}° Latitude: ${lat.toFixed(5)}° `;
  });

  // ── Layer toggles ────────────────────────────────────────────────────────
  document.getElementById('toggle-aerial').addEventListener('change', e => {
    map.setLayoutProperty('aerial-layer', 'visibility', e.target.checked ? 'visible' : 'none');
  });

  document.getElementById('toggle-topo').addEventListener('change', e => {
    topoVisible = e.target.checked;
    const vis = topoVisible ? 'visible' : 'none';
    for (const id of topoLayerIds) {
      if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', vis);
    }
  });

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
  // Drop a start/end pin with a button (places it at the current map
  // center), then drag it to fine-tune. Markers own their own drag
  // gesture (MapLibre disables map panning while a marker drag is in
  // progress), so this sidesteps any conflict with the map's own
  // click-and-drag panning — a plain map click was unreliable for this.

  const START_COLOR = '#ff8400'; // matches --accent
  const END_COLOR   = '#2fd4a0'; // matches --accent2

  const routeHintEl      = document.getElementById('route-hint');
  const pointADotEl      = document.getElementById('point-a-dot');
  const pointBDotEl      = document.getElementById('point-b-dot');
  const pointACoordsEl   = document.getElementById('point-a-coords');
  const pointBCoordsEl   = document.getElementById('point-b-coords');
  const placeStartBtn    = document.getElementById('place-start');
  const placeEndBtn      = document.getElementById('place-end');
  const generateRouteBtn = document.getElementById('generate-route');
  const clearRouteBtn    = document.getElementById('clear-route');
  const routeStatsEl     = document.getElementById('route-stats');
  const statDistanceEl   = document.getElementById('stat-distance');
  const statTimeEl       = document.getElementById('stat-time');
  const statClimbEl      = document.getElementById('stat-climb');
  const routeErrorEl     = document.getElementById('route-error');

  let pointA = null;       // [lon, lat]
  let pointB = null;       // [lon, lat]
  let markerA = null;
  let markerB = null;
  let routeLoading = false;

  function formatCoords(lon, lat) {
    return `${lat.toFixed(4)}°, ${lon.toFixed(4)}°`;
  }

  function formatDuration(hours) {
    const totalMinutes = Math.round(hours * 60);
    const h = Math.floor(totalMinutes / 60);
    const m = totalMinutes % 60;
    if (h === 0) return `${m} min`;
    return `${h}h ${m}m`;
  }

  function updateRouteHint() {
    if (routeLoading) return; // hint is owned by the in-flight request while loading
    if (!pointA && !pointB) routeHintEl.textContent = 'Drop a start and end pin, then drag to fine-tune';
    else if (!pointA)       routeHintEl.textContent = 'Drop a start pin, then drag it into place';
    else if (!pointB)       routeHintEl.textContent = 'Drop an end pin, then drag it into place';
    else                     routeHintEl.textContent = 'Ready — hit Generate Route (drag pins anytime to adjust)';
  }

  function clearRouteLine() {
    if (map.getLayer('route-line')) map.removeLayer('route-line');
    if (map.getLayer('route-glow')) map.removeLayer('route-glow');
    if (map.getSource('route'))     map.removeSource('route');
  }

  // Any pin move invalidates a route that's already on screen — clear it
  // and hide stale results, but leave the pins themselves alone.
  function invalidateRoute() {
    clearRouteLine();
    routeStatsEl.hidden = true;
    routeErrorEl.hidden = true;
  }

  function setPointDisplay(which, lng, lat) {
    const dotEl    = which === 'start' ? pointADotEl    : pointBDotEl;
    const coordsEl = which === 'start' ? pointACoordsEl : pointBCoordsEl;
    coordsEl.textContent = formatCoords(lng, lat);
    coordsEl.classList.add('is-set');
    dotEl.classList.add('is-set');
  }

  function placePin(which) {
    const center = map.getCenter();
    const lngLat = [center.lng, center.lat];

    if (which === 'start') {
      pointA = lngLat;
      if (markerA) {
        markerA.setLngLat(lngLat);
      } else {
        markerA = new maplibregl.Marker({ color: START_COLOR, draggable: true })
          .setLngLat(lngLat)
          .addTo(map);
        markerA.on('drag', () => {
          const ll = markerA.getLngLat();
          pointA = [ll.lng, ll.lat];
          setPointDisplay('start', ll.lng, ll.lat);
          invalidateRoute();
        });
      }
      setPointDisplay('start', lngLat[0], lngLat[1]);
    } else {
      pointB = lngLat;
      if (markerB) {
        markerB.setLngLat(lngLat);
      } else {
        markerB = new maplibregl.Marker({ color: END_COLOR, draggable: true })
          .setLngLat(lngLat)
          .addTo(map);
        markerB.on('drag', () => {
          const ll = markerB.getLngLat();
          pointB = [ll.lng, ll.lat];
          setPointDisplay('end', ll.lng, ll.lat);
          invalidateRoute();
        });
      }
      setPointDisplay('end', lngLat[0], lngLat[1]);
    }

    invalidateRoute();
    generateRouteBtn.disabled = !(pointA && pointB);
    updateRouteHint();
  }

  function resetRoute() {
    pointA = null;
    pointB = null;

    if (markerA) { markerA.remove(); markerA = null; }
    if (markerB) { markerB.remove(); markerB = null; }

    clearRouteLine();

    pointADotEl.classList.remove('is-set');
    pointBDotEl.classList.remove('is-set');
    pointACoordsEl.classList.remove('is-set');
    pointBCoordsEl.classList.remove('is-set');
    pointACoordsEl.textContent = 'Not set';
    pointBCoordsEl.textContent = 'Not set';
    routeStatsEl.hidden = true;
    routeErrorEl.hidden = true;
    generateRouteBtn.disabled = true;
    updateRouteHint();
  }

  function drawRoute(routeFeature) {
    const geojson = { type: 'FeatureCollection', features: [routeFeature] };

    if (map.getSource('route')) {
      map.getSource('route').setData(geojson);
    } else {
      map.addSource('route', { type: 'geojson', data: geojson });

      // Soft glow underneath the line
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

    const coords = routeFeature.geometry.coordinates;
    const bounds = coords.reduce(
      (b, c) => b.extend(c),
      new maplibregl.LngLatBounds(coords[0], coords[0])
    );
    map.fitBounds(bounds, { padding: 80, duration: 800 });
  }

  placeStartBtn.addEventListener('click', () => { if (!routeLoading) placePin('start'); });
  placeEndBtn.addEventListener('click',   () => { if (!routeLoading) placePin('end'); });

  generateRouteBtn.addEventListener('click', async () => {
    if (!pointA || !pointB || routeLoading) return;

    // Show full-screen loading screen
    showLoadingScreen();

    routeLoading = true;
    generateRouteBtn.disabled = true;
    generateRouteBtn.textContent = 'Calculating…';
    clearRouteBtn.disabled = true;
    placeStartBtn.disabled = true;
    placeEndBtn.disabled = true;
    
    if (markerA) markerA.setDraggable(false);
    if (markerB) markerB.setDraggable(false);
    
    routeErrorEl.hidden = true;
    routeStatsEl.hidden = true;
    routeHintEl.textContent = 'Crunching terrain data — this can take up to 30 seconds for longer routes.';

    const controller = new AbortController();
    // times out after 130 secs 
    const timeoutId = setTimeout(() => controller.abort(), 120000); 

    try {
      const resp = await fetch(ROUTE_API_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ a: pointA, b: pointB }),
        signal: controller.signal,
      });

      clearTimeout(timeoutId);

      let data;
      try {
        data = await resp.json();
      } catch (_) {
        throw new Error(`Server returned an unreadable response (status ${resp.status}).`);
      }

      if (!resp.ok || !data.ok) {
        throw new Error(data.error || `Route request failed (status ${resp.status}).`);
      }

      drawRoute(data.route);

      statDistanceEl.textContent = `${data.distance_km.toFixed(2)} km`;
      statTimeEl.textContent = formatDuration(data.estimated_hours);
      statClimbEl.textContent = `${Math.round(data.climb_m)} m`;
      routeStatsEl.hidden = false;
      routeHintEl.textContent = 'Route generated. Drag either pin to plan a new route.';

    } catch (err) {
      clearTimeout(timeoutId);
      const message = err.name === 'AbortError'
        ? 'The route request timed out. Try two points that are closer together.'
        : (err.message || 'Something went wrong generating the route.');
      routeErrorEl.textContent = message;
      routeErrorEl.hidden = false;
      routeHintEl.textContent = 'Ready — hit Generate Route to try again.';
    } finally {

      // Hide loading screen when Lambda finishes OR fails
      hideLoadingScreen();

      routeLoading = false;
      
      generateRouteBtn.disabled = !(pointA && pointB);
      generateRouteBtn.textContent = 'Generate Route';
      
      clearRouteBtn.disabled = false;
      placeStartBtn.disabled = false;
      placeEndBtn.disabled = false;
      
      if (markerA) markerA.setDraggable(true);
      if (markerB) markerB.setDraggable(true);
    }
  });

  clearRouteBtn.addEventListener('click', () => {
    if (routeLoading) return;
    resetRoute();
  });


  // Show the loading screen
function showLoadingScreen() {
    document.getElementById("loading-screen").style.display = "flex";
}

// Hide the loading screen
function hideLoadingScreen() {
    document.getElementById("loading-screen").style.display = "none";
}
