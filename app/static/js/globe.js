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

/** Why this screen can't have the 3D view, or "" if it can. localStorage twg-3d-force=1 skips the check (tests). */
export function graphicsProblem() {
  let force = false;
  try { force = localStorage.getItem("twg-3d-force") === "1"; } catch { /* storage blocked */ }
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
 *   onPick(siteId): a pin was clicked
 *   onTooSlow(reason): frames are slow enough to delay alarms; the caller should close the view
 */
export async function openGlobe(el, { pins, onPick, onTooSlow }) {
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
  const stats = { frames: 0, slow: 0, maxMs: 0 };
  widget.render = () => {
    const t0 = performance.now();
    render();
    const ms = performance.now() - t0;
    if (++frames <= GRACE_FRAMES || tripped || ms < 1) return;
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

  const entities = new Map();   // site id -> entity
  const color = (state) => Cesium.Color.fromCssColorString(
    { alarm: cssVar("--red", "#C0392B"), warn: cssVar("--warn", "#D9A21B"), ok: cssVar("--ok", "#2FA36B") }[state] || cssVar("--offline", "#767676"));

  function update() {
    const seen = new Set();
    for (const p of pins()) {
      if (p.lat == null || p.lng == null) continue;
      seen.add(p.id);
      const text = `${p.name}${p.count ? `  (${p.count})` : ""}${p.disarmed ? "  DISARMED" : ""}`;
      const size = p.state === "alarm" ? 22 : 16;
      let e = entities.get(p.id);
      if (!e) {
        e = viewer.entities.add({
          position: Cesium.Cartesian3.fromDegrees(p.lng, p.lat, 2),
          point: { pixelSize: size, outlineColor: Cesium.Color.WHITE, outlineWidth: 3, disableDepthTestDistance: Number.POSITIVE_INFINITY },
          label: {
            text, font: "600 14px Arial, Helvetica, sans-serif", fillColor: Cesium.Color.WHITE,
            outlineColor: Cesium.Color.fromCssColorString("#1A1A1A"), outlineWidth: 4, style: Cesium.LabelStyle.FILL_AND_OUTLINE,
            verticalOrigin: Cesium.VerticalOrigin.BOTTOM, pixelOffset: new Cesium.Cartesian2(0, -18),
            disableDepthTestDistance: Number.POSITIVE_INFINITY,
          },
        });
        e.siteId = p.id;
        entities.set(p.id, e);
      }
      e.position = Cesium.Cartesian3.fromDegrees(p.lng, p.lat, 2);
      e.point.color = color(p.state);
      e.point.pixelSize = size;
      e.label.text = text;
    }
    for (const [id, e] of entities) if (!seen.has(id)) { viewer.entities.remove(e); entities.delete(id); }
    scene.requestRender();
  }

  new Cesium.ScreenSpaceEventHandler(scene.canvas).setInputAction((click) => {
    const hit = scene.pick(click.position);
    const id = hit?.id?.siteId;
    if (id != null) onPick(id);
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

  // Start out in space above the site, then dive in: the "god view".
  function flyTo(site, { fromSpace = false } = {}) {
    if (site?.lat == null) return;
    const target = new Cesium.BoundingSphere(Cesium.Cartesian3.fromDegrees(site.lng, site.lat, 0), 120);
    if (fromSpace) viewer.camera.setView({ destination: Cesium.Cartesian3.fromDegrees(site.lng, site.lat - 25, 14e6) });
    viewer.camera.flyToBoundingSphere(target, {
      offset: new Cesium.HeadingPitchRange(0, Cesium.Math.toRadians(-40), 650),
      duration: fromSpace ? 3.5 : 1.5,
    });
  }

  function setImagery(kind) {
    viewer.imageryLayers.removeAll();
    viewer.imageryLayers.add(layerFor(kind));
    scene.requestRender();
  }

  update();
  return {
    update, flyTo, setImagery,
    destroy() { viewer.destroy(); },
    get viewer() { return viewer; },   // for tests and debugging
    stats,
  };
}
