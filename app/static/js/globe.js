// 3D site view (CesiumJS), opened from the map page's site panel. Loaded only when asked for:
// nothing here runs, and Cesium isn't downloaded, until someone clicks "3D view".
//
// Alarm safety: this module only draws. The alarm feed, sound, pop-ups and the live connection
// live in common.js / console.js and don't depend on it, but they share the page's main thread:
// a slow 3D frame delays everything queued behind it, alarms included. So:
//  - screens without graphics acceleration (software WebGL) don't get the 3D view at all;
//    measured on one: frames of 0.5-3 s, alarms up to 0.8 s late while it loaded;
//  - a watchdog times every frame and closes the view (onTooSlow) if frames get slow;
//  - rendering is on demand (requestRenderMode): an idle 3D view draws nothing.
// If WebGL or Cesium fails, the caller gets an exception and stays on the 2D map.
import { CFG } from "./common.js";

const BASE = new URL("../vendor/cesium/", import.meta.url).href;   // versioned, like every asset
let loading = null;

function loadCesium() {
  if (window.Cesium) return Promise.resolve(window.Cesium);
  if (loading) return loading;
  window.CESIUM_BASE_URL = BASE;
  const css = document.createElement("link");
  css.rel = "stylesheet"; css.href = `${BASE}Widgets/widgets.css`;
  document.head.append(css);
  loading = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = `${BASE}Cesium.js`;
    s.onload = () => resolve(window.Cesium);
    s.onerror = () => { loading = null; s.remove(); reject(new Error("Couldn't load the 3D library")); };
    document.head.append(s);
  });
  return loading;
}

// Frame watchdog limits. One frame this long, or STRIKES slow frames within STRIKE_WINDOW_MS,
// closes the view. The first frames (shader compiling) are let off.
const FRAME_MAX_MS = 1000, FRAME_SLOW_MS = 150, STRIKES = 3, STRIKE_WINDOW_MS = 10000, GRACE_FRAMES = 2;
const SOFTWARE_GL = /swiftshader|llvmpipe|softpipe|software|basic render|mesa offscreen/i;

// Test switch (localStorage twg-3d-force): "1" skips the software-graphics check, "2" also turns
// the frame watchdog off so headless browsers can exercise the UI. Never set on real screens.
const forceLevel = () => { try { return Number(localStorage.getItem("twg-3d-force")) || 0; } catch { return 0; } };

/** Why this screen can't have the 3D view, or "" if it can. */
export function graphicsProblem() {
  const force = forceLevel() >= 1;
  const gl = document.createElement("canvas").getContext("webgl2") || document.createElement("canvas").getContext("webgl");
  if (!gl) return "this browser has no WebGL";
  const info = gl.getExtension("WEBGL_debug_renderer_info");
  const renderer = String(gl.getParameter(info ? info.UNMASKED_RENDERER_WEBGL : gl.RENDERER) || "");
  gl.getExtension("WEBGL_lose_context")?.loseContext();
  if (SOFTWARE_GL.test(renderer) && !force) return "this screen has no graphics acceleration, and 3D would slow down alarms";
  return "";
}

const cssVar = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;

/**
 * Open the 3D view in `el`. Returns a controller, or throws (no WebGL, software-only graphics,
 * library didn't load).
 *   pins: () => [{ id, name, lat, lng, state: "alarm"|"warn"|"offline"|"ok", count, disarmed }]
 *   cameras: () => [{ siteId, deviceId, name, lat, lng, heading, fov, range_m, alarm }]
 *   models: () => [{ siteId, url, anchor, placement: { lat, lng, height, heading, scale } }]: site 3D models (3D Tiles)
 *   guards: () => [{ id, name, lat, lng, speed, fresh: "live"|"late"|"stale"|"none", trail: [[lat, lng], ...] }]
 *   onPick(siteId): a site pin was clicked; onPickCamera(siteId, deviceId): a camera was clicked
 *   onView({ lat, lng, alt, heading }): the view stopped moving (for the HUD readout)
 *   onTooSlow(reason): frames are slow enough to delay alarms; the caller should close the view
 */
export async function openGlobe(el, { pins, cameras = () => [], models = () => [], guards = () => [], onPick, onPickCamera, onView, onTooSlow }) {
  const problem = graphicsProblem();
  if (problem) throw new Error(problem);
  const Cesium = await loadCesium();
  const map = CFG.map || {};
  const layerFor = (kind) => new Cesium.ImageryLayer(new Cesium.UrlTemplateImageryProvider(kind === "street"
    ? { url: map.tileUrl, maximumLevel: map.maxZoom || 19, credit: new Cesium.Credit(map.attribution || "", true) }
    : { url: map.satUrl, maximumLevel: map.satMaxZoom || 19, credit: new Cesium.Credit(map.satAttribution || "", true) }));

  const viewer = new Cesium.Viewer(el, {
    baseLayer: layerFor("sat"),
    // Everything that would call Cesium ion (their paid cloud) or isn't needed is off.
    baseLayerPicker: false, geocoder: false, homeButton: false, sceneModePicker: false,
    navigationHelpButton: false, animation: false, timeline: false, fullscreenButton: false,
    infoBox: false, selectionIndicator: false, vrButton: false,
    requestRenderMode: true, maximumRenderTimeChange: Infinity, msaaSamples: 1,
  });
  const scene = viewer.scene;
  scene.globe.maximumScreenSpaceError = 3;   // a little fewer, coarser tiles (default 2)

  // Watchdog: time each frame (camera input + draw) where Cesium's render loop calls it.
  const widget = viewer.cesiumWidget, render = widget.render.bind(widget);
  let frames = 0, strikes = [], tripped = false;
  const noWatchdog = forceLevel() >= 2;
  const stats = { frames: 0, slow: 0, maxMs: 0 };
  widget.render = () => {
    const t0 = performance.now();
    render();
    const ms = performance.now() - t0;
    if (++frames <= GRACE_FRAMES || tripped || ms < 1 || noWatchdog) return;
    stats.frames++; stats.maxMs = Math.max(stats.maxMs, Math.round(ms));
    if (ms < FRAME_SLOW_MS) return;
    stats.slow++;
    const now = performance.now();
    strikes = strikes.filter((t) => now - t < STRIKE_WINDOW_MS).concat(now);
    if (ms >= FRAME_MAX_MS || strikes.length >= STRIKES) {
      tripped = true;
      setTimeout(() => onTooSlow?.(`3D frames took up to ${Math.round(ms)} ms on this screen`), 0);
    }
  };
  scene.globe.enableLighting = false;
  scene.globe.baseColor = Cesium.Color.fromCssColorString("#2B2B2B");   // shown where imagery hasn't loaded yet (default is bright blue)
  scene.globe.showGroundAtmosphere = true;
  scene.fog.enabled = true;
  viewer.cesiumWidget.creditContainer.classList.add("globe-credits");

  // Real 3D buildings, trees and terrain when a Google Map Tiles key is configured. They replace
  // the flat globe; "Street" switches back to the globe with the street map draped on it.
  let google = null;
  const ground = new Map();       // "lat,lng" -> ground height (m above the ellipsoid) under Google's tiles
  if (map.google3dKey) {
    try {
      google = await Cesium.createGooglePhotorealistic3DTileset({ key: map.google3dKey });
      google.maximumScreenSpaceError = 24;      // default 16; fewer tiles for the same look from a few hundred metres
      scene.primitives.add(google);
      scene.globe.show = false;
    } catch (err) {
      console.error("Google 3D tiles unavailable; using the flat aerial view", err);
      google = null;
    }
  }
  const clamp = google ? Cesium.HeightReference.CLAMP_TO_3D_TILE : Cesium.HeightReference.CLAMP_TO_GROUND;

  // ---- HUD-style colours: TWG orange for the overlay, state colours for pins and cones
  const ORANGE = Cesium.Color.fromCssColorString("#E08A30"), INK = Cesium.Color.fromCssColorString("#0D0D0D");
  const color = (state) => Cesium.Color.fromCssColorString(
    { alarm: cssVar("--red", "#C0392B"), warn: cssVar("--warn", "#D9A21B"), ok: cssVar("--ok", "#2FA36B") }[state] || cssVar("--offline", "#767676"));
  const hudLabel = (text, extra = {}) => ({
    text, font: "600 13px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace", fillColor: Cesium.Color.WHITE,
    showBackground: true, backgroundColor: INK.withAlpha(0.78), backgroundPadding: new Cesium.Cartesian2(8, 5),
    verticalOrigin: Cesium.VerticalOrigin.BOTTOM, heightReference: clamp,
    disableDepthTestDistance: Number.POSITIVE_INFINITY, ...extra,
  });

  // ---- site pins
  const sitesE = new Map();   // site id -> entity
  function updatePins() {
    const seen = new Set();
    for (const p of pins()) {
      if (p.lat == null || p.lng == null) continue;
      seen.add(p.id);
      const text = `${p.name.toUpperCase()}${p.count ? `  [${p.count}]` : ""}${p.disarmed ? "  DISARMED" : ""}`;
      const size = p.state === "alarm" ? 20 : 14;
      let e = sitesE.get(p.id);
      if (!e) {
        e = viewer.entities.add({
          point: { outlineColor: Cesium.Color.WHITE, outlineWidth: 3, heightReference: clamp, disableDepthTestDistance: Number.POSITIVE_INFINITY },
          label: hudLabel(text, { pixelOffset: new Cesium.Cartesian2(0, -16) }),
        });
        e.siteId = p.id;
        sitesE.set(p.id, e);
      }
      e.position = Cesium.Cartesian3.fromDegrees(p.lng, p.lat, 0);
      e.point.color = color(p.state);
      e.point.pixelSize = size;
      e.label.text = text;
      e.label.fillColor = p.state === "alarm" ? color("alarm").brighten(0.4, new Cesium.Color()) : Cesium.Color.WHITE;
    }
    for (const [id, e] of sitesE) if (!seen.has(id)) { viewer.entities.remove(e); sitesE.delete(id); }
  }

  // ---- cameras: a marker, a name tag and a view cone on the ground
  const M_PER_DEG = 111320;
  const offset = (lat, lng, bearingDeg, m) => {
    const b = Cesium.Math.toRadians(bearingDeg);
    return [lng + (m * Math.sin(b)) / (M_PER_DEG * Math.cos(Cesium.Math.toRadians(lat))), lat + (m * Math.cos(b)) / M_PER_DEG];
  };
  function coneLngLat(c) {          // flat [lng, lat, lng, lat, ...] outline of the view cone
    const fov = Math.min(c.fov || 60, 360), r = c.range_m || 40, pts = [];
    if (fov < 359.5) pts.push(c.lng, c.lat);
    const steps = Math.max(6, Math.round(fov / 6));
    for (let i = 0; i <= steps; i++) pts.push(...offset(c.lat, c.lng, (c.heading || 0) - fov / 2 + (fov * i) / steps, r));
    return pts;
  }
  const conePositions = (c) => Cesium.Cartesian3.fromDegreesArray(coneLngLat(c));
  const distM = (a, b) => Math.hypot((b.lng - a.lng) * M_PER_DEG * Math.cos(Cesium.Math.toRadians(a.lat)), (b.lat - a.lat) * M_PER_DEG);
  const bearing = (a, b) => (Cesium.Math.toDegrees(Math.atan2((b.lng - a.lng) * Math.cos(Cesium.Math.toRadians(a.lat)), b.lat - a.lat)) + 360) % 360;
  const camsE = new Map();    // "site:device" -> { marker, cone }
  function updateCameras() {
    const seen = new Set();
    for (const c of cameras()) {
      const key = `${c.siteId}:${c.deviceId}`;
      seen.add(key);
      const hide = key === editKey;   // being edited: the editor draws it
      const tint = c.alarm ? color("alarm") : ORANGE;
      let e = camsE.get(key);
      if (!e) {
        e = {
          marker: viewer.entities.add({
            point: { pixelSize: 10, outlineColor: Cesium.Color.WHITE, outlineWidth: 2, heightReference: clamp, disableDepthTestDistance: Number.POSITIVE_INFINITY },
            label: hudLabel("", { pixelOffset: new Cesium.Cartesian2(0, -12), font: "600 11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
                                  distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 2500) }),
          }),
          cone: viewer.entities.add({ polygon: { classificationType: Cesium.ClassificationType.BOTH } }),
          edge: viewer.entities.add({ polyline: { clampToGround: true, width: 2, classificationType: Cesium.ClassificationType.BOTH } }),
        };
        e.marker.camera = e.cone.camera = e.edge.camera = { siteId: c.siteId, deviceId: c.deviceId };
        camsE.set(key, e);
      }
      e.marker.position = Cesium.Cartesian3.fromDegrees(c.lng, c.lat, 0);
      e.marker.point.color = tint;
      e.marker.point.pixelSize = c.alarm ? 14 : 10;
      e.marker.label.text = `${c.alarm ? "ALARM  " : ""}${(c.name || "Camera").toUpperCase()}`;
      e.marker.label.fillColor = c.alarm ? color("alarm").brighten(0.4, new Cesium.Color()) : Cesium.Color.WHITE;
      const ring = conePositions(c);
      e.cone.polygon.hierarchy = new Cesium.PolygonHierarchy(ring);
      e.cone.polygon.material = tint.withAlpha(c.alarm ? 0.5 : 0.32);
      e.edge.polyline.positions = [...ring, ring[0]];
      e.edge.polyline.material = tint;
      e.edge.polyline.width = c.alarm ? 3 : 2;
      e.marker.show = e.cone.show = e.edge.show = !hide;
    }
    for (const [key, e] of camsE) {
      if (seen.has(key)) continue;
      for (const part of [e.marker, e.cone, e.edge]) viewer.entities.remove(part);
      camsE.delete(key);
    }
  }

  // ---- site 3D models (3D Tiles from app/routers/site_models.py). Content is in metres, east/north/up
  // around the model's floor (converted models), or on the Earth already (georeferenced 3D Tiles: then
  // `anchor` is where its floor is, and the placement moves that spot). Height is above the ground.
  const modelsE = new Map();     // site id -> { url, tileset, placement }
  let modelsShown = true, modelEdit = null;    // modelEdit = { siteId, st }
  const enu = (lat, lng, h) => Cesium.Transforms.eastNorthUpToFixedFrame(Cesium.Cartesian3.fromDegrees(lng, lat, h));
  function modelMatrix(p, anchor, groundH = 0) {
    const M = Cesium.Matrix4;
    let m = M.multiply(enu(p.lat, p.lng, groundH + (p.height || 0)),
      M.fromRotation(Cesium.Matrix3.fromRotationZ(-Cesium.Math.toRadians(p.heading || 0))), new M());
    m = M.multiply(m, M.fromUniformScale(p.scale || 1), m);
    if (anchor) m = M.multiply(m, M.inverseTransformation(enu(anchor.lat, anchor.lng, anchor.h || 0), new M()), m);
    return m;
  }
  async function placeModel(siteId) {
    const e = modelsE.get(siteId);
    if (!e?.tileset) return;
    const p = modelEdit?.siteId === siteId ? modelEdit.st : e.placement;
    const known = google ? ground.get(`${p.lat.toFixed(5)},${p.lng.toFixed(5)}`) : 0;
    e.tileset.modelMatrix = modelMatrix(p, e.anchor, known ?? 0);
    scene.requestRender();
    if (google && known == null) {
      const h = await groundHeight(p.lat, p.lng);
      const now = modelEdit?.siteId === siteId ? modelEdit.st : modelsE.get(siteId)?.placement;
      if (now && modelsE.get(siteId)?.tileset) { modelsE.get(siteId).tileset.modelMatrix = modelMatrix(now, e.anchor, h); scene.requestRender(); }
    }
  }
  function updateModels() {
    const seen = new Set();
    for (const m of models()) {
      seen.add(m.siteId);
      let e = modelsE.get(m.siteId);
      if (e && e.url !== m.url) { if (e.tileset) scene.primitives.remove(e.tileset); modelsE.delete(m.siteId); e = null; }
      if (!e) {
        e = { url: m.url, anchor: m.anchor, placement: m.placement, tileset: null };
        modelsE.set(m.siteId, e);
        // Converted models have levels of detail (app/services/model_lod.py): from afar only a coarse tile loads.
        // The cache holds a close-up's tiles, so moving around doesn't evict and reload them.
        Cesium.Cesium3DTileset.fromUrl(m.url, { maximumScreenSpaceError: 16, cacheBytes: 768 * 1024 * 1024,
                                                maximumCacheOverflowBytes: 512 * 1024 * 1024 }).then((ts) => {
          if (modelsE.get(m.siteId) !== e || viewer.isDestroyed()) { ts.destroy(); return; }
          e.tileset = ts;
          ts.show = modelsShown;
          ts.tileLoad.addEventListener(() => scene.requestRender());
          scene.primitives.add(ts);
          placeModel(m.siteId);
        }).catch((err) => { console.error("site model didn't load", err); e.failed = String(err?.message || err); });
      } else {
        e.anchor = m.anchor;
        if (JSON.stringify(e.placement) !== JSON.stringify(m.placement)) { e.placement = m.placement; placeModel(m.siteId); }
      }
    }
    for (const [id, e] of modelsE) if (!seen.has(id)) { if (e.tileset) scene.primitives.remove(e.tileset); modelsE.delete(id); }
  }
  function showModels(on) {
    modelsShown = on;
    for (const e of modelsE.values()) if (e.tileset) e.tileset.show = on;
    scene.requestRender();
  }
  function flyToModel(siteId) {
    const ts = modelsE.get(siteId)?.tileset;
    if (ts) viewer.camera.flyToBoundingSphere(ts.boundingSphere, { offset: new Cesium.HeadingPitchRange(0, Cesium.Math.toRadians(-35), ts.boundingSphere.radius * 2.6), duration: 1.2 });
  }

  // ---- guards / patrol vehicles: a dot + name tag and their recent trail, both on the ground
  const guardsE = new Map();      // id -> { dot, trail }
  const GUARD_COLOR = { live: Cesium.Color.fromCssColorString("#2FA36B"), late: Cesium.Color.fromCssColorString("#D9A21B") };
  function updateGuards() {
    const seen = new Set();
    for (const g of guards()) {
      if (g.lat == null) continue;
      seen.add(g.id);
      let e = guardsE.get(g.id);
      if (!e) {
        e = {
          dot: viewer.entities.add({
            point: { pixelSize: 13, outlineColor: Cesium.Color.WHITE, outlineWidth: 3, heightReference: clamp, disableDepthTestDistance: Number.POSITIVE_INFINITY },
            label: hudLabel("", { pixelOffset: new Cesium.Cartesian2(0, -14), font: "600 12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" }),
          }),
          trail: viewer.entities.add({ polyline: { clampToGround: true, width: 3, material: ORANGE.withAlpha(0.8), classificationType: Cesium.ClassificationType.BOTH } }),
        };
        guardsE.set(g.id, e);
      }
      const col = GUARD_COLOR[g.fresh] || Cesium.Color.fromCssColorString("#8a8a8a");
      e.dot.position = Cesium.Cartesian3.fromDegrees(g.lng, g.lat, 0);
      e.dot.point.color = col;
      e.dot.label.text = `${g.name.toUpperCase()}${g.speed != null && g.speed * 2.23694 >= 1 ? `  ${Math.round(g.speed * 2.23694)} MPH` : ""}`;
      const pts = (g.trail || []).slice(-200);
      e.trail.show = pts.length > 1;
      if (pts.length > 1) e.trail.polyline.positions = Cesium.Cartesian3.fromDegreesArray(pts.flatMap(([la, lo]) => [lo, la]));
    }
    for (const [id, e] of guardsE) if (!seen.has(id)) { viewer.entities.remove(e.dot); viewer.entities.remove(e.trail); guardsE.delete(id); }
  }

  // Ground-draped shapes are built in the background and can finish after the frame that asked
  // for them; with on-demand rendering nothing would redraw, so ask a few more times.
  let settle = null;
  function update() {
    updatePins(); updateCameras(); updateModels(); updateGuards(); scene.requestRender();
    clearInterval(settle);
    let n = 0;
    settle = setInterval(() => { scene.requestRender(); if (++n >= 6) clearInterval(settle); }, 250);
  }

  // ---- clicks: pick a pin or camera; while editing, the editor gets the mouse
  const toDeg = Cesium.Math.toDegrees;
  function groundAt(pos) {
    // Flat globe: the ellipsoid is the ground. Google's tiles: the depth under the mouse.
    let c = google && scene.pickPositionSupported ? scene.pickPosition(pos) : undefined;
    if (!c) c = viewer.camera.pickEllipsoid(pos, scene.globe.ellipsoid);
    if (!c) return null;
    const g = Cesium.Cartographic.fromCartesian(c);
    return { lat: toDeg(g.latitude), lng: toDeg(g.longitude) };
  }
  let editKey = null, editor = null;     // the camera being placed or edited
  const input = new Cesium.ScreenSpaceEventHandler(scene.canvas);
  input.setInputAction((click) => {
    if (editor?.click(click.position)) return;
    const hit = scene.pick(click.position)?.id;
    if (hit?.camera) onPickCamera?.(hit.camera.siteId, hit.camera.deviceId);
    else if (hit?.siteId != null) onPick?.(hit.siteId);
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
  input.setInputAction((move) => editor?.move(move.endPosition), Cesium.ScreenSpaceEventType.MOUSE_MOVE);
  input.setInputAction((down) => editor?.down(down.position), Cesium.ScreenSpaceEventType.LEFT_DOWN);
  input.setInputAction(() => editor?.up(), Cesium.ScreenSpaceEventType.LEFT_UP);

  // ---- HUD readout: where the view is centred, when it stops moving
  viewer.camera.moveEnd.addEventListener(() => {
    if (!onView) return;
    const centre = groundAt(new Cesium.Cartesian2(scene.canvas.clientWidth / 2, scene.canvas.clientHeight / 2));
    const carto = viewer.camera.positionCartographic;
    onView({ ...(centre || { lat: toDeg(carto.latitude), lng: toDeg(carto.longitude) }), alt: carto.height,
             heading: (toDeg(viewer.camera.heading) + 360) % 360 });
  });

  // ---- flying: start in space above the site, then dive in (the "god view")
  async function groundHeight(lat, lng) {
    if (!google) return 0;
    const key = `${lat.toFixed(5)},${lng.toFixed(5)}`;
    if (!ground.has(key)) {
      // Most-detailed sampling only advances while frames render; on-demand rendering would stall it.
      const tick = setInterval(() => scene.requestRender(), 100);
      try {
        const [c] = await Promise.race([scene.sampleHeightMostDetailed([Cesium.Cartographic.fromDegrees(lng, lat)]),
                                        new Promise((_, no) => setTimeout(() => no(new Error("timeout")), 15000))]);
        if (c?.height != null && Number.isFinite(c.height)) ground.set(key, c.height);
      } catch { /* not loaded yet */ } finally { clearInterval(tick); }
    }
    return ground.get(key) ?? 0;
  }
  let flight = 0;
  function flyTo(site, { fromSpace = false } = {}) {
    if (site?.lat == null) return;
    const id = ++flight;
    const go = (h, duration) => viewer.camera.flyToBoundingSphere(
      new Cesium.BoundingSphere(Cesium.Cartesian3.fromDegrees(site.lng, site.lat, h), 120),
      { offset: new Cesium.HeadingPitchRange(0, Cesium.Math.toRadians(-38), 650), duration });
    if (fromSpace) viewer.camera.setView({ destination: Cesium.Cartesian3.fromDegrees(site.lng, site.lat - 25, 14e6) });
    const known = google ? ground.get(`${site.lat.toFixed(5)},${site.lng.toFixed(5)}`) : 0;
    go(known ?? 0, fromSpace ? 3.5 : 1.5);
    // Under Google's tiles the ground can be hundreds of metres above the ellipsoid: re-aim once it's known.
    if (google && known == null) {
      setTimeout(async () => {
        const h = await groundHeight(site.lat, site.lng);
        if (id === flight && Math.abs(h) > 5) go(h, 1.2);
      }, fromSpace ? 3600 : 1600);
    }
  }

  function setImagery(kind) {
    if (google) {
      google.show = kind !== "street";
      scene.globe.show = kind === "street";
      if (kind === "street" && !viewer.imageryLayers.length) viewer.imageryLayers.add(layerFor("street"));
    } else {
      viewer.imageryLayers.removeAll();
      viewer.imageryLayers.add(layerFor(kind));
    }
    scene.requestRender();
  }

  // ---- camera editor (admins). Not placed yet: click its spot, then move the mouse to aim it
  // (direction and distance follow the mouse) and click. Placed: drag the handles. The centre
  // moves it, the tip aims it and sets the distance, the side handles set the width (any angle,
  // all the way to a 360° dome). Every finished change goes to onCommit (saved by the caller).
  const LIMITS = { fov: [5, 360], range: [2, 500] };
  const clampTo = (v, [lo, hi]) => Math.min(hi, Math.max(lo, v));
  function editCamera(cam, { onChange, onCommit, onMode } = {}) {
    stopEditing();
    editKey = `${cam.siteId}:${cam.deviceId}`;
    const st = { heading: 0, fov: 60, range_m: 30, ...cam };
    let mode = st.lat == null ? "spot" : "edit", drag = null;
    const live = (fn) => new Cesium.CallbackProperty(fn, false);
    const at = (bearingDeg, m) => { const [lng, lat] = offset(st.lat, st.lng, bearingDeg, m); return Cesium.Cartesian3.fromDegrees(lng, lat, 0); };
    const ready = () => st.lat != null;
    const handle = (kind, pos, size, col) => viewer.entities.add({
      position: live(() => (ready() ? pos() : Cesium.Cartesian3.ZERO)),
      point: { show: live(() => ready() && mode === "edit"), pixelSize: size, color: col, outlineColor: INK, outlineWidth: 2, disableDepthTestDistance: Number.POSITIVE_INFINITY },
    });
    const YELLOW = Cesium.Color.fromCssColorString("#FFD54A");
    const parts = [
      viewer.entities.add({
        polygon: { show: live(() => ready()), hierarchy: live(() => new Cesium.PolygonHierarchy(ready() ? conePositions(st) : [])), material: Cesium.Color.WHITE.withAlpha(0.28), height: 0 },
        polyline: { show: live(() => ready()), positions: live(() => { if (!ready()) return []; const r = conePositions(st); return [...r, r[0]]; }), width: 2,
                    material: ORANGE, depthFailMaterial: ORANGE },
      }),
      handle("move", () => Cesium.Cartesian3.fromDegrees(st.lng, st.lat, 0), 16, Cesium.Color.WHITE),
      handle("aim", () => at(st.heading, st.range_m), 13, ORANGE),
      handle("left", () => at(st.heading - Math.min(st.fov, 359) / 2, st.range_m * 0.8), 11, YELLOW),
      handle("right", () => at(st.heading + Math.min(st.fov, 359) / 2, st.range_m * 0.8), 11, YELLOW),
    ];
    ["move", "aim", "left", "right"].forEach((k, i) => { parts[i + 1].editHandle = k; });
    const setMode = (m) => { mode = m; onMode?.(m, { ...st }); scene.requestRender(); };
    const changed = () => { onChange?.({ ...st }); scene.requestRender(); };
    const commit = () => onCommit?.({ lat: st.lat, lng: st.lng, heading: Math.round(st.heading * 10) / 10 % 360,
                                      fov: Math.round(st.fov), range_m: Math.round(st.range_m * 10) / 10 });
    const ctl = scene.screenSpaceCameraController;
    editor = {
      click(pos) {
        if (mode === "spot") { const pt = groundAt(pos); if (pt) { Object.assign(st, pt); setMode("aim"); changed(); } return true; }
        if (mode === "aim") { setMode("edit"); commit(); return true; }
        return !!drag;
      },
      move(pos) {
        if (mode === "aim" || drag) {
          const pt = groundAt(pos);
          if (!pt) return;
          if (drag === "move") Object.assign(st, pt);
          else if (mode === "aim" || drag === "aim") { st.heading = bearing(st, pt); st.range_m = clampTo(distM(st, pt), LIMITS.range); }
          else if (drag === "left" || drag === "right") {
            const diff = Math.abs(((bearing(st, pt) - st.heading + 540) % 360) - 180);    // 0..180 from the centre line
            st.fov = clampTo(2 * diff, LIMITS.fov);
            if (st.fov > 350) st.fov = 360;
            st.range_m = clampTo(distM(st, pt) / 0.8, LIMITS.range);
          }
          changed();
        }
      },
      down(pos) {
        if (mode !== "edit") return;
        const kind = scene.pick(pos)?.id?.editHandle;
        if (!kind) return;
        drag = kind;
        ctl.enableInputs = false;          // the drag moves the handle, not the view
      },
      up() {
        if (!drag) return;
        drag = null;
        ctl.enableInputs = true;
        commit();
      },
      stop() { ctl.enableInputs = true; parts.forEach((e) => viewer.entities.remove(e)); },
      set(values) { Object.assign(st, values); changed(); if (mode === "edit") commit(); },
      get mode() { return mode; },
      get state() { return { ...st }; },
    };
    setMode(mode);
    update();
    return editor;
  }
  // ---- model editor (admins): drag the white centre dot to move the model, the orange dot on the
  // ring to turn it. Numbers (height, size, exact turn) come through set(). onCommit saves.
  function editModel(siteId, placement, { onChange, onCommit } = {}) {
    stopEditing();
    const st = { heading: 0, height: 0, scale: 1, ...placement };
    modelEdit = { siteId, st };
    const ts = modelsE.get(siteId)?.tileset;
    const ringR = () => Math.max(8, Math.min(400, (ts?.boundingSphere?.radius || 20) * 0.9 * (st.scale || 1) / (modelsE.get(siteId)?.placement?.scale || 1)));
    const live = (fn) => new Cesium.CallbackProperty(fn, false);
    const lift = 0.5;
    const at = (bearingDeg, m) => { const [lng, lat] = offset(st.lat, st.lng, bearingDeg, m); return Cesium.Cartesian3.fromDegrees(lng, lat, lift); };
    let drag = null;
    const parts = [
      viewer.entities.add({ polyline: { positions: live(() => Array.from({ length: 73 }, (_, i) => at(i * 5, ringR()))), width: 2,
                                        material: ORANGE.withAlpha(0.8), depthFailMaterial: ORANGE.withAlpha(0.45) } }),
      viewer.entities.add({ polyline: { positions: live(() => [Cesium.Cartesian3.fromDegrees(st.lng, st.lat, lift), at(st.heading, ringR())]), width: 2,
                                        material: ORANGE, depthFailMaterial: ORANGE.withAlpha(0.45) } }),
      viewer.entities.add({ position: live(() => Cesium.Cartesian3.fromDegrees(st.lng, st.lat, lift)),
                            point: { pixelSize: 18, color: Cesium.Color.WHITE, outlineColor: INK, outlineWidth: 2, disableDepthTestDistance: Number.POSITIVE_INFINITY } }),
      viewer.entities.add({ position: live(() => at(st.heading, ringR())),
                            point: { pixelSize: 15, color: ORANGE, outlineColor: INK, outlineWidth: 2, disableDepthTestDistance: Number.POSITIVE_INFINITY } }),
    ];
    parts[2].editHandle = "move"; parts[3].editHandle = "turn";
    const changed = () => { placeModel(siteId); onChange?.({ ...st }); scene.requestRender(); };
    const round = () => ({ lat: Math.round(st.lat * 1e8) / 1e8, lng: Math.round(st.lng * 1e8) / 1e8, height: Math.round(st.height * 100) / 100,
                           heading: Math.round((((st.heading % 360) + 360) % 360) * 100) / 100, scale: Math.round(st.scale * 1e5) / 1e5 });
    const ctl = scene.screenSpaceCameraController;
    let grab = null;          // where on the ground the move started, so the model doesn't jump to the mouse
    editor = {
      click() { return !!drag; },
      move(pos) {
        if (!drag) return;
        const pt = groundAt(pos);
        if (!pt) return;
        if (drag === "move") { st.lat = grab.lat + (pt.lat - grab.mlat); st.lng = grab.lng + (pt.lng - grab.mlng); }
        else st.heading = bearing(st, pt);
        changed();
      },
      down(pos) {
        const kind = scene.pick(pos)?.id?.editHandle;
        if (!kind) return;
        const pt = groundAt(pos);
        if (!pt) return;
        drag = kind; grab = { lat: st.lat, lng: st.lng, mlat: pt.lat, mlng: pt.lng };
        ctl.enableInputs = false;
      },
      up() { if (!drag) return; drag = null; ctl.enableInputs = true; onCommit?.(round()); },
      stop() { ctl.enableInputs = true; parts.forEach((e) => viewer.entities.remove(e)); modelEdit = null; placeModel(siteId); },
      set(values) { Object.assign(st, values); changed(); onCommit?.(round()); },
      get mode() { return "model"; },
      get state() { return { ...st }; },
    };
    changed();
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
    update, flyTo, setImagery, editCamera, editModel, stopEditing, showModels, flyToModel,
    modelState: (siteId) => { const e = modelsE.get(siteId); return e ? { loaded: !!e.tileset, failed: e.failed || "", tiles: e.tileset ? e.tileset.statistics.numberOfLoadedTilesTotal : 0 } : null; },
    get editing() { return editor ? { key: editKey, mode: editor.mode, model: modelEdit?.siteId ?? null } : null; },
    currentEditor: () => editor,
    hasBuildings: !!google,
    destroy() { clearInterval(settle); stopEditing(); input.destroy(); viewer.destroy(); },
    get viewer() { return viewer; },   // for tests and debugging
    stats,
  };
}
