// ── CONFIG ───────────────────────────────────────────────────────────────
  // No LINZ key here anymore — it lives server-side in Secrets Manager.
  const API_BASE = 'https://k3w7aj90ok.execute-api.ap-southeast-2.amazonaws.com';
  const LINZ_ORIGIN = 'https://basemaps.linz.govt.nz/v1/';
  // ─────────────────────────────────────────────────────────────────────────

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
      url: ${API_BASE}/proxy/${path}${parsed.search}
    };
  }

  return { url };
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
