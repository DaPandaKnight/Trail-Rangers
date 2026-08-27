// ── CONFIG ───────────────────────────────────────────────────────────────

// No LINZ key here anymore — it lives server-side.
const API_BASE =
  'https://k3w7aj90ok.execute-api.ap-southeast-2.amazonaws.com';

const LINZ_ORIGIN =
  'https://basemaps.linz.govt.nz/v1/';

// Route generation Lambda Function URL
const ROUTE_API_URL =
  'https://n2jlhbeb2jdxav7cdb6eyn7jze0tgbqm.lambda-url.ap-southeast-2.on.aws/';


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

    const path =
      parsed.pathname.replace(/^\/v1\//, '');

    return {
      url:
        API_BASE +
        '/proxy/' +
        path +
        parsed.search
    };
  }

  return { url };
}


// Reduce simultaneous image requests if supported
if (
  typeof maplibregl.setMaxParallelImageRequests ===
  'function'
) {
  maplibregl.setMaxParallelImageRequests(4);
}


// ========================================================================
// MAP
// ========================================================================

const map = new maplibregl.Map({

  container: 'map',

  style: {

    version: 8,

    sprite:
      `${API_BASE}/proxy/sprites/topographic`,

    glyphs:
      `${API_BASE}/proxy/fonts/{fontstack}/{range}.pbf`,

    sources: {

      aerial: {

        type: 'raster',

        tiles: [
          AERIAL_URL
        ],

        tileSize: 256,

        attribution:
          '© LINZ CC BY 4.0'
      }
    },

    layers: [

      {
        id: 'aerial-layer',

        type: 'raster',

        source: 'aerial',

        paint: {
          'raster-opacity': 1
        }
      }
    ]
  },

  center: [
    172.5,
    -41.0
  ],

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

    const resp =
      await fetch(TOPO_STYLE_URL);

    const style =
      await resp.json();


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
    applyTopoOpacity(
      topoOpacity
    );


  } catch (err) {

    console.error(
      'Failed to load LINZ topo style:',
      err
    );
  }

});


// ── Topo opacity helper ──────────────────────────────────────────────────

function applyTopoOpacity(opacity) {

  for (const id of topoLayerIds) {

    if (!map.getLayer(id)) {
      continue;
    }

    const type =
      map.getLayer(id).type;


    try {

      if (type === 'fill') {
        map.setPaintProperty(
          id,
          'fill-opacity',
          opacity
        );
      }


      if (type === 'background') {
        map.setPaintProperty(
          id,
          'background-opacity',
          opacity
        );
      }


      if (type === 'line') {
        map.setPaintProperty(
          id,
          'line-opacity',
          opacity
        );
      }


      if (type === 'symbol') {

        map.setPaintProperty(
          id,
          'icon-opacity',
          opacity
        );

        map.setPaintProperty(
          id,
          'text-opacity',
          opacity
        );
      }


      if (type === 'circle') {
        map.setPaintProperty(
          id,
          'circle-opacity',
          opacity
        );
      }


      if (type === 'raster') {
        map.setPaintProperty(
          id,
          'raster-opacity',
          opacity
        );
      }

    } catch (_) {

      // Ignore unsupported opacity properties
    }
  }
}


// ========================================================================
// COORDINATE DISPLAY
// ========================================================================

const coordsEl =
  document.getElementById('coords');


map.on('mousemove', event => {

  const {
    lng,
    lat
  } = event.lngLat;


  coordsEl.textContent =
    `Longitude: ${lng.toFixed(5)}° Latitude: ${lat.toFixed(5)}° `;
});


// ========================================================================
// MAP LAYER CONTROLS
// ========================================================================

// Aerial imagery
document
  .getElementById('toggle-aerial')
  .addEventListener(
    'change',
    event => {

      map.setLayoutProperty(
        'aerial-layer',
        'visibility',

        event.target.checked
          ? 'visible'
          : 'none'
      );
    }
  );


// Topographic map
document
  .getElementById('toggle-topo')
  .addEventListener(
    'change',
    event => {

      topoVisible =
        event.target.checked;

      const visibility =
        topoVisible
          ? 'visible'
          : 'none';


      for (
        const id
        of topoLayerIds
      ) {

        if (map.getLayer(id)) {

          map.setLayoutProperty(
            id,
            'visibility',
            visibility
          );
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
  const exportGPXBtn     = document.getElementById('export-gpx');

  let pointA = null;       // [lon, lat]
  let pointB = null;       // [lon, lat]
  let markerA = null;
  let markerB = null;
  let routeLoading = false;



  function formatCoords(lon, lat) {
    return `${lat.toFixed(4)}°, ${lon.toFixed(4)}°`;
  }

  function formatDuration(hours) {
    const h = Math.floor(hours);
    const m = Math.round((hours - h) * 60);
    return `${h}h ${m}m`;
  }


// ========================================================================
// ROUTE HINT
// ========================================================================

function updateRouteHint() {

  if (routeLoading) {
    return;
  }


  if (
    !pointA &&
    !pointB
  ) {

    routeHintEl.textContent =
      'Drop a start and end pin, then drag to fine-tune';
  }


  else if (!pointA) {

    routeHintEl.textContent =
      'Drop a start pin, then drag it into place';
  }


  else if (!pointB) {

    routeHintEl.textContent =
      'Drop an end pin, then drag it into place';
  }


  else {

    routeHintEl.textContent =
      'Ready — hit Generate Route (drag pins anytime to adjust)';
  }
}


// ========================================================================
// REMOVE ROUTE FROM MAP
// ========================================================================

function clearRouteLine() {

  if (
    map.getLayer(
      'route-line'
    )
  ) {

    map.removeLayer(
      'route-line'
    );
  }


  if (
    map.getLayer(
      'route-glow'
    )
  ) {

    map.removeLayer(
      'route-glow'
    );
  }


  if (
    map.getSource(
      'route'
    )
  ) {

    map.removeSource(
      'route'
    );
  }
}


// ========================================================================
// INVALIDATE EXISTING ROUTE
// ========================================================================

function invalidateRoute() {

  clearRouteLine();


  routeStatsEl.hidden =
    true;

  routeErrorEl.hidden =
    true;

}


// ========================================================================
// UPDATE PIN DISPLAY
// ========================================================================

function setPointDisplay(
  which,
  lng,
  lat
) {

  const dotEl =
    which === 'start'
      ? pointADotEl
      : pointBDotEl;


  const coordinatesEl =
    which === 'start'
      ? pointACoordsEl
      : pointBCoordsEl;


  coordinatesEl.textContent =
    formatCoords(
      lng,
      lat
    );


  coordinatesEl.classList.add(
    'is-set'
  );


  dotEl.classList.add(
    'is-set'
  );
}


// ========================================================================
// PLACE START / END PIN
// ========================================================================

function placePin(which) {

  const center =
    map.getCenter();


  const lngLat = [
    center.lng,
    center.lat
  ];


  // ── START ──────────────────────────────────────────────────────────────

  if (which === 'start') {

    pointA =
      lngLat;


    if (markerA) {

      markerA.setLngLat(
        lngLat
      );

    } else {

      markerA =
        new maplibregl.Marker({

          color:
            START_COLOR,

          draggable:
            true
        })

          .setLngLat(
            lngLat
          )

          .addTo(
            map
          );


      markerA.on(
        'drag',
        () => {

          const ll =
            markerA.getLngLat();


          pointA = [
            ll.lng,
            ll.lat
          ];


          setPointDisplay(
            'start',
            ll.lng,
            ll.lat
          );


          invalidateRoute();
        }
      );
    }


    setPointDisplay(
      'start',
      lngLat[0],
      lngLat[1]
    );
  }


  // ── END ────────────────────────────────────────────────────────────────

  else {

    pointB =
      lngLat;


    if (markerB) {

      markerB.setLngLat(
        lngLat
      );

    } else {

      markerB =
        new maplibregl.Marker({

          color:
            END_COLOR,

          draggable:
            true
        })

          .setLngLat(
            lngLat
          )

          .addTo(
            map
          );


      markerB.on(
        'drag',
        () => {

          const ll =
            markerB.getLngLat();


          pointB = [
            ll.lng,
            ll.lat
          ];


          setPointDisplay(
            'end',
            ll.lng,
            ll.lat
          );


          invalidateRoute();
        }
      );
    }


    setPointDisplay(
      'end',
      lngLat[0],
      lngLat[1]
    );
  }


  // Moving a pin removes the existing route
  invalidateRoute();


  // Generate becomes available when
  // both points have been selected
  generateRouteBtn.disabled =
    !(pointA && pointB);


  updateRouteHint();
}


// ========================================================================
// RESET ROUTE
// ========================================================================

function resetRoute() {

  pointA =
    null;

  pointB =
    null;


  if (markerA) {

    markerA.remove();

    markerA =
      null;
  }


  if (markerB) {

    markerB.remove();

    markerB =
      null;
  }


  clearRouteLine();


  pointADotEl.classList.remove(
    'is-set'
  );

  pointBDotEl.classList.remove(
    'is-set'
  );


  pointACoordsEl.classList.remove(
    'is-set'
  );

  pointBCoordsEl.classList.remove(
    'is-set'
  );


  pointACoordsEl.textContent =
    'Not set';

  pointBCoordsEl.textContent =
    'Not set';


  routeStatsEl.hidden =
    true;

  routeErrorEl.hidden =
    true;


  generateRouteBtn.disabled =
    true;

  if (exportGPXBtn) {

    exportGPXBtn.disabled =
      true;
  }


  updateRouteHint();
}


// ========================================================================
// DRAW ROUTE
// ========================================================================

function drawRoute(
  routeFeature
) {

  const geojson = {

    type:
      'FeatureCollection',

    features: [
      routeFeature
    ]
  };


  if (
    map.getSource(
      'route'
    )
  ) {

    map
      .getSource(
        'route'
      )
      .setData(
        geojson
      );

  } else {

    map.addSource(
      'route',
      {

        type:
          'geojson',

        data:
          geojson
      }
    );


    // Route glow
    map.addLayer({

      id:
        'route-glow',

      type:
        'line',

      source:
        'route',

      layout: {

        'line-join':
          'round',

        'line-cap':
          'round'
      },

      paint: {

        'line-color':
          END_COLOR,

        'line-width':
          9,

        'line-blur':
          6,

        'line-opacity':
          0.35
      }
    });


    // Main route line
    map.addLayer({

      id:
        'route-line',

      type:
        'line',

      source:
        'route',

      layout: {

        'line-join':
          'round',

        'line-cap':
          'round'
      },

      paint: {

        'line-color':
          END_COLOR,

        'line-width':
          3.5,

        'line-opacity':
          0.95
      }
    });
  }


  // Zoom map to generated route
  const routeCoordinates =
    routeFeature
      .geometry
      .coordinates;


  const bounds =
    routeCoordinates.reduce(

      (
        existingBounds,
        coordinate
      ) =>

        existingBounds.extend(
          coordinate
        ),

      new maplibregl.LngLatBounds(

        routeCoordinates[0],

        routeCoordinates[0]
      )
    );


  map.fitBounds(
    bounds,
    {

      padding:
        80,

      duration:
        800
    }
  );
}


// ========================================================================
// PIN BUTTONS
// ========================================================================

placeStartBtn.addEventListener(
  'click',
  () => {

    if (!routeLoading) {

      placePin(
        'start'
      );
    }
  }
);


placeEndBtn.addEventListener(
  'click',
  () => {

    if (!routeLoading) {

      placePin(
        'end'
      );
    }
  }
);


// ========================================================================
// GENERATE ROUTE
// ========================================================================

generateRouteBtn.addEventListener(
  'click',
  async () => {

    if (
      !pointA ||
      !pointB ||
      routeLoading
    ) {

      return;
    }


    // --------------------------------------------------
    // SHOW LOADING SCREEN
    // --------------------------------------------------

    showLoadingScreen();


    routeLoading =
      true;


    generateRouteBtn.disabled =
      true;


    generateRouteBtn.textContent =
      'Calculating…';


    clearRouteBtn.disabled =
      true;


    placeStartBtn.disabled =
      true;


    placeEndBtn.disabled =
      true;


    if (exportGPXBtn) {

      exportGPXBtn.disabled =
        true;
    }


    if (markerA) {

      markerA.setDraggable(
        false
      );
    }


    if (markerB) {

      markerB.setDraggable(
        false
      );
    }


    routeErrorEl.hidden =
      true;


    routeStatsEl.hidden =
      true;


    routeHintEl.textContent =
      'Crunching terrain data — this can take a little while for longer routes.';


    // --------------------------------------------------
    // TIMEOUT
    // --------------------------------------------------

    const controller =
      new AbortController();


    // 120 second timeout
    const timeoutId =
      setTimeout(
        () =>
          controller.abort(),

        120000
      );


    try {

      // ------------------------------------------------
      // SEND ROUTE REQUEST TO LAMBDA
      // ------------------------------------------------

      const response =
        await fetch(
          ROUTE_API_URL,
          {

            method:
              'POST',

            headers: {

              'Content-Type':
                'application/json'
            },

            body:
              JSON.stringify({

                a:
                  pointA,

                b:
                  pointB
              }),

            signal:
              controller.signal
          }
        );


      clearTimeout(
        timeoutId
      );


      // ------------------------------------------------
      // READ RESPONSE
      // ------------------------------------------------

      let data;

      try {

        data =
          await response.json();

      } catch (_) {

        throw new Error(

          `Server returned an unreadable response (status ${response.status}).`
        );
      }


      // ------------------------------------------------
      // CHECK FOR SERVER ERROR
      // ------------------------------------------------

      if (!response.ok ||!data.ok) {

        throw new Error(

          data.error ||

          `Route request failed (status ${response.status}).`
        );
      }

    


      // ------------------------------------------------
      // DRAW ROUTE
      // ------------------------------------------------

      drawRoute(
        data.route
      );


      // ------------------------------------------------
      // DISPLAY ROUTE STATISTICS
      // ------------------------------------------------

      statDistanceEl.textContent =
        `${data.distance_km.toFixed(2)} km`;


      statTimeEl.textContent =
        formatDuration(
          data.estimated_hours
        );


      statClimbEl.textContent =
        `${Math.round(data.climb_m)} m`;


      routeStatsEl.hidden =
        false;


      routeHintEl.textContent =
        'Route generated. Drag either pin to plan a new route.';


    } catch (error) {


      clearTimeout(
        timeoutId
      );


      const message =
        error.name ===
        'AbortError'

          ? 'The route request timed out. Try two points that are closer together.'

          : (
              error.message ||

              'Something went wrong generating the route.'
            );


      routeErrorEl.textContent =
        message;


      routeErrorEl.hidden =
        false;


      routeHintEl.textContent =
        'Ready — hit Generate Route to try again.';


      console.error(
        'Route generation error:',
        error
      );


    } finally {


      // ------------------------------------------------
      // ALWAYS REMOVE LOADING SCREEN
      // ------------------------------------------------

      hideLoadingScreen();


      routeLoading =
        false;

      

      generateRouteBtn.disabled =
        !(pointA && pointB);


      generateRouteBtn.textContent =
        'Generate Route';


      clearRouteBtn.disabled =
        false;


      placeStartBtn.disabled =
        false;


      placeEndBtn.disabled =
        false;


      if (markerA) {

        markerA.setDraggable(
          true
        );
      }


      if (markerB) {

        markerB.setDraggable(
          true
        );
      }
    }
  }
);


if (exportGPXBtn) {
  exportGPXBtn.addEventListener('click', downloadGPX);
}


// ========================================================================
// CLEAR ROUTE
// ========================================================================

clearRouteBtn.addEventListener(
  'click',
  () => {

    if (routeLoading) {
      return;
    }

    resetRoute();
  }
);


// ========================================================================
// LOADING SCREEN
// ========================================================================

function showLoadingScreen() {

  const loadingScreen =
    document.getElementById(
      'loading-screen'
    );


  if (loadingScreen) {

    loadingScreen.style.display =
      'flex';
  }
}


function hideLoadingScreen() {

  const loadingScreen =
    document.getElementById(
      'loading-screen'
    );


  if (loadingScreen) {

    loadingScreen.style.display =
      'none';
  }
}