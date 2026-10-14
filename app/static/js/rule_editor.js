// NX rule editor (administrators): every part of a site's NX rule, with drop-downs built from what that site's NX
// actually supports (GET /api/sites/{id}/rules/catalog): event type, cameras, analytics event type / object type,
// the zones found on each camera, text filters, NX's action and its settings, plus the portal's level tag, #24h,
// title and on/off. Saves go to NX and are read back by the server (app/services/rule_edit.py).
import { api, esc, toast, relTime } from "./common.js";

const LEVELS = [["none", "No tag: the event type decides"], ["critical", "Critical (#critical)"], ["alarm", "Alarm (#alarm)"],
  ["warning", "Warning (#warning)"], ["ignore", "Never an alarm (#ignore)"]];
const STATE_LABEL = { started: "When it starts", stopped: "When it ends", instant: "Each time it happens", none: "For as long as it lasts" };
const CHECK_LABEL = { containsKeywords: "contains", doesNotContainKeywords: "doesn't contain", inList: "is in lookup list",
  notInList: "isn't in lookup list", hasAttributes: "has attributes" };
const GROUP_LABEL = { "": "Detections and triggers", deviceIssueGroup: "Camera problems", serverIssueGroup: "Server problems" };
const LEVEL_TAG = /(^|\s)#(critical|alarm|warning|ignore)\b/i;
const stripTags = (t) => (t || "").replace(/(^|\s)#(critical|alarm|warning|ignore|24h)\b/gi, " ").replace(/\s{2,}/g, " ").trim();
const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
const uid = (v) => String(v || "").replace(/[{}]/g, "").toLowerCase();

export async function loadCatalog(siteId, refresh = false) {
  return api(`/api/sites/${siteId}/rules/catalog${refresh ? "?refresh=1" : ""}`);
}

// Takes over `host` (the rules dialog) until Save / Delete (onDone(rulesView)) or Back (onCancel()).
// Returns { dirty() } so the dialog can ask before closing on unsaved changes.
export function openRuleEditor(host, { site, ruleId = null, onDone, onCancel, onQuickEdit }) {
  const st = { site, ruleId, cat: null, rule: null, draft: null, saved: "", error: "", zonesFor: null, busy: false, camSearch: {} };
  const root = document.createElement("div");
  root.className = "rx";
  host.replaceChildren(root);
  root.innerHTML = `<p class="muted">Reading what ${esc(site.name)}'s NX supports…</p>`;

  const kind = (side, t) => (side === "event" ? st.cat.events : st.cat.actions).find((k) => k.id === t);
  const draftJson = () => JSON.stringify(st.draft);
  const dirty = () => !!st.draft && draftJson() !== st.saved;
  const cams = () => st.cat.cameras;
  const camName = (id) => cams().find((c) => c.id === uid(id))?.name || id;

  function fromRule(rule) {
    const ev = rule.event || {}, act = rule.action || {};
    const take = (side, part) => {
      const k = kind(side, part.type);
      const fields = {};
      (k?.fields || []).forEach((f) => { if (part[f.key] !== undefined) fields[f.key] = clone(part[f.key]); });
      return { type: part.type, fields };
    };
    const tag = (rule.comment || "").match(LEVEL_TAG);
    return { event: take("event", ev), action: take("action", act), comment: stripTags(rule.comment),
      level: tag ? tag[2].toLowerCase() : "none", always: /(^|\s)#24h\b/i.test(rule.comment || ""), enabled: rule.enabled !== false };
  }
  function blank() {
    const withAnalytics = Object.values(st.cat.analytics).some((a) => a.event_types.length);
    const ev = kind("event", withAnalytics ? "analytics" : "motion") ? (withAnalytics ? "analytics" : "motion") : st.cat.events[0].id;
    return { event: defaults("event", ev), action: { type: "writeToLog", fields: { intervalS: 0 } }, comment: "", level: "alarm", always: false, enabled: true };
  }
  function defaults(side, t) {
    const fields = {};
    (kind(side, t)?.fields || []).forEach((f) => {
      if (!f.editable || !f.visible) return;
      if (f.type === "state") fields[f.key] = f.default || (f.enum || [])[1] || "started";
      else if (["device", "user", "server", "users", "servers"].includes(f.type)) fields[f.key] = { acceptAll: f.type === "users" || f.accept_all, ids: [] };
      else if (f.type === "devices") fields[f.key] = { ids: [], useSource: true };
      else if (["textLookup"].includes(f.type)) fields[f.key] = { checkType: "containsKeywords", value: "" };
      else if (f.type === "objectLookup") fields[f.key] = { checkType: "hasAttributes", value: "" };
      else if (f.key === "intervalS") fields[f.key] = 0;
      else if (["analyticsEventType", "analyticsObjectType"].includes(f.type)) fields[f.key] = "";
      else if (f.default !== null && f.default !== undefined) fields[f.key] = f.default;
    });
    return { type: t, fields };
  }

  async function start(refresh = false) {
    try {
      const [cat, rule] = await Promise.all([loadCatalog(site.id, refresh),
        st.ruleId && !st.rule ? api(`/api/sites/${site.id}/rules/${encodeURIComponent(st.ruleId)}`) : Promise.resolve(st.rule)]);
      st.cat = cat; st.rule = rule;
      if (!st.draft) { st.draft = rule ? fromRule(rule) : blank(); st.saved = draftJson(); st.initial = JSON.parse(st.saved); }
      render();
    } catch (err) {
      root.innerHTML = `<div class="rules-head"><h2>NX rule editor · ${esc(site.name)}</h2><button type="button" class="btn btn-sm" data-x="back">← Back to rules</button></div>
        <div class="alert alert-error">${esc(err.message)}</div>
        <p class="muted">The editor needs the site's NX to describe its rule engine.${st.ruleId && onQuickEdit ? " You can still change this rule's tags, on/off and Interval of action:" : ""}</p>
        <div style="display:flex;gap:8px"><button type="button" class="btn btn-sm" data-x="retry">Try again</button>
        ${st.ruleId && onQuickEdit ? '<button type="button" class="btn btn-sm" data-x="quick">Quick edit</button>' : ""}</div>`;
    }
  }

  // ------------------------------------------------------------ rendering
  function selectedCams(fields) {
    const d = fields.devices;
    if (!d || d.acceptAll || !(d.ids || []).length) return cams().map((c) => c.id);
    return d.ids.map(uid);
  }
  const opt = (v, label, cur, extra = "") => `<option value="${esc(v)}" ${String(cur ?? "") === String(v) ? "selected" : ""} ${extra}>${esc(label)}</option>`;
  const fid = (side, key, sub = "") => `data-f="${side}" data-k="${esc(key)}"${sub ? ` data-sub="${sub}"` : ""}`;

  function camPicker(side, f, val, { useSource = false } = {}) {
    val = val || {};
    const all = !!val.acceptAll, ids = (val.ids || []).map(uid), q = (st.camSearch[side] || "").toLowerCase();
    const list = cams().filter((c) => !q || c.name.toLowerCase().includes(q) || ids.includes(c.id));
    const zoneCount = (c) => (st.cat.analytics[c.id]?.zones || []).length;
    return `<div class="rx-cams">
      ${f.accept_all ? `<label class="check"><input type="checkbox" ${fid(side, f.key, "all")} ${all ? "checked" : ""}> <span><b>Any camera</b></span></label>` : ""}
      ${useSource ? `<label class="check"><input type="checkbox" ${fid(side, f.key, "source")} ${val.useSource ? "checked" : ""}> <span><b>The event's own camera</b></span></label>` : ""}
      ${cams().length > 8 ? `<input type="search" class="rx-search" placeholder="Find a camera" data-cam-search="${side}" value="${esc(st.camSearch[side] || "")}">` : ""}
      <div class="rx-cam-list ${all ? "rx-disabled" : ""}">${list.map((c) => `<label class="check rx-cam"><input type="checkbox" ${fid(side, f.key, "id")} value="${esc(c.id)}" ${ids.includes(c.id) ? "checked" : ""} ${all ? "disabled" : ""}>
        <span>${esc(c.name)}${c.engines.length ? ` <span class="chip rx-chip" title="Analytics plugins on this camera">${esc(c.engines.map((e) => e.name || "plugin").join(", "))}</span>` : ""}${zoneCount(c) ? ` <span class="chip rx-chip rx-zone-chip">${zoneCount(c)} zone${zoneCount(c) === 1 ? "" : "s"}</span>` : ""}</span></label>`).join("") || '<span class="muted">No cameras match.</span>'}</div></div>`;
  }
  function peoplePicker(side, f, val, { groups = false, anyLabel = "Everyone" } = {}) {
    val = val || {};
    const all = !!val.acceptAll, ids = (val.ids || []).map(uid);
    const row = (p, tag = "") => `<label class="check"><input type="checkbox" ${fid(side, f.key, "id")} value="${esc(p.id)}" ${ids.includes(p.id) ? "checked" : ""} ${all ? "disabled" : ""}> <span>${esc(p.name || p.id)}${tag}</span></label>`;
    const known = new Set([...st.cat.users.map((u) => u.id), ...st.cat.groups.map((g) => g.id)]);
    return `<div class="rx-cams"><label class="check"><input type="checkbox" ${fid(side, f.key, "all")} ${all ? "checked" : ""}> <span><b>${anyLabel}</b></span></label>
      <div class="rx-cam-list ${all ? "rx-disabled" : ""}">${groups ? st.cat.groups.map((g) => row(g, ' <span class="chip rx-chip">group</span>')).join("") : ""}
        ${st.cat.users.map((u) => row(u)).join("")}${ids.filter((i) => !known.has(i)).map((i) => row({ id: i, name: i }, ' <span class="chip rx-chip">kept</span>')).join("")}</div></div>`;
  }
  function eventTypeOptions(fields, cur, which) {
    const key = which === "object" ? "object_types" : "event_types";
    const groups = selectedCams(fields).map((id) => ({ id, name: camName(id), list: st.cat.analytics[id]?.[key] || [] })).filter((g) => g.list.length);
    let found = false;
    const html = groups.map((g) => `<optgroup label="${esc(g.name)}">${g.list.map((t) => {
      if (t.id === cur) found = true;
      const seen = t.seen ? ` · seen ${t.seen}×${t.last_ms ? ` (${relTime(t.last_ms)})` : ""}` : "";
      return opt(t.id, (t.label || t.name || t.id) + seen, cur);
    }).join("")}</optgroup>`).join("");
    const fallback = which === "object" && !groups.length
      ? ["nx.base.Person", "nx.base.Vehicle", "nx.base.Animal", "nx.base.Bike", "nx.base.Face", "nx.base.LicensePlate"].map((t) => { if (t === cur) found = true; return opt(t, t.split(".").pop(), cur); }).join("") : "";
    return `<option value="" ${cur ? "" : "selected"} disabled>Pick a type…</option>${html}${fallback}${cur && !found ? opt(cur, `${cur} (current)`, cur) : ""}
      <option value="__other">Other (type its NX id)…</option>`;
  }
  function zonesHtml(fields) {
    const withZones = selectedCams(fields).map((id) => ({ id, name: camName(id), zones: st.cat.analytics[id]?.zones || [],
      types: st.cat.analytics[id]?.event_types || [] })).filter((c) => c.zones.length);
    if (!withZones.length) {
      return `<div class="rx-zones muted small-print">No zones found on ${fields.devices?.acceptAll || !(fields.devices?.ids || []).length ? "this site's cameras" : "the selected cameras"} yet.
        Zones show up here when a plugin lists them, or once a zone's event has happened (AXIS Object Analytics scenarios, for example).</div>`;
    }
    return `<div class="rx-zones"><div class="rx-zones-head"><b>Zones found</b> <span class="muted small-print">Pick one to fill in the event type or filter</span></div>
      ${withZones.map((c) => `<div class="rx-zone-cam"><div class="rx-zone-cam-name">${esc(c.name)}
        <button type="button" class="btn btn-sm" data-x="zones-show" data-cam="${esc(c.id)}">${st.zonesFor === c.id ? "Hide picture" : "Show on camera"}</button></div>
        <div class="rx-zone-list">${c.zones.map((z) => {
          const t = c.types.find((x) => (x.zones || []).includes(z.name));
          const cur = t && t.id === fields.eventTypeId;
          return `<button type="button" class="rx-zone ${cur ? "active" : ""}" data-x="zone" data-cam="${esc(c.id)}" data-zone="${esc(z.name)}" data-type="${esc(t?.id || "")}"
            title="${esc(`${z.kind} · found in ${z.sources.join(", ")}`)}">${esc(z.name)} <small>${esc(z.kind)}</small></button>`;
        }).join("")}</div>
        ${st.zonesFor === c.id ? zonePicture(c) : ""}</div>`).join("")}</div>`;
  }
  function zonePicture(c) {
    const shapes = c.zones.filter((z) => z.points?.length >= 2).map((z) => {
      const pts = z.points.map(([x, y]) => `${x * 100},${y * 100}`).join(" ");
      const [lx, ly] = z.points[0];
      return `${z.kind === "line" ? `<polyline points="${pts}" class="rx-zone-line"/>` : `<polygon points="${pts}" class="rx-zone-area"/>`}
        <text x="${lx * 100}" y="${Math.max(4, ly * 100 - 1.5)}" class="rx-zone-label">${esc(z.name)}</text>`;
    }).join("");
    return `<div class="rx-pic"><img alt="${esc(c.name)} now" src="/api/sites/${st.site.id}/rules/cameras/${encodeURIComponent(c.id)}/still.jpg?t=${Date.now()}"
        onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'muted small-print',textContent:'No picture from this camera right now.'}))">
      ${shapes ? `<svg viewBox="0 0 100 100" preserveAspectRatio="none">${shapes}</svg>` : ""}
      ${shapes ? "" : '<div class="muted small-print">These zones have no drawn outline the portal can read (they come from the camera\'s own events).</div>'}</div>`;
  }

  function eventField(f, fields) {
    const v = fields[f.key], side = "event";
    const label = f.label || f.name;
    if (!f.visible || f.type === "uniqueId") return "";
    if (!f.editable) return `<div class="rx-field"><span class="rx-label">${esc(label)}</span><span class="muted small-print">Kept as set in Nx Witness${v !== undefined ? `: <code>${esc(JSON.stringify(v))}</code>` : ""}</span></div>`;
    switch (f.type) {
      case "state":
        return `<label class="rx-field"><span class="rx-label">${esc(label)}</span><select ${fid(side, f.key)}>${(f.enum || ["started", "stopped", "instant"]).map((s) => opt(s, STATE_LABEL[s] || s, v)).join("")}</select></label>`;
      case "device":
        return `<div class="rx-field rx-wide"><span class="rx-label">${esc(label)} (cameras)</span>${camPicker(side, f, v)}</div>`;
      case "analyticsEventType":
      case "analyticsObjectType": {
        const which = f.type === "analyticsObjectType" ? "object" : "event";
        const other = st.other?.[f.key];
        return `<label class="rx-field rx-wide"><span class="rx-label">${esc(label)} ${which === "object" ? "(object)" : "(analytics event)"}</span>
          <select ${fid(side, f.key)}>${eventTypeOptions(fields, v, which)}</select>
          ${other ? `<input type="text" ${fid(side, f.key)} value="${esc(v || "")}" placeholder="e.g. nx.base.Person or cvedia.rt.intrusion">` : ""}</label>
          ${which === "event" ? `<div class="rx-wide">${zonesHtml(fields)}</div>` : ""}`;
      }
      case "textLookup":
      case "objectLookup": {
        const val = v || {};
        const checks = (f.enum || ["containsKeywords", "doesNotContainKeywords"]).filter((c) => !/List$/.test(c) || val.checkType === c);
        return `<div class="rx-field rx-wide"><span class="rx-label">${esc(label)}</span><div class="rx-row">
          <select ${fid(side, f.key, "checkType")}>${checks.map((c) => opt(c, CHECK_LABEL[c] || c, val.checkType || checks[0])).join("")}</select>
          <input type="text" ${fid(side, f.key, "value")} value="${esc(val.value || "")}" placeholder="${f.type === "objectLookup" ? "e.g. Color=Red Type=Truck (blank = any)" : "words, blank = any"}"></div></div>`;
      }
      case "inputPort": {
        const ports = [...new Map(selectedCams(fields).flatMap((id) => (cams().find((c) => c.id === id)?.inputs || []).map((p) => [p.id, p]))).values()];
        return `<label class="rx-field"><span class="rx-label">${esc(label)} (input port)</span><select ${fid(side, f.key)}>${opt("", "Any input port", v || "")}
          ${ports.map((p) => opt(p.id, `${p.name} (${p.id})`, v)).join("")}${v && !ports.some((p) => p.id === v) ? opt(v, `${v} (current)`, v) : ""}</select></label>`;
      }
      case "user":
        return `<div class="rx-field rx-wide"><span class="rx-label">${esc(label)} (who pressed it)</span>${peoplePicker(side, f, v, { anyLabel: "Anyone" })}</div>`;
      case "server":
        return `<div class="rx-field rx-wide"><span class="rx-label">${esc(label)} (servers)</span><div class="rx-cams">
          <label class="check"><input type="checkbox" ${fid(side, f.key, "all")} ${v?.acceptAll ? "checked" : ""}> <span><b>Any server</b></span></label>
          ${st.cat.servers.map((s) => `<label class="check"><input type="checkbox" ${fid(side, f.key, "id")} value="${esc(s.id)}" ${(v?.ids || []).map(uid).includes(s.id) ? "checked" : ""}> <span>${esc(s.name)}</span></label>`).join("")}</div></div>`;
      case "analyticsEngine":
        return `<label class="rx-field"><span class="rx-label">${esc(label)}</span><select ${fid(side, f.key)}>${opt("00000000-0000-0000-0000-000000000000", "Any plugin", uid(v) || "00000000-0000-0000-0000-000000000000")}
          ${st.cat.engines.map((e) => opt(e.id, e.name, uid(v))).join("")}</select></label>`;
      case "analyticsEventLevel": {
        const on = String(v || "").split("|");
        return `<div class="rx-field"><span class="rx-label">${esc(label)}</span><div class="rx-row">${["info", "warning", "error"].map((l) =>
          `<label class="check"><input type="checkbox" ${fid(side, f.key, "level")} value="${l}" ${on.includes(l) ? "checked" : ""}> <span>${l}</span></label>`).join("")}</div></div>`;
      }
      case "customizableFlag":
        return `<label class="check rx-field"><input type="checkbox" ${fid(side, f.key)} ${v ? "checked" : ""}> <span>${esc(label)}</span></label>`;
      default:
        return `<label class="rx-field"><span class="rx-label">${esc(label)}</span><input type="text" ${fid(side, f.key)} value="${esc(v ?? "")}"></label>`;
    }
  }
  function actionField(f, fields) {
    const v = fields[f.key], side = "action";
    const label = f.label || f.name;
    if (!f.visible || !f.editable) return "";
    switch (f.type) {
      case "users":
        return `<div class="rx-field rx-wide"><span class="rx-label">${esc(label)} (NX users)</span>${peoplePicker(side, f, v, { groups: true, anyLabel: "All users" })}</div>`;
      case "devices":
        return `<div class="rx-field rx-wide"><span class="rx-label">${esc(label)} (cameras)</span>${camPicker(side, f, v, { useSource: true })}</div>`;
      case "servers":
        return `<div class="rx-field rx-wide"><span class="rx-label">${esc(label)} (servers)</span><div class="rx-cams">${st.cat.servers.map((s) =>
          `<label class="check"><input type="checkbox" ${fid(side, f.key, "id")} value="${esc(s.id)}" ${(v?.ids || []).map(uid).includes(s.id) ? "checked" : ""}> <span>${esc(s.name)}</span></label>`).join("")}</div></div>`;
      case "flag":
        return `<label class="check rx-field"><input type="checkbox" ${fid(side, f.key)} ${v ? "checked" : ""}> <span>${esc(label)}${f.key === "acknowledge" ? ' <span class="muted small-print">(the portal treats these as Critical unless a tag says otherwise)</span>' : ""}</span></label>`;
      case "time":
      case "optionalTime":
      case "fps": {
        const interval = f.key === "intervalS";
        return `<label class="rx-field"><span class="rx-label">${esc(interval ? "Interval of action" : label)} ${f.type === "fps" ? "(fps)" : "(seconds)"}</span>
          <input type="number" min="0" max="86400" ${fid(side, f.key)} value="${esc(v ?? 0)}">
          ${interval ? `<small class="${Number(v) > 0 ? "text-warn" : "muted"}">${Number(v) > 0 ? "Repeats inside this time reach the portal late" : "0 = off: every event reaches the portal at once"}</small>` : ""}</label>`;
      }
      case "volume":
        return `<label class="rx-field"><span class="rx-label">${esc(label)}</span><input type="range" min="0" max="1" step="0.05" ${fid(side, f.key)} value="${esc(v ?? 0.8)}"></label>`;
      case "streamQuality":
        return `<label class="rx-field"><span class="rx-label">${esc(label)}</span><select ${fid(side, f.key)}>${(f.enum || []).map((q) => opt(q, q, v || f.default)).join("")}</select></label>`;
      case "outputPort": {
        const ports = [...new Map(cams().flatMap((c) => c.outputs.map((p) => [p.id, p]))).values()];
        return `<label class="rx-field"><span class="rx-label">${esc(label)}</span><select ${fid(side, f.key)}>${opt("", "First output", v || "")}
          ${ports.map((p) => opt(p.id, `${p.name} (${p.id})`, v)).join("")}</select></label>`;
      }
      default:
        return /description|content|message/i.test(f.key)
          ? `<label class="rx-field rx-wide"><span class="rx-label">${esc(label)}</span><textarea rows="2" ${fid(side, f.key)}>${esc(v ?? "")}</textarea></label>`
          : `<label class="rx-field"><span class="rx-label">${esc(label)}</span><input type="text" ${fid(side, f.key)} value="${esc(v ?? "")}"></label>`;
    }
  }

  function summary() {
    const d = st.draft, ek = kind("event", d.event.type), ak = kind("action", d.action.type);
    const f = d.event.fields;
    const camsTxt = f.devices ? (f.devices.acceptAll || !(f.devices.ids || []).length ? (ek?.fields.some((x) => x.key === "devices" && x.accept_all) ? "any camera" : "no camera yet")
      : f.devices.ids.length <= 3 ? f.devices.ids.map(camName).join(", ") : `${f.devices.ids.length} cameras`) : "";
    const typeId = f.eventTypeId || f.objectTypeId;
    const t = typeId && selectedCams(f).map((id) => (st.cat.analytics[id]?.event_types || []).concat(st.cat.analytics[id]?.object_types || []).find((x) => x.id === typeId)).find(Boolean);
    const filters = ["caption", "description", "source", "attributes"].filter((k) => f[k]?.value).map((k) => `${k} ${CHECK_LABEL[f[k].checkType] || ""} “${f[k].value}”`);
    const lvl = { none: "level by event type", critical: "Critical", alarm: "Alarm", warning: "Warning", ignore: "ignored" }[d.level];
    return `<b>${esc(ek?.name || d.event.type)}</b>${t ? ` · ${esc(t.label || t.name)}` : typeId ? ` · <code>${esc(typeId)}</code>` : ""}${camsTxt ? ` on ${esc(camsTxt)}` : ""}${filters.length ? `, ${esc(filters.join(", "))}` : ""}
      → NX: <b>${esc(ak?.name || d.action.type)}</b> → portal: <b>${esc(lvl)}</b>${d.always ? " even while disarmed" : ""}${d.enabled ? "" : " · <span class='text-warn'>rule off</span>"}`;
  }
  // Only what's being changed has to be complete: an existing rule saved with just a new tag goes through as it is.
  const partChanged = (side) => !st.ruleId || !st.initial || JSON.stringify(st.draft[side]) !== JSON.stringify(st.initial[side]);
  function problems() {
    const d = st.draft, out = [];
    if (partChanged("event")) (kind("event", d.event.type)?.fields || []).forEach((f) => {
      if (!f.visible || !f.editable || f.optional) return;
      const v = d.event.fields[f.key];
      if (f.type === "device" && !f.accept_all && !(v?.acceptAll || (v?.ids || []).length)) out.push("Pick at least one camera");
      if (["analyticsEventType", "analyticsObjectType"].includes(f.type) && !v) out.push(`Pick “${f.label || "Of Type"}”`);
    });
    if (partChanged("action") && !kind("action", d.action.type)?.editable && !(st.rule && st.rule.action?.type === d.action.type)) out.push("Pick what NX should do");
    return out;
  }

  function render() {
    const d = st.draft, ek = kind("event", d.event.type), ak = kind("action", d.action.type);
    const locked = st.rule?.action_locked;
    const curAction = st.rule?.action?.type;
    const groups = {};
    st.cat.events.forEach((e) => (groups[e.group] ??= []).push(e));
    const srcBad = st.cat.sources.filter((s) => s.failed && !s.ok);
    const prob = problems();
    root.innerHTML = `<div class="rules-head"><div><h2>${st.ruleId ? "Edit NX rule" : "New NX rule"} · ${esc(st.site.name)}</h2>
        <div class="muted small-print">Choices read from this site's NX ${esc(relTime(st.cat.read_at * 1000))} (${st.cat.took_s} s) ·
          <button type="button" class="linkish" data-x="refresh">Read again</button> ·
          <details class="rx-sources"><summary>What was found</summary><ul>${st.cat.sources.map((s) =>
            `<li>${esc(s.name)}: ${s.ok ? `${s.ok} answered` : ""}${s.ok && s.failed ? ", " : ""}${s.failed ? `<span class="${s.ok ? "muted" : "text-warn"}">${s.failed} not (${esc(s.detail)})</span>` : ""}</li>`).join("")}</ul></details></div></div>
        <button type="button" class="btn btn-sm" data-x="back">← Back to rules</button></div>
      ${st.error ? `<div class="alert alert-error">${esc(st.error)}</div>` : ""}
      <section class="rx-sec"><h3>In the portal</h3><div class="rx-grid">
        <label class="rx-field"><span class="rx-label">Alarm level</span><select data-d="level">${LEVELS.map(([v, l]) => opt(v, l, d.level)).join("")}</select></label>
        <label class="rx-field"><span class="rx-label">Title / comment in NX (tags are added for you)</span><input type="text" maxlength="400" data-d="comment" value="${esc(d.comment)}" placeholder="e.g. Front lot after hours"></label>
        <label class="check rx-field"><input type="checkbox" data-d="always" ${d.always ? "checked" : ""}> <span><b>#24h</b>: raise even while the site is disarmed</span></label>
        <label class="check rx-field"><input type="checkbox" data-d="enabled" ${d.enabled ? "checked" : ""}> <span>Rule is on in NX</span></label>
      </div></section>
      <section class="rx-sec"><h3>When this happens</h3><div class="rx-grid">
        <label class="rx-field rx-wide"><span class="rx-label">Event</span><select data-x="event-type">${Object.entries(groups).map(([g, list]) =>
          `<optgroup label="${esc(GROUP_LABEL[g] || g)}">${list.map((e) => opt(e.id, e.name, d.event.type)).join("")}</optgroup>`).join("")}</select>
          ${ek?.description ? `<small class="muted">${esc(ek.description)}</small>` : ""}</label>
        ${(ek?.fields || []).map((f) => eventField(f, d.event.fields)).join("") || '<p class="muted small-print rx-wide">This event has no settings.</p>'}
      </div></section>
      <section class="rx-sec"><h3>NX then does</h3><div class="rx-grid">
        <label class="rx-field rx-wide"><span class="rx-label">Action</span><select data-x="action-type" ${locked ? "disabled" : ""}>
          ${curAction && !kind("action", curAction)?.editable ? opt(curAction, `${kind("action", curAction)?.name || curAction} (current: kept as set in Nx Witness)`, d.action.type) : ""}
          ${st.cat.actions.filter((a) => a.editable).map((a) => opt(a.id, a.name, d.action.type)).join("")}</select>
          <small class="muted">${locked ? "This action holds a login (e.g. an HTTP request with a password), so the portal never rewrites it: change it in Nx Witness."
            : "The portal sees every rule that fires, whatever the action: “Write to Log” is enough for the portal."}</small></label>
        ${ak?.editable && !locked ? (ak.fields || []).map((f) => actionField(f, d.action.fields)).join("") : ""}
      </div></section>
      <div class="rx-summary">${summary()}</div>
      ${prob.length ? `<div class="rx-problems text-warn small-print">${prob.map(esc).join(" · ")}</div>` : ""}
      <div class="rx-foot"><div>${st.ruleId ? `<button type="button" class="btn btn-sm btn-danger" data-x="delete">Delete rule</button>
          <button type="button" class="btn btn-sm" data-x="duplicate">Duplicate</button>` : ""}</div>
        <div><button type="button" class="btn btn-sm" data-x="back">Cancel</button>
          <button type="button" class="btn btn-sm btn-primary" data-x="save" ${prob.length || st.busy ? "disabled" : ""}>${st.busy ? "Saving…" : st.ruleId ? "Save in NX" : "Create in NX"}</button></div></div>
      ${srcBad.length ? `<p class="muted small-print">Not available from this NX: ${esc(srcBad.map((s) => s.name).join(", "))}. The lists above use everything else that was found.</p>` : ""}`;
  }

  // ------------------------------------------------------------ input
  function setField(el) {
    const side = el.dataset.f, key = el.dataset.k, sub = el.dataset.sub;
    const fields = st.draft[side].fields;
    if (sub === "all" || sub === "id" || sub === "source") {
      const cur = fields[key] = { ...(fields[key] || {}) };
      cur.ids = (cur.ids || []).map(uid);
      if (sub === "all") cur.acceptAll = el.checked;
      else if (sub === "source") cur.useSource = el.checked;
      else cur.ids = el.checked ? [...new Set([...cur.ids, el.value])] : cur.ids.filter((i) => i !== el.value);
      return true;
    }
    if (sub === "checkType" || sub === "value") {
      fields[key] = { ...(fields[key] || {}), [sub]: el.value };
      return false;
    }
    if (sub === "level") {
      const on = new Set(String(fields[key] || "").split("|").filter(Boolean));
      el.checked ? on.add(el.value) : on.delete(el.value);
      fields[key] = ["info", "warning", "error"].filter((l) => on.has(l)).join("|");
      return false;
    }
    if (el.tagName === "SELECT" && el.value === "__other") { (st.other ??= {})[key] = true; fields[key] = ""; return true; }
    if (el.type === "checkbox") fields[key] = el.checked;
    else if (el.type === "number") fields[key] = Math.max(0, Math.min(86400, Number(el.value) || 0));
    else if (el.type === "range") fields[key] = Number(el.value);
    else fields[key] = el.value;
    return el.tagName === "SELECT" || key === "intervalS";
  }
  root.addEventListener("input", (e) => {
    const el = e.target;
    if (el.dataset.camSearch) { st.camSearch[el.dataset.camSearch] = el.value; const pos = el.selectionStart; render(); const again = root.querySelector(`[data-cam-search="${el.dataset.camSearch}"]`); again?.focus(); again?.setSelectionRange(pos, pos); return; }
    if (el.dataset.d) { st.draft[el.dataset.d] = el.type === "checkbox" ? el.checked : el.value; if (el.type === "checkbox" || el.tagName === "SELECT") render(); else refreshSummary(); return; }
    if (el.dataset.f && el.type !== "checkbox" && el.tagName !== "SELECT") { setField(el); refreshSummary(); }
  });
  root.addEventListener("change", (e) => {
    const el = e.target;
    if (el.dataset.x === "event-type") { st.draft.event = defaults("event", el.value); st.other = {}; return render(); }
    if (el.dataset.x === "action-type") {
      st.draft.action = el.value === st.rule?.action?.type ? fromRule(st.rule).action : defaults("action", el.value);
      return render();
    }
    if (el.dataset.d && (el.type === "checkbox" || el.tagName === "SELECT")) return;   // handled on input
    if (el.dataset.f && (el.type === "checkbox" || el.tagName === "SELECT")) { setField(el); render(); }
  });
  function refreshSummary() {
    const s = root.querySelector(".rx-summary");
    if (s) s.innerHTML = summary();
  }
  root.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-x]");
    if (!b || b.tagName === "SELECT") return;
    const x = b.dataset.x;
    if (x === "back") {
      if (dirty() && !confirm("Leave without saving your changes to this rule?")) return;
      return onCancel();
    }
    if (x === "retry") return start();
    if (x === "quick") return onQuickEdit?.(st.ruleId);
    if (x === "refresh") { root.querySelector(".rules-head .muted").textContent = "Reading from NX…"; return start(true); }
    if (x === "zones-show") { st.zonesFor = st.zonesFor === b.dataset.cam ? null : b.dataset.cam; return render(); }
    if (x === "zone") {
      const f = st.draft.event.fields, zone = b.dataset.zone;
      const hasField = (k) => kind("event", st.draft.event.type)?.fields.some((x) => x.key === k && x.editable);
      if (b.dataset.type && hasField("eventTypeId")) {
        f.eventTypeId = b.dataset.type;
        if (f.devices && !f.devices.acceptAll && !(f.devices.ids || []).includes(b.dataset.cam)) f.devices.ids = [...(f.devices.ids || []), b.dataset.cam];
        toast(`Event type set to the “${esc(zone)}” zone`);
      } else {
        const key = ["description", "caption"].find(hasField);
        if (!key) return toast("This event type has no text filter: pick the zone's event type instead", { kind: "error" });
        f[key] = { checkType: "containsKeywords", value: zone.includes(" ") ? `"${zone}"` : zone };
        toast(`Only events whose ${key} mentions “${esc(zone)}”`);
      }
      return render();
    }
    if (x === "duplicate") {
      if (dirty() && !confirm("Duplicate with your unsaved changes? The original rule stays as it is in NX.")) return;
      st.ruleId = null; st.rule = null; st.initial = null;
      st.draft.comment = `${st.draft.comment} (copy)`.trim();
      st.saved = "";
      if (!kind("action", st.draft.action.type)?.editable) st.draft.action = { type: "writeToLog", fields: { intervalS: 0 } };
      return render();
    }
    if (x === "delete") {
      if (!confirm(`Delete this rule from ${st.site.name}'s NX server?\n\n${st.draft.comment || kind("event", st.draft.event.type)?.name}\n\nThe portal's audit log keeps a copy of it.`)) return;
      b.disabled = true;
      try {
        const res = await api(`/api/sites/${st.site.id}/rules/${encodeURIComponent(st.ruleId)}`, { method: "DELETE" });
        toast("Rule deleted in NX");
        st.saved = draftJson();
        return onDone(res.rules);
      } catch (err) { st.error = err.message; b.disabled = false; return render(); }
    }
    if (x === "save") {
      const d = st.draft;
      const keepAction = st.rule?.action_locked || (!kind("action", d.action.type)?.editable && st.rule?.action?.type === d.action.type);
      const body = { comment: d.comment, level: d.level, always: d.always, enabled: d.enabled };
      if (partChanged("event")) body.event = d.event;
      if (partChanged("action")) body.action = keepAction ? { type: d.action.type } : d.action;
      if (!confirm(`${st.ruleId ? "Change this rule" : "Create this rule"} on ${st.site.name}'s NX server?`)) return;
      st.busy = true; st.error = ""; render();
      try {
        const res = st.ruleId
          ? await api(`/api/sites/${st.site.id}/rules/${encodeURIComponent(st.ruleId)}`, { method: "PUT", body })
          : await api(`/api/sites/${st.site.id}/rules`, { method: "POST", body });
        toast(st.ruleId ? (res.changed?.length ? `Saved in NX: ${esc(res.changed.join(", "))}` : "Nothing changed") : "Rule created in NX");
        st.saved = draftJson();
        return onDone(res.rules);
      } catch (err) { st.error = err.message; st.busy = false; render(); root.scrollIntoView({ block: "start" }); }
    }
  });

  start();
  return { dirty };
}
