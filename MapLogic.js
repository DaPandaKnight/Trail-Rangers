// ─────────────────────────────────────────────────────────────────────────
// RidgeWalker Map Configuration
// ─────────────────────────────────────────────────────────────────────────

// For a public prototype, avoid committing this API key to a public repository.
const LINZ_API_KEY = 'c01kqxnkw0j9qyfvywax1ga2wcb';

const AERIAL_URL =
  `https://basemaps.linz.govt.nz/v1/tiles/aerial/WebMercatorQuad/{z}/{x}/{y}.webp?api=${LINZ_API_KEY}`;

const TOPO_STYLE_URL =
  `https://basemaps.linz.govt.nz/v1/tiles/topographic/WebMercatorQuad/style/topographic.json?api=${LINZ_API_KEY}`;

// ─────────────────────────────────────────────────────────────────────────
// Map state
// ─────────────────────────────────────────────────────────────────────────

let map;

let topoLayerIds = [];
let topoVisible = true;
let topoOpacity = 0.6;

// Prevent controls from being initialised more than once
let controlsInitialised = false;

// TODO: update to wherever the Flask app in terrain_routing_v5.py is
// actually running once it's deployed/started.
const ROUTE_API_URL = 'http://localhost:5000/route';

// Hardcoded test waypoints, since there's no waypoint picker yet.
// These match DEFAULT_TEST_BBOX's corners in terrain_routing_v5.py.
const TEST_WAYPOINT_A = [175.60, -39.16]; // [lon, lat]
const TEST_WAYPOINT_B = [175.62, -39.15];

const ROUTE_SOURCE_ID = 'route';
const ROUTE_LINE_LAYER_ID = 'route-line';

// Stats for whichever route is currently drawn, read by the click popup.
let activeRouteDetails = null;
let routePopup = null;

// ─────────────────────────────────────────────────────────────────────────
// Initialise map
// ─────────────────────────────────────────────────────────────────────────

async function initialiseMap() {
  try {
    const response = await fetch(TOPO_STYLE_URL);

    if (!response.ok) {
      throw new Error(
        `LINZ topographic style request failed: ` +
        `${response.status} ${response.statusText}`
      );
    }

    const topoStyle = await response.json();

    if (!topoStyle.sources || !topoStyle.layers) {
      throw new Error(
        'The LINZ topographic response does not contain valid sources or layers.'
      );
    }

    // Store the IDs of all original LINZ topographic layers.
    // This allows the visibility and opacity controls to update only topo layers.
    topoLayerIds = topoStyle.layers.map(layer => layer.id);

    const combinedStyle = {
      version: 8,

      // LINZ sprite sheet.
      // This contains patterns and icons such as moraine_poly_half.
      ...(topoStyle.sprite && {
        sprite: topoStyle.sprite
      }),

      // LINZ glyphs used for map labels.
      ...(topoStyle.glyphs && {
        glyphs: topoStyle.glyphs
      }),

      sources: {
        // RidgeWalker aerial raster source
        aerial: {
          type: 'raster',
          tiles: [AERIAL_URL],
          tileSize: 256,
          attribution: '© LINZ CC BY 4.0'
        },

        // LINZ topographic vector sources
        ...topoStyle.sources
      },

      layers: [
        // Add aerial first so all topographic layers appear above it
        {
          id: 'aerial-layer',
          type: 'raster',
          source: 'aerial',
          paint: {
            'raster-opacity': 1
          }
        },

        // Add all LINZ topographic layers
        ...topoStyle.layers
      ]
    };

    map = new maplibregl.Map({
      container: 'map',
      style: combinedStyle,
      center: [172.5, -41.0],
      zoom: 5,
      minZoom: 4,
      maxZoom: 19,

      // Mobile and desktop map interactions
      dragPan: true,
      scrollZoom: true,
      touchZoomRotate: true
    });

    // Prevent accidental rotation when users pinch-zoom on mobile
    map.touchZoomRotate.disableRotation();

    map.on('load', () => {
      applyTopoOpacity(topoOpacity);
      initialiseMapControls();

      console.log('RidgeWalker map loaded successfully.');
    });

    map.on('error', event => {
      console.error('MapLibre error:', event.error || event);
    });

  } catch (error) {
    console.error('Failed to initialise the RidgeWalker map:', error);

    showMapError(
      'The map could not be loaded. Please check your internet connection ' +
      'and LINZ API configuration.'
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────
// Apply topographic opacity
// ─────────────────────────────────────────────────────────────────────────

function applyTopoOpacity(opacity) {
  if (!map) return;

  for (const id of topoLayerIds) {
    const layer = map.getLayer(id);

    if (!layer) continue;

    try {
      switch (layer.type) {
        case 'fill':
          map.setPaintProperty(id, 'fill-opacity', opacity);
          break;

        case 'background':
          map.setPaintProperty(id, 'background-opacity', opacity);
          break;

        case 'line':
          map.setPaintProperty(id, 'line-opacity', opacity);
          break;

        case 'symbol':
          map.setPaintProperty(id, 'icon-opacity', opacity);
          map.setPaintProperty(id, 'text-opacity', opacity);
          break;

        case 'circle':
          map.setPaintProperty(id, 'circle-opacity', opacity);
          break;

        case 'raster':
          map.setPaintProperty(id, 'raster-opacity', opacity);
          break;

        case 'heatmap':
          map.setPaintProperty(id, 'heatmap-opacity', opacity);
          break;

        case 'fill-extrusion':
          map.setPaintProperty(id, 'fill-extrusion-opacity', opacity);
          break;

        default:
          break;
      }
    } catch (error) {
      console.warn(
        `Could not change opacity for topographic layer "${id}".`,
        error
      );
    }
  }
}

// ─────────────────────────────────────────────────────────────────────────
// Initialise interface controls
// ─────────────────────────────────────────────────────────────────────────

function initialiseMapControls() {
  if (controlsInitialised) return;

  controlsInitialised = true;

  initialiseCoordinateDisplay();
  initialiseLayerToggles();
  initialiseOpacitySlider();
  initialiseZoomButtons();
  initialiseRoutePopup();

  generateRoute(TEST_WAYPOINT_A, TEST_WAYPOINT_B);
}

// ─────────────────────────────────────────────────────────────────────────
// Coordinate display
// ─────────────────────────────────────────────────────────────────────────

function initialiseCoordinateDisplay() {
  const coordsEl = document.getElementById('coords');

  if (!coordsEl) {
    console.warn('Coordinate display element with ID "coords" was not found.');
    return;
  }

  function updateCoordinates(lngLat) {
    if (!lngLat) return;

    const { lng, lat } = lngLat;

    coordsEl.textContent =
      `Longitude: ${lng.toFixed(5)}° ` +
      `Latitude: ${lat.toFixed(5)}°`;
  }

  // Desktop pointer movement
  map.on('mousemove', event => {
    updateCoordinates(event.lngLat);
  });

  // Mobile tap
  map.on('click', event => {
    updateCoordinates(event.lngLat);
  });
}

// ─────────────────────────────────────────────────────────────────────────
// Layer visibility controls
// ─────────────────────────────────────────────────────────────────────────

function initialiseLayerToggles() {
  const aerialToggle = document.getElementById('toggle-aerial');
  const topoToggle = document.getElementById('toggle-topo');

  if (aerialToggle) {
    aerialToggle.addEventListener('change', event => {
      if (!map.getLayer('aerial-layer')) return;

      map.setLayoutProperty(
        'aerial-layer',
        'visibility',
        event.target.checked ? 'visible' : 'none'
      );
    });
  } else {
    console.warn('Aerial toggle with ID "toggle-aerial" was not found.');
  }

  if (topoToggle) {
    topoToggle.addEventListener('change', event => {
      topoVisible = event.target.checked;

      const visibility = topoVisible ? 'visible' : 'none';

      for (const id of topoLayerIds) {
        if (map.getLayer(id)) {
          map.setLayoutProperty(id, 'visibility', visibility);
        }
      }
    });
  } else {
    console.warn('Topo toggle with ID "toggle-topo" was not found.');
  }
}

// ─────────────────────────────────────────────────────────────────────────
// Topographic opacity control
// ─────────────────────────────────────────────────────────────────────────

function initialiseOpacitySlider() {
  const opacitySlider = document.getElementById('topo-opacity');
  const opacityVal = document.getElementById('opacity-val');

  if (!opacitySlider) {
    console.warn('Opacity slider with ID "topo-opacity" was not found.');
    return;
  }

  // Set the initial slider position to match topoOpacity
  opacitySlider.value = String(Math.round(topoOpacity * 100));

  if (opacityVal) {
    opacityVal.textContent = `${opacitySlider.value}%`;
  }

  opacitySlider.addEventListener('input', () => {
    const sliderValue = Number(opacitySlider.value);

    if (!Number.isFinite(sliderValue)) return;

    topoOpacity = sliderValue / 100;

    applyTopoOpacity(topoOpacity);

    if (opacityVal) {
      opacityVal.textContent = `${sliderValue}%`;
    }
  });
}

// ─────────────────────────────────────────────────────────────────────────
// Zoom controls
// ─────────────────────────────────────────────────────────────────────────

function initialiseZoomButtons() {
  const zoomInButton = document.getElementById('zoom-in');
  const zoomOutButton = document.getElementById('zoom-out');

  if (zoomInButton) {
    zoomInButton.addEventListener('click', () => {
      map.zoomIn();
    });
  } else {
    console.warn('Zoom-in button with ID "zoom-in" was not found.');
  }

  if (zoomOutButton) {
    zoomOutButton.addEventListener('click', () => {
      map.zoomOut();
    });
  } else {
    console.warn('Zoom-out button with ID "zoom-out" was not found.');
  }
}

// ─────────────────────────────────────────────────────────────────────────
// Route generation
// ─────────────────────────────────────────────────────────────────────────

async function generateRoute(waypointA, waypointB) {
  try {
    const response = await fetch(ROUTE_API_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ a: waypointA, b: waypointB })
    });

    if (!response.ok) {
      throw new Error(`Route request failed: ${response.status} ${response.statusText}`);
    }

    const result = await response.json();

    if (!result.ok) {
      throw new Error(result.error || 'Routing failed.');
    }

    drawRoute(result);
  } catch (error) {
    console.error('Failed to generate route:', error);
  }
}

function drawRoute(result) {
  activeRouteDetails = {
    distance_km: result.distance_km,
    estimated_hours: result.estimated_hours,
    climb_m: result.climb_m
  };

  const existingSource = map.getSource(ROUTE_SOURCE_ID);

  if (existingSource) {
    existingSource.setData(result.route);
  } else {
    map.addSource(ROUTE_SOURCE_ID, {
      type: 'geojson',
      data: result.route
    });

    map.addLayer({
      id: ROUTE_LINE_LAYER_ID,
      type: 'line',
      source: ROUTE_SOURCE_ID,
      layout: {
        'line-cap': 'round',
        'line-join': 'round'
      },
      paint: {
        'line-color': '#ff8400',
        'line-width': 4
      }
    });
  }

  const coordinates = result.route.geometry.coordinates;

  const bounds = coordinates.reduce(
    (bounds, coord) => bounds.extend(coord),
    new maplibregl.LngLatBounds(coordinates[0], coordinates[0])
  );

  map.fitBounds(bounds, { padding: 60 });
}

// ─────────────────────────────────────────────────────────────────────────
// Route popup
// ─────────────────────────────────────────────────────────────────────────

function formatDuration(hours) {
  if (!Number.isFinite(hours)) return 'Unknown';

  const totalMinutes = Math.round(hours * 60);
  const h = Math.floor(totalMinutes / 60);
  const m = totalMinutes % 60;

  if (h === 0) return `${m} min`;
  if (m === 0) return `${h} h`;
  return `${h} h ${m} min`;
}

function initialiseRoutePopup() {
  map.on('mouseenter', ROUTE_LINE_LAYER_ID, () => {
    map.getCanvas().style.cursor = 'pointer';
  });

  map.on('mouseleave', ROUTE_LINE_LAYER_ID, () => {
    map.getCanvas().style.cursor = '';
  });

  map.on('click', ROUTE_LINE_LAYER_ID, event => {
    if (!activeRouteDetails) return;

    const { distance_km, estimated_hours, climb_m } = activeRouteDetails;

    if (routePopup) {
      routePopup.remove();
    }

    routePopup = new maplibregl.Popup()
      .setLngLat(event.lngLat)
      .setHTML(`
        <h3>Route Details</h3>
        <div>Distance: ${distance_km.toFixed(2)} km</div>
        <div>Est. time: ${formatDuration(estimated_hours)}</div>
        <div>Climb: ${Math.round(climb_m)} m</div>
      `)
      .addTo(map);
  });
}


// ─────────────────────────────────────────────────────────────────────────
// Error message
// ─────────────────────────────────────────────────────────────────────────

function showMapError(message) {
  const mapContainer = document.getElementById('map');

  if (!mapContainer) return;

  const errorMessage = document.createElement('div');

  errorMessage.textContent = message;

  errorMessage.style.position = 'absolute';
  errorMessage.style.top = '50%';
  errorMessage.style.left = '50%';
  errorMessage.style.transform = 'translate(-50%, -50%)';
  errorMessage.style.zIndex = '1000';
  errorMessage.style.maxWidth = '420px';
  errorMessage.style.padding = '18px';
  errorMessage.style.border = '1px solid #2a2f2c';
  errorMessage.style.borderRadius = '10px';
  errorMessage.style.background = 'rgba(20, 24, 23, 0.95)';
  errorMessage.style.color = '#e8ede9';
  errorMessage.style.fontFamily = "'DM Mono', monospace";
  errorMessage.style.fontSize = '0.8rem';
  errorMessage.style.lineHeight = '1.5';
  errorMessage.style.textAlign = 'centre';

  mapContainer.appendChild(errorMessage);
}

// ─────────────────────────────────────────────────────────────────────────
// Start RidgeWalker
// ─────────────────────────────────────────────────────────────────────────

initialiseMap();