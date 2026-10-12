// Top-down site view: the 2D map switched to aerial imagery and zoomed right into a site, with the
// cameras drawn as view cones and the same place/edit tools as the 3D view. It uses the same saved
// placements (sites.camera_places), so a camera placed here shows up in 3D and the other way round.
//
// Alarm safety: plain Leaflet (SVG shapes on the existing 2D map), no WebGL. A few dozen extra
// shapes cost the alarm path nothing measurable, so unlike the 3D view there's no graphics check or
// frame watchdog. Measured in tools/e2e/topdown.py.
import { CFG, esc } from "./common.js";

export const MAX_ZOOM = 21;          // imagery is sharpest at z19; z20-21 enlarge it for fine placing
const ORANGE = "#E08A30";
const M_PER_DEG = 111320, RAD = Math.PI / 180;
const LIMITS = { fov: [5, 360], range: [2, 500] };
const clampTo = (v, [lo, hi]) => Math.min(hi, Math.max(lo, v));
const cssVar = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;

// Flat-earth maths, fine over a few hundred metres (same as globe.js).
const offset = (lat, lng, bearingDeg, m) => [
  lat + (m * Math.cos(bearingDeg * RAD)) / M_PER_DEG,
  lng + (m * Math.sin(bearingDeg * RAD)) / (M_PER_DEG * Math.cos(lat * RAD)),
];
const distM = (a, b) => Math.hypot((b.lng - a.lng) * M_PER_DEG * Math.cos(a.lat * RAD), (b.lat - a.lat) * M_PER_DEG);
const bearing = (a, b) => (Math.atan2((b.lng - a.lng) * Math.cos(a.lat * RAD), b.lat - a.lat) / RAD + 360) % 360;
function cone(c) {                   // [[lat, lng], ...] outline of the view cone (a circle at 360°)
  const fov = Math.min(c.fov || 60, 360), r = c.range_m || 40, pts = [];
  if (fov < 359.5) pts.push([c.lat, c.lng]);
  const steps = Math.max(6, Math.round(fov / 6));
  for (let i = 0; i <= steps; i++) pts.push(offset(c.lat, c.lng, (c.heading || 0) - fov / 2 + (fov * i) / steps, r));
  return pts;
}

/**
 * Turn the Leaflet map `lmap` into the top-down view. Returns a controller; destroy() puts the
 * normal street map back.
 *   cameras: () => [{ siteId, deviceId, name, lat, lng, heading, fov, range_m, alarm }]
 *   onPickCamera(siteId, deviceId): a camera (its dot or cone) was clicked (optional)
 *   onHoverCamera(siteId, deviceId, containerPoint | null): the mouse went onto (or off) a camera's dot or cone
 * The editor API matches globe.js (editCamera, stopEditing, editing, currentEditor), so map.js
 * drives both views the same way.
 */
export function openPlan(lmap, { cameras, onPickCamera, onHoverCamera }) {
  const cfg = CFG.map || {};
  const base = [];
  lmap.eachLayer((l) => { if (l instanceof L.TileLayer) base.push(l); });
  base.forEach((l) => l.remove());
  const layerFor = (kind) => (kind === "street"
    ? L.tileLayer(cfg.tileUrl, { maxNativeZoom: cfg.maxZoom || 19, maxZoom: MAX_ZOOM, attribution: cfg.attribution })
    : L.tileLayer(cfg.satUrl, { maxNativeZoom: cfg.satMaxZoom || 19, maxZoom: MAX_ZOOM, attribution: cfg.satAttribution }));
  let imagery = layerFor("sat").addTo(lmap);
  lmap.getContainer().classList.add("plan-sat");      // photos aren't dark-mode inverted like the street map

  if (!lmap.getPane("planCones")) lmap.createPane("planCones").style.zIndex = 450;   // above tiles, below pins
  const group = L.layerGroup().addTo(lmap);
  const red = () => cssVar("--red", "#C0392B");
  let editor = null, editKey = null;
  let showCones = true;      // setCones(false) hides view cones; a camera with an open alarm keeps its cone

  // ---- placed cameras: a cone and a dot with the name under it
  const drawn = new Map();   // "site:device" -> { cone, marker, sig }
  function update() {
    const seen = new Set();
    for (const c of cameras()) {
      if (c.lat == null || c.lng == null) continue;
      const key = `${c.siteId}:${c.deviceId}`;
      seen.add(key);
      let d = drawn.get(key);
      if (!d) {
        const pick = () => { if (!editor || editor.mode === "edit") onPickCamera?.(c.siteId, c.deviceId); };
        const over = (e) => { if (!editor) onHoverCamera?.(c.siteId, c.deviceId, e.containerPoint); };
        const out = () => onHoverCamera?.(c.siteId, c.deviceId, null);
        d = {
          cone: L.polygon([], { pane: "planCones", weight: 2 }).on("click", pick).on("mouseover", over).on("mouseout", out),
          marker: L.marker([c.lat, c.lng], { zIndexOffset: 5000, keyboard: true }).on("click", pick).on("mouseover", over).on("mouseout", out),
          sig: "",
        };
        drawn.set(key, d);
      }
      const hide = key === editKey;   // being edited: the editor draws it
      const coneOn = !hide && (showCones || !!c.alarm);
      const sig = JSON.stringify([c.lat, c.lng, c.heading, c.fov, c.range_m, c.alarm, c.name, hide, coneOn]);
      if (sig === d.sig) continue;
      d.sig = sig;
      const tint = c.alarm ? red() : ORANGE;
      d.cone.setLatLngs(cone(c)).setStyle({ color: tint, fillColor: tint, fillOpacity: c.alarm ? 0.45 : 0.28, weight: c.alarm ? 3 : 2 });
      d.marker.setLatLng([c.lat, c.lng]).setIcon(L.divIcon({
        className: `plan-cam${c.alarm ? " plan-cam--alarm" : ""}`, iconSize: [14, 14], iconAnchor: [7, 7],
        html: `<i></i><span>${c.alarm ? "ALARM · " : ""}${esc(c.name || "Camera")}</span>`,
      }));
      for (const [l, on] of [[d.cone, coneOn], [d.marker, !hide]]) {
        if (!on && group.hasLayer(l)) group.removeLayer(l);
        if (on && !group.hasLayer(l)) group.addLayer(l);
      }
      d.cone.getElement()?.classList.toggle("plan-cone--alarm", !!c.alarm);   // flashes (theme.css)
    }
    for (const [key, d] of drawn) {
      if (seen.has(key)) continue;
      group.removeLayer(d.cone); group.removeLayer(d.marker);
      drawn.delete(key);
    }
  }

  // ---- moving the view: the site's placed cameras if it has any, else the site itself
  function flyTo(site) {
    if (site?.lat == null) return;
    const pts = cameras().filter((c) => c.siteId === site.id && c.lat != null).flatMap(cone);
    if (pts.length) lmap.flyToBounds(L.latLngBounds(pts), { padding: [40, 40], maxZoom: 20, duration: 0.8 });
    else lmap.flyTo([site.lat, site.lng], 19, { duration: 0.8 });
  }

  // Fit one camera's cone in view (the alarm details map). Unplaced: the site itself.
  function focusCamera(site, deviceId) {
    const c = cameras().find((x) => x.siteId === site.id && x.deviceId === deviceId && x.lat != null);
    if (c) lmap.fitBounds(L.latLngBounds(cone(c)), { padding: [30, 30], maxZoom: 20, animate: false });
    else if (site.lat != null) lmap.setView([site.lat, site.lng], 18, { animate: false });
    return !!c;
  }

  function setImagery(kind) {
    imagery.remove();
    imagery = layerFor(kind).addTo(lmap);
    imagery.bringToBack();
    lmap.getContainer().classList.toggle("plan-sat", kind !== "street");
  }

  // ---- camera editor (admins). Same steps as the 3D view: not placed yet, click its spot, then
  // move the mouse to aim it (direction and distance follow) and click. Placed: drag the handles.
  // White centre moves it, the orange tip aims it and sets the distance, the yellow sides set the
  // width (any angle up to a 360° dome). Every finished change goes to onCommit (the caller saves).
  function editCamera(cam, { onChange, onCommit, onMode } = {}) {
    stopEditing();
    editKey = `${cam.siteId}:${cam.deviceId}`;
    const st = { heading: 0, fov: 60, range_m: 30, ...cam };
    let mode = st.lat == null ? "spot" : "edit";
    const ready = () => st.lat != null;
    const shape = L.polygon([], { pane: "planCones", interactive: false, color: ORANGE, weight: 2, fillColor: "#fff", fillOpacity: 0.3 }).addTo(lmap);
    const where = {
      move: () => [st.lat, st.lng],
      aim: () => offset(st.lat, st.lng, st.heading, st.range_m),
      left: () => offset(st.lat, st.lng, st.heading - Math.min(st.fov, 359) / 2, st.range_m * 0.8),
      right: () => offset(st.lat, st.lng, st.heading + Math.min(st.fov, 359) / 2, st.range_m * 0.8),
    };
    const TITLES = { move: "Drag to move", aim: "Drag to aim and set the distance", left: "Drag to change the width", right: "Drag to change the width" };
    const handles = Object.fromEntries(Object.keys(where).map((k) => [k, L.marker([0, 0], {
      draggable: true, keyboard: false, zIndexOffset: 9000, title: TITLES[k],
      icon: L.divIcon({ className: `plan-handle ${k}`, iconSize: [18, 18], iconAnchor: [9, 9] }),
    })]));

    function draw(except) {
      shape.setLatLngs(ready() ? cone(st) : []);
      const show = ready() && mode === "edit";
      for (const [k, h] of Object.entries(handles)) {
        if (show && !lmap.hasLayer(h)) h.addTo(lmap);
        if (!show && lmap.hasLayer(h)) h.remove();
        if (show && k !== except) h.setLatLng(where[k]());
      }
    }
    const setMode = (m) => {
      mode = m;
      lmap.getContainer().classList.toggle("plan-placing", m !== "edit");
      draw(); onMode?.(m, { ...st });
    };
    const changed = (except) => { draw(except); onChange?.({ ...st }); };
    const commit = () => onCommit?.({ lat: st.lat, lng: st.lng, heading: Math.round(st.heading * 10) / 10 % 360,
                                      fov: Math.round(st.fov), range_m: Math.round(st.range_m * 10) / 10 });
    const aimAt = (pt) => { st.heading = bearing(st, pt); st.range_m = clampTo(distM(st, pt), LIMITS.range); };

    const onMove = (e) => { if (mode === "aim") { aimAt(e.latlng); changed(); } };
    const onClick = (e) => {
      if (mode === "spot") { st.lat = e.latlng.lat; st.lng = e.latlng.lng; setMode("aim"); changed(); }
      else if (mode === "aim") { aimAt(e.latlng); setMode("edit"); commit(); }     // a tap aims too (touch screens)
    };
    lmap.on("click", onClick).on("mousemove", onMove);
    for (const [k, h] of Object.entries(handles)) {
      h.on("drag", () => {
        const pt = h.getLatLng();
        if (k === "move") { st.lat = pt.lat; st.lng = pt.lng; }
        else if (k === "aim") aimAt(pt);
        else {
          const diff = Math.abs(((bearing(st, pt) - st.heading + 540) % 360) - 180);    // 0..180 from the centre line
          st.fov = clampTo(2 * diff, LIMITS.fov);
          if (st.fov > 350) st.fov = 360;
          st.range_m = clampTo(distM(st, pt) / 0.8, LIMITS.range);
        }
        changed(k);
      });
      h.on("dragend", () => { draw(); commit(); });
    }

    editor = {
      set(values) { Object.assign(st, values); changed(); if (mode === "edit") commit(); },
      stop() {
        lmap.off("click", onClick).off("mousemove", onMove);
        lmap.getContainer().classList.remove("plan-placing");
        shape.remove(); Object.values(handles).forEach((h) => h.remove());
      },
      get mode() { return mode; },
      get state() { return { ...st }; },
      handles,   // for tests
    };
    setMode(mode);
    update();
    return editor;
  }
  function stopEditing() {
    if (!editor) return;
    editor.stop();
    editor = null; editKey = null;
    update();
  }

  update();
  return {
    update, flyTo, focusCamera, setImagery, editCamera, stopEditing,
    setCones(on) { showCones = !!on; update(); },
    get editing() { return editor ? { key: editKey, mode: editor.mode } : null; },
    currentEditor: () => editor,
    destroy() {
      stopEditing();
      group.remove(); imagery.remove();
      lmap.getContainer().classList.remove("plan-sat");
      base.forEach((l) => l.addTo(lmap));
    },
  };
}
