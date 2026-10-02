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
 *   onPick(siteId): a site pin was clicked; onPickCamera(siteId, deviceId): a camera was clicked
 *   onView({ lat, lng, alt, heading }): the view stopped moving (for the HUD readout)
 *   onTooSlow(reason): frames are slow enough to delay alarms; the caller should close the view
 */
export async function openGlobe(el, { pins, cameras = () => [], onPick, onPickCamera, onView, onTooSlow }) {
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
  function conePositions(c) {
    const fov = Math.min(c.fov || 60, 360), r = c.range_m || 40, pts = [];
    if (fov < 360) pts.push(c.lng, c.lat);
    const steps = Math.max(6, Math.round(fov / 6));
    for (let i = 0; i <= steps; i++) pts.push(...offset(c.lat, c.lng, (c.heading || 0) - fov / 2 + (fov * i) / steps, r));
    return Cesium.Cartesian3.fromDegreesArray(pts);
  }
  const camsE = new Map();    // "site:device" -> { marker, cone }
  function updateCameras() {
    const seen = new Set();
    for (const c of cameras()) {
      const key = `${c.siteId}:${c.deviceId}`;
      seen.add(key);
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
    }
    for (const [key, e] of camsE) {
      if (seen.has(key)) continue;
      for (const part of [e.marker, e.cone, e.edge]) viewer.entities.remove(part);
      camsE.delete(key);
    }
  }

  // Ground-draped shapes are built in the background and can finish after the frame that asked
  // for them; with on-demand rendering nothing would redraw, so ask a few more times.
  let settle = null;
  function update() {
    updatePins(); updateCameras(); scene.requestRender();
    clearInterval(settle);
    let n = 0;
    settle = setInterval(() => { scene.requestRender(); if (++n >= 6) clearInterval(settle); }, 250);
  }

  // ---- clicks: pick a pin or camera, or (while placing) a spot on the ground
  const toDeg = Cesium.Math.toDegrees;
  function groundAt(pos) {
    let c = scene.pickPositionSupported ? scene.pickPosition(pos) : undefined;
    if (!c || (!google && !scene.globe.show)) c = viewer.camera.pickEllipsoid(pos, scene.globe.ellipsoid);
    if (!c) return null;
    const g = Cesium.Cartographic.fromCartesian(c);
    return { lat: toDeg(g.latitude), lng: toDeg(g.longitude) };
  }
  let placing = null;         // { onPoint(pt), onMove(pt) } while an admin places a camera
  const input = new Cesium.ScreenSpaceEventHandler(scene.canvas);
  input.setInputAction((click) => {
    if (placing) { const pt = groundAt(click.position); if (pt) placing.onPoint(pt); return; }
    const hit = scene.pick(click.position)?.id;
    if (hit?.camera) onPickCamera?.(hit.camera.siteId, hit.camera.deviceId);
    else if (hit?.siteId != null) onPick?.(hit.siteId);
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
  input.setInputAction((move) => {
    if (!placing?.onMove) return;
    const pt = groundAt(move.endPosition);
    if (pt) placing.onMove(pt);
  }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);

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
      try {
        const [c] = await scene.sampleHeightMostDetailed([Cesium.Cartographic.fromDegrees(lng, lat)]);
        if (c?.height != null && Number.isFinite(c.height)) ground.set(key, c.height);
      } catch { /* not loaded yet */ }
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

  // ---- placing a camera: click its spot, then the direction it faces (a cone follows the mouse)
  let preview = null;
  function placeCamera({ fov = 60, range_m = 40 } = {}) {
    cancelPlacing();
    return new Promise((resolve) => {
      let at = null;
      const draw = (heading) => {
        const c = { ...at, heading, fov, range_m };
        if (!preview) preview = viewer.entities.add({ polygon: { classificationType: Cesium.ClassificationType.BOTH, material: Cesium.Color.WHITE.withAlpha(0.35) } });
        preview.polygon.hierarchy = new Cesium.PolygonHierarchy(conePositions(c));
        scene.requestRender();
      };
      const bearing = (to) => (toDeg(Math.atan2((to.lng - at.lng) * Math.cos(Cesium.Math.toRadians(at.lat)), to.lat - at.lat)) + 360) % 360;
      placing = {
        step: "spot",
        onPoint(pt) {
          if (!at) { at = pt; placing.step = "direction"; draw(0); placing.onStep?.("direction"); return; }
          const heading = fov >= 360 ? 0 : bearing(pt);
          endPlacing();
          resolve({ ...at, heading, fov, range_m });
        },
        onMove(pt) { if (at && fov < 360) draw(bearing(pt)); },
        cancel() { resolve(null); },
      };
      if (fov >= 360) placing.onPoint = (pt) => { endPlacing(); resolve({ ...pt, heading: 0, fov, range_m }); };
    });
  }
  function endPlacing() {           // stop placing; returns what was being placed
    const p = placing;
    placing = null;
    if (preview) { viewer.entities.remove(preview); preview = null; scene.requestRender(); }
    return p;
  }
  function cancelPlacing() { endPlacing()?.cancel?.(); }

  update();
  return {
    update, flyTo, setImagery, placeCamera, cancelPlacing,
    get placingStep() { return placing?.step || null; },
    onPlacingStep(cb) { if (placing) placing.onStep = cb; },
    hasBuildings: !!google,
    destroy() { clearInterval(settle); cancelPlacing(); input.destroy(); viewer.destroy(); },
    get viewer() { return viewer; },   // for tests and debugging
    stats,
  };
}
