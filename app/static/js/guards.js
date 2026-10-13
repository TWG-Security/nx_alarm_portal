// Live guard / patrol-vehicle positions on the map page (Peplink routers with GPS, read by the server from
// InControl2: app/services/trackers.py). Markers and a 30-minute trail on the Leaflet map (street map and
// top-down view alike), a "Guards" list in the side panel, and the data for the 3D view.
// Positions arrive about every 1-2 minutes, so the marker shows how fresh its position is.
import { api, esc, on, relTime } from "./common.js";

const FRESH_S = 180, STALE_S = 900;       // green under 3 min, amber under 15 min, then grey "last seen"
const MPH = 2.23694;

export function freshness(t, now = Date.now()) {
  if (!t.fix_at) return "none";
  const age = (now - Date.parse(t.fix_at)) / 1000;
  return age < FRESH_S ? "live" : age < STALE_S ? "late" : "stale";
}
export const speedText = (t) => (t.speed != null && t.speed * MPH >= 1 ? `${Math.round(t.speed * MPH)} mph` : "stopped");

/**
 * lmap: the Leaflet map; listEl: the side-panel box; onChange(): something moved (redraw the 3D view).
 * Returns { list(), focus(id), destroy() }.
 */
export function mountGuards(lmap, listEl, { onChange } = {}) {
  const guards = new Map();          // id -> { t, marker, trail, line }
  lmap.createPane("guards").style.zIndex = 640;          // above cones and site pins, below popups
  let follow = null;

  function icon(t) {
    const f = freshness(t), moving = t.speed != null && t.speed * MPH >= 1 && t.heading != null;
    const sub = f === "stale" || f === "none" ? (t.fix_at ? `last seen ${relTime(t.fix_at)}` : "no position yet") : speedText(t);
    return L.divIcon({
      className: "guard-icon", iconSize: [0, 0],
      html: `<div class="guard guard--${f}${t.error ? " guard--err" : ""}">
        <div class="guard-dot">${moving ? `<i class="guard-arrow" style="transform:rotate(${Math.round(t.heading)}deg)"></i>` : ""}</div>
        <div class="guard-label">${esc(t.name)}<small>${esc(sub)}</small></div></div>`,
    });
  }

  function upsert(t) {
    if (t.enabled === false) { remove(t.id); return; }
    let g = guards.get(t.id);
    if (!g) {
      g = { t, trail: (t.trail || []).map(([la, lo]) => [la, lo]) };
      g.line = L.polyline(g.trail, { pane: "guards", color: "#E08A30", weight: 3, opacity: 0.75, dashArray: "6 6", interactive: false }).addTo(lmap);
      guards.set(t.id, g);
    }
    const prev = g.t;
    g.t = { ...prev, ...t };
    if (g.t.lat != null) {
      const last = g.trail[g.trail.length - 1];
      if (!last || last[0] !== g.t.lat || last[1] !== g.t.lng) g.trail.push([g.t.lat, g.t.lng]);
      if (g.trail.length > 400) g.trail.splice(0, g.trail.length - 400);
      g.line.setLatLngs(g.trail);
      if (!g.marker) {
        g.marker = L.marker([g.t.lat, g.t.lng], { pane: "guards", icon: icon(g.t), keyboard: true, alt: g.t.name, zIndexOffset: 2000 })
          .addTo(lmap).on("click", () => focus(g.t.id));
      } else {
        g.marker.setLatLng([g.t.lat, g.t.lng]);
        g.marker.setIcon(icon(g.t));
      }
      if (follow === g.t.id) lmap.panTo([g.t.lat, g.t.lng]);
    }
    renderList();
    onChange?.();
  }

  function remove(id) {
    const g = guards.get(id);
    if (!g) return;
    g.marker?.remove(); g.line.remove();
    guards.delete(id);
    if (follow === id) follow = null;
    renderList();
    onChange?.();
  }

  function renderList() {
    const all = [...guards.values()].map((g) => g.t).sort((a, b) => a.name.localeCompare(b.name));
    listEl.hidden = !all.length;
    if (!all.length) { listEl.replaceChildren(); return; }
    listEl.innerHTML = `<h3>Guards</h3><ul class="guard-list">${all.map((t) => {
      const f = freshness(t);
      return `<li data-guard="${t.id}" class="${follow === t.id ? "following" : ""}" tabindex="0" title="${esc(t.error || t.device_name || "")}">
        <span class="guard-pip guard--${f}"></span><b>${esc(t.name)}</b>
        <span class="muted small-print">${f === "none" ? "no position yet" : f === "stale" ? `last seen ${esc(relTime(t.fix_at))}` : `${esc(speedText(t))} · ${esc(relTime(t.fix_at))}`}</span>
        ${t.error ? '<span class="chip offline" title="' + esc(t.error) + '">!</span>' : ""}</li>`;
    }).join("")}</ul>`;
  }

  function focus(id) {
    const g = guards.get(id);
    if (!g || g.t.lat == null) return;
    follow = follow === id ? null : id;          // a second click stops following
    if (follow) lmap.setView([g.t.lat, g.t.lng], Math.max(lmap.getZoom(), 17));
    renderList();
  }

  listEl.addEventListener("click", (e) => {
    const li = e.target.closest("[data-guard]");
    if (li) focus(Number(li.dataset.guard));
  });
  listEl.addEventListener("keydown", (e) => {
    const li = e.target.closest("[data-guard]");
    if (li && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); focus(Number(li.dataset.guard)); }
  });

  async function load() {
    const rows = await api("/api/trackers?trail_min=30");
    const seen = new Set(rows.map((t) => t.id));
    for (const id of [...guards.keys()]) if (!seen.has(id)) remove(id);
    for (const t of rows) {
      const g = guards.get(t.id);
      if (g) { g.trail = t.trail.map(([la, lo]) => [la, lo]); g.line.setLatLngs(g.trail); }
      upsert(t);
    }
  }
  const offs = [
    on("tracker.moved", (t) => upsert(t)),
    on("tracker.removed", ({ id }) => remove(id)),
    on("reconnect", () => load().catch(() => {})),
  ];
  // Freshness changes with time alone: repaint the markers and the list every 15 s.
  const tick = setInterval(() => { for (const g of guards.values()) g.marker?.setIcon(icon(g.t)); renderList(); }, 15000);
  lmap.on("dragstart", () => { if (follow) { follow = null; renderList(); } });
  load().catch(() => {});

  return {
    list: () => [...guards.values()].map((g) => ({ ...g.t, trail: g.trail, fresh: freshness(g.t) })),
    focus,
    destroy() { clearInterval(tick); offs.forEach((off) => off?.()); for (const id of [...guards.keys()]) remove(id); },
  };
}
