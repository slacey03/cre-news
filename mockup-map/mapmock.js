// MOCKUP ONLY: a map added to the dashboard. Pins = projects whose exact street address the Census geocoder matched.
(function () {
  const COLORS = { commercial: '#1F3A5F', industrial: '#C98A1B', residential: '#2d5a3d', other: '#7A828C' };
  const LABEL = { commercial: 'Commercial', industrial: 'Industrial', residential: 'Residential', other: 'Institutional / other' };
  const groupOf = c => Object.keys(GROUPS).find(k => GROUPS[k].includes(c)) || 'other';
  const st = document.createElement('style');
  st.textContent = `
  .map-panel { border: 1px solid var(--rule); border-radius: 4px; margin-bottom: 16px; overflow: hidden; background: #fff; }
  .map-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 10px 14px; border-bottom: 1px solid var(--rule); }
  .map-head .mk { font-family: 'DM Mono', monospace; font-size: 11px; letter-spacing: .12em; text-transform: uppercase; color: var(--ink); font-weight: 600; }
  .map-head .mc { font-size: 13px; color: var(--ink-faded); margin-left: 8px; }
  .seg { display: inline-flex; border: 1px solid var(--rule); border-radius: 3px; overflow: hidden; }
  .seg button { font-family: 'DM Mono', monospace; font-size: 11px; letter-spacing: .1em; text-transform: uppercase; padding: 7px 14px; border: none; background: #fff; color: var(--ink-soft); cursor: pointer; }
  .seg button.on { background: var(--third); color: #fff; }
  #map { height: 470px; }
  .map-legend { display: flex; flex-wrap: wrap; gap: 6px 18px; padding: 10px 14px; border-top: 1px solid var(--rule); font-size: 12px; color: var(--ink-soft); align-items: center; }
  .map-legend i { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: -1px; border: 1.5px solid #fff; box-shadow: 0 0 0 1px rgba(0,0,0,.15); }
  .map-legend .note { margin-left: auto; color: var(--ink-faded); }
  .leaflet-popup-content { margin: 12px 14px; font-family: 'DM Sans', sans-serif; }
  .pp .pt { font-family: 'DM Mono', monospace; font-size: 10px; letter-spacing: .12em; text-transform: uppercase; color: var(--ink-faded); margin-bottom: 4px; }
  .pp b { font-family: 'Playfair Display', serif; font-size: 15px; line-height: 1.25; display: block; margin-bottom: 4px; color: var(--ink); }
  .pp .pa { font-size: 12px; color: var(--ink-soft); }
  .pp .pc { font-size: 11px; color: var(--ink-faded); margin: 6px 0 8px; text-transform: capitalize; }
  .pp a { color: var(--third); font-weight: 600; font-size: 12px; text-decoration: none; }
  body.list-only .map-panel #map, body.list-only .map-legend { display: none; }
  @media (max-width: 768px) {
    #map { height: 62vh; }
    body:not(.view-map) #map, body:not(.view-map) .map-legend { display: none; }
    body.view-map .workspace { display: none; }
    .map-legend .note { margin-left: 0; width: 100%; }
    .map-head .mc { display: block; margin: 2px 0 0; font-size: 12px; }
  }`;
  document.head.appendChild(st);
  const panel = document.createElement('div');
  panel.className = 'map-panel';
  panel.innerHTML = `<div class="map-head"><div><span class="mk">Project map</span><span class="mc" id="map-count"></span></div>
    <div class="seg"><button data-v="list">List</button><button data-v="map" class="on">Map</button></div></div>
    <div id="map"></div>
    <div class="map-legend">${Object.keys(COLORS).map(k => `<span><i style="background:${COLORS[k]}"></i>${LABEL[k]}</span>`).join('')}
    <span class="note">Pins only where the exact street address was matched</span></div>`;
  const ws = document.querySelector('.workspace');
  ws.parentNode.insertBefore(panel, ws);
  const isPh = () => window.matchMedia('(max-width: 768px)').matches;
  const seg = panel.querySelector('.seg');
  const setView = v => {
    seg.querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.v === v));
    document.body.classList.toggle('view-map', v === 'map');
    document.body.classList.toggle('list-only', v === 'list' && !isPh());
    setTimeout(() => map.invalidateSize(), 50);
  };
  seg.addEventListener('click', e => { const b = e.target.closest('button'); if (b) setView(b.dataset.v); });
  if (isPh()) setView('list'); else setView('map');

  const map = L.map('map', { scrollWheelZoom: false, zoomControl: true }).setView([40.65, -75.43], 10);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
    { attribution: '&copy; OpenStreetMap contributors &copy; CARTO', subdomains: 'abcd', maxZoom: 19 }).addTo(map);
  const layer = L.layerGroup().addTo(map);
  window.__markers = {};
  let fitted = false;
  function draw() {
    layer.clearLayers(); window.__markers = {};
    const list = ROWS.filter(matches), pinned = list.filter(r => r.lat != null);
    document.getElementById('map-count').textContent = `${pinned.length} of ${list.length} projects pinned`;
    pinned.sort((a, b) => (a.id === activeId) - (b.id === activeId)).forEach(r => {
      const g = groupOf(r.category), on = r.id === activeId;
      const m = L.circleMarker([r.lat, r.lon], { radius: on ? 11 : 7, color: on ? '#111417' : '#fff', weight: on ? 3 : 2, fillColor: COLORS[g], fillOpacity: .95 });
      m.bindPopup(`<div class="pp"><div class="pt">${esc(r.municipality)}</div><b>${esc(r.name)}</b><div class="pa">${esc(r.address || '')}</div>
        <div class="pc">${esc(r.category)} · ${esc(r.stage || '')}</div><a href="#" onclick="selectItem('${r.id}');return false">View project →</a></div>`, { maxWidth: 260 });
      m.on('click', () => { if (!isPh()) { selectItem(r.id); setTimeout(() => { window.__markers[r.id] && window.__markers[r.id].openPopup(); document.querySelector('.item.active')?.scrollIntoView({ block: 'nearest' }); }, 30); } });
      layer.addLayer(m); window.__markers[r.id] = m;
    });
    if (!fitted && pinned.length) { map.fitBounds(L.latLngBounds(pinned.map(r => [r.lat, r.lon])), { padding: [24, 24] }); fitted = true; }
  }
  const orig = renderFeed;
  renderFeed = function () { orig(); draw(); };
  window.__map = map;
})();
