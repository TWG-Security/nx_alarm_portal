// Overview: sites (left), map (center), live alarm feed with active timers (right).
import { CFG, api, esc, toast, fmtTime, relTime, timerHtml, on, openAlarms, openCounts, openAck, alarmQueue, createMap, pinIcon, markerState, STATUS, PRIORITY, setArmed, armCardHtml, inView, tenantTag, multiCompany } from "./common.js";
import { openDrawer, closeDrawer } from "./drawer.js";
import { modelPanel } from "./model_panel.js";
import { mountGuards } from "./guards.js";
import { openRuleEditor } from "./rule_editor.js";
import { guardUpdates } from "./call_updates.js";

const sites = new Map();     // id -> site
const markers = new Map();   // site id -> Leaflet marker
let selectedId = null, feedPriority = "";
let globe = null, globeOpening = false;   // the 3D view (globe.js), loaded on first use
let plan = null;                           // the top-down view (plan.js): the 2D map on aerial imagery with cameras
const view = () => globe || plan;          // whichever camera view is open; both have the same editor API
const SEVERITY = { alarm: 0, warn: 1, offline: 2, ok: 3 };
const DEFAULT_CENTER = [39.8, -98.6];
const lmap = createMap(document.getElementById("map"), { center: DEFAULT_CENTER, zoom: 4 });

// Guards / patrol vehicles (Peplink GPS): markers + trails on this map, a list in the side panel.
const guards = mountGuards(lmap, document.getElementById("guard-list"), { onChange: () => globe?.update() });
const stateOf = (site) => { const c = openCounts(site.id); return { c, marker: markerState(site, c) }; };

// ---------------------------------------------------------------- left: stats, sites, site detail
function renderStats() {
  const c = openCounts();
  let offline = 0;
  for (const s of sites.values()) if (stateOf(s).marker === "offline") offline++;
  document.getElementById("stat-critical").textContent = c[1];
  document.getElementById("stat-alarm").textContent = c[2];
  document.getElementById("stat-warn").textContent = c[3];
  document.getElementById("stat-offline").textContent = offline;
}

// The Sites section folds away like Guards (remembered in this browser). Folded, its header still shows the count
// and, in red, how many alarms are open across the sites.
const SITES_KEY = "twg-sites-collapsed";
let sitesCollapsed = (() => { try { return localStorage.getItem(SITES_KEY) === "1"; } catch { return false; } })();
const CHEVRON = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>';
function renderSitesHead(list) {
  const open = list.reduce((n, { st }) => n + st.c[1] + st.c[2], 0);
  document.getElementById("sites-head").innerHTML = `<button type="button" class="guard-toggle" id="sites-toggle" aria-expanded="${!sitesCollapsed}"
      title="${sitesCollapsed ? "Show" : "Hide"} the sites list"><span class="guard-chev">${CHEVRON}</span><h3>Sites</h3>
      <span class="guard-count">${list.length}</span>${open ? `<span class="badge" title="Open alarms">${open}</span>` : ""}</button>`;
  document.getElementById("site-list").hidden = sitesCollapsed;
}
document.getElementById("sites-head").addEventListener("click", (e) => {
  if (!e.target.closest("#sites-toggle")) return;
  sitesCollapsed = !sitesCollapsed;
  try { localStorage.setItem(SITES_KEY, sitesCollapsed ? "1" : "0"); } catch { /* applied, just not remembered */ }
  renderSites();
});

function renderSites() {
  const ul = document.getElementById("site-list");
  const list = [...sites.values()].map((s) => ({ s, st: stateOf(s) }))
    .sort((a, b) => SEVERITY[a.st.marker] - SEVERITY[b.st.marker] || a.s.name.localeCompare(b.s.name));
  renderSitesHead(list);
  if (!list.length) {
    ul.innerHTML = `<li class="empty">No sites yet.${CFG.isAdmin ? ' <a href="/sites/new">Add the first site</a>.' : ""}</li>`;
    return;
  }
  ul.innerHTML = list.map(({ s, st }) => `
    <li class="site-item ${s.id === selectedId ? "selected" : ""}" data-id="${s.id}" tabindex="0">
      <span class="dot dot--${st.marker}"></span>
      <span class="name" title="${esc(s.tenant_name ? `${s.tenant_name}: ${s.name}` : s.name)}">${esc(s.name)}</span>${tenantTag(s)}
      ${s.arming?.armed === false ? '<span class="chip disarmed" title="Security events are recorded but not raised">Disarmed</span>' : ""}
      ${st.c[1] + st.c[2] ? `<span class="badge">${st.c[1] + st.c[2]}</span>` : ""}
      ${st.c[3] ? `<span class="chip p3">${st.c[3]}</span>` : ""}
      ${s.lat == null ? '<span class="chip" title="No location set">no pin</span>' : ""}
    </li>`).join("");
}

// The site panel (the user, 2026-10-14: "clean nice efficient and more professional"): fixed sections in a fixed
// order. Header (state dot, name, address, close), facts row, view switcher, arming, one NX-rules status line
// (details fold out), a few facts, then the actions. Line icons like the Guards panel.
const ico = (d) => `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const ICON = {
  close: ico('<path d="M6 6l12 12M18 6 6 18"/>'),
  camera: ico('<path d="M3 7h11a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H3z"/><path d="m16 10 5-3v10l-5-3"/>'),
  pulse: ico('<path d="M3 12h4l3-7 4 14 3-7h4"/>'),
  check: ico('<path d="M3 12a9 9 0 1 0 18 0 9 9 0 1 0-18 0"/><path d="m8.5 12 2.5 2.5 4.5-5"/>'),
  warn: ico('<path d="M12 3 2 20h20L12 3z"/><path d="M12 10v4M12 17.5v.01"/>'),
  refresh: ico('<path d="M20 12a8 8 0 1 1-2.3-5.7L20 8.5"/><path d="M20 3.5v5h-5"/>'),
  rules: ico('<path d="M9 6h11M9 12h11M9 18h11"/><path d="M4 6h.01M4 12h.01M4 18h.01"/>'),
  history: ico('<path d="M3 12a9 9 0 1 0 2.7-6.4L3 8"/><path d="M3 3v5h5"/><path d="M12 7.5V12l3 2"/>'),
  edit: ico('<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="m13.5 6.5 4 4"/>'),
  chevron: ico('<path d="M9 6l6 6-6 6"/>'),
};
const MARKER_TXT = { ok: "Normal", warn: "Warnings open", alarm: "Alarms open", offline: "Offline" };
const durTxt = (s) => (s >= 3600 ? `${+(s / 3600).toFixed(1)} h` : s >= 60 ? `${+(s / 60).toFixed(1)} min` : `${s} s`);
let rulesLineOpen = false;     // the NX rules warning stays folded out across re-renders (they come with every alarm)

// NX rules health in one line; the detail folds out (it used to fill half the panel).
function rulesLineHtml(s) {
  const d = s.rule_delays || [], sys = s.system_delays || { count: 0 };
  const btn = '<button type="button" class="btn btn-sm" data-rules-open>Open NX rules</button>';
  if (s.rules_readable === false) {
    return `<details class="sp-status warn" ${rulesLineOpen ? "open" : ""}><summary>${ICON.warn}<span>Can't read NX rules</span>${ICON.chevron}</summary>
      <p>The portal's NX account needs rule-read rights (e.g. Power Users) for #tags, #24h and the repeat-delay check to work.</p>${btn}</details>`;
  }
  if (s.rules_readable == null) return "";
  const sysTxt = sys.count ? `System events (${sys.count} types): repeats held up to ${durTxt(sys.max_s)}${sys.worst ? ` (${sys.worst})` : ""} by NX's default notification rules. Usually fine.` : "";
  if (!d.length) {
    return `<div class="sp-status ok" title="${esc(sysTxt)}">${ICON.check}<span>Security alarms on time</span></div>`;
  }
  return `<details class="sp-status warn" ${rulesLineOpen ? "open" : ""}><summary>${ICON.warn}
      <span>${d.length} NX rule${d.length === 1 ? "" : "s"} delay${d.length === 1 ? "s" : ""} repeat alarms</span><span class="sp-status-more">Review</span>${ICON.chevron}</summary>
    <ul>${d.slice(0, 8).map((r) => `<li>${esc(r.name)}: repeats up to <b>${durTxt(r.interval_s)}</b> late</li>`).join("")}
      ${d.length > 8 ? `<li>…and ${d.length - 8} more</li>` : ""}</ul>
    <p><b>Fix:</b> in NX, add a rule for the same event with action <b>Write to log</b> and <b>Interval of action</b> off. Existing notification rules can keep their limits.</p>
    ${sysTxt ? `<p class="muted">${esc(sysTxt)}</p>` : ""}${btn}</details>`;
}

// Which camera view the map pane shows: the switcher marks it.
const currentView = () => (globe || globeOpening ? "3d" : plan ? "plan" : "map");

function renderDetail() {
  const box = document.getElementById("site-detail");
  const s = sites.get(selectedId);
  if (!s) { box.hidden = true; return; }
  box.hidden = false;
  const { c, marker } = stateOf(s);
  const open = c[1] + c[2], warns = c[3], v = currentView();
  const viewBtn = (key, attr, label, title) =>
    `<button type="button" ${attr} class="${v === key ? "active" : ""}" aria-pressed="${v === key}" title="${title}">${label}</button>`;
  box.innerHTML = `
    <div class="sp-head">
      <span class="dot dot--${marker}" title="${MARKER_TXT[marker]}"></span>
      <div class="sp-title">
        <h2>${esc(s.name)}</h2>
        ${multiCompany ? `<div class="sp-company">${esc(s.tenant_name)}</div>` : ""}
        <div class="muted">${esc(s.address || "No address")}</div>
      </div>
      <button type="button" class="sp-close" id="close-detail" aria-label="Close" title="Back to all sites">${ICON.close}</button>
    </div>
    <div class="sp-facts">
      <span class="chip ${s.status}">${esc(STATUS[s.status] || s.status)}</span>${s.enabled ? "" : '<span class="chip">Disabled</span>'}
      ${open ? `<span class="chip p2">${open} open</span>` : ""}${warns ? `<span class="chip p3">${warns} warning${warns === 1 ? "" : "s"}</span>` : ""}
      <span class="sp-fact" title="Cameras">${ICON.camera}${s.camera_count}</span>
      <span class="sp-fact" title="Last contact with NX${s.push ? " · alarms arrive by live push" : s.push === false ? " · polling only (5 s)" : ""}">${ICON.pulse}${relTime(s.last_seen_at)}${s.push === false ? " · polling" : ""}</span>
    </div>
    ${s.status_detail ? `<div class="sp-detail ${s.status === "online" ? "" : "bad"}">${esc(s.status_detail)}</div>` : ""}
    ${s.lat != null ? `<div class="seg sp-views" role="group" aria-label="View">
      ${viewBtn("map", "data-view-map", "Map", "Street map around this site")}
      ${viewBtn("plan", "data-plan", "Top-down", "Aerial view from straight above, with this site's cameras")}
      ${viewBtn("3d", "data-globe", "3D", "Fly into a 3D aerial view of this site")}</div>` : ""}
    ${armCardHtml(s)}
    ${rulesLineHtml(s)}
    <dl class="sp-kv">
      <dt>NX</dt><dd>${esc(s.nx_site_name || "—")}${s.nx_version ? ` <span class="muted">v${esc(s.nx_version)}</span>` : ""}</dd>
      ${s.notes ? `<dt>Notes</dt><dd class="sp-notes">${esc(s.notes)}</dd>` : ""}
    </dl>
    <div class="sp-actions">
      <button class="btn btn-sm" type="button" data-check title="Ask NX now: status, cameras, rules and new events (instead of waiting for the next refresh)">${ICON.refresh}<span>Check now</span></button>
      <button class="btn btn-sm" type="button" data-rules title="Every NX rule the portal read for this site, and what it does here">${ICON.rules}<span>NX rules</span></button>
      <a class="btn btn-sm" href="/alarms?site_id=${s.id}&state=all" title="Every alarm from this site">${ICON.history}<span>History</span></a>
      ${CFG.isAdmin ? `<a class="btn btn-sm" href="/sites/${s.id}/edit">${ICON.edit}<span>Edit site</span></a>` : ""}
    </div>`;
}
document.getElementById("site-detail").addEventListener("toggle", (e) => {
  if (e.target.matches?.(".sp-status")) rulesLineOpen = e.target.open;
}, true);

// ---------------------------------------------------------------- site check + NX rules list
const LEVEL_CHIP = { critical: '<span class="chip p1">Critical</span>', alarm: '<span class="chip p2">Alarm</span>',
  warning: '<span class="chip p3">Warning</span>', ignore: '<span class="chip">Ignored</span>', off: '<span class="chip">Off in NX</span>' };
const rulesDlg = document.createElement("dialog");
rulesDlg.className = "rules-dialog";
document.body.append(rulesDlg);
// View filter (remembered in this browser) and, for administrators, the company switch and per-rule editing.
const RULES_FILTER_KEY = "twg-rules-filter";
let rulesFilter = (() => { try { return localStorage.getItem(RULES_FILTER_KEY) || "all"; } catch { return "all"; } })();
let rulesState = null;          // { s, v } the list on screen
let editingRule = null;         // rule id being edited
const stripTags = (t) => (t || "").replace(/(^|\s)#(critical|alarm|warning|ignore|24h)\b/gi, " ").replace(/\s{2,}/g, " ").trim();
function editorRow(r) {
  return `<tr class="rule-editor"><td colspan="4"><div class="rule-edit-grid" data-rule-form="${esc(r.id)}">
      <label>Level in the portal <select data-re="level">${[["none", "No tag (event type decides; ignored with tagged rules only)"], ["critical", "#critical"], ["alarm", "#alarm"], ["warning", "#warning"], ["ignore", "#ignore"]]
        .map(([v, l]) => `<option value="${v}" ${(r.tag || "none") === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
      <label class="check"><input type="checkbox" data-re="always" ${r.always ? "checked" : ""}> <span><b>#24h</b>: raise even while the site is disarmed</span></label>
      <label>Title / Comment (tags are added for you) <input type="text" data-re="comment" maxlength="400" value="${esc(stripTags(r.comment))}"></label>
      <label class="check"><input type="checkbox" data-re="enabled" ${r.enabled ? "checked" : ""}> <span>Rule is on in NX</span></label>
      <label>Interval of action (seconds; 0 = off, every event comes through at once)
        <input type="number" min="0" max="86400" data-re="interval" value="${r.interval_s}" ${r.action_has_login ? "disabled" : ""}>
        ${r.action_has_login ? '<small class="muted">This rule\'s action holds a login: change its interval in Nx Witness.</small>' : r.interval_s ? '<small class="text-warn">Over 0 holds repeat events back.</small>' : ""}</label>
      <div class="rule-edit-actions"><button type="button" class="btn btn-sm" data-re-cancel>Cancel</button>
        <button type="button" class="btn btn-sm btn-primary" data-re-save>Save in NX</button></div>
    </div></td></tr>`;
}
function paintRules(s, v, error = "") {
  rulesState = { s, v };
  const raised = v.rules.filter((r) => !["ignore", "off"].includes(r.level)).length;
  const shown = rulesFilter === "tagged" ? v.rules.filter((r) => r.tag) : v.rules;
  const admin = CFG.isAdmin;
  rulesDlg.innerHTML = `<div class="rules-head"><div><h2>NX rules · ${esc(s.name)}</h2>
      <div class="muted small-print">${v.read_at ? `Read from NX ${esc(relTime(v.read_at * 1000))}` : "Not read yet"} · ${v.rules.length} rules, ${raised} raise alarms here
      ${v.tagged_only ? " · <b>tagged rules only</b> (Settings → Alarm levels)" : ""}</div></div>
      <div style="display:flex;gap:6px;flex:0 0 auto;align-items:center;flex-wrap:wrap;justify-content:flex-end">
      <div class="seg" role="group" aria-label="Show"><button type="button" data-rules-filter="all" class="${rulesFilter === "all" ? "active" : ""}">All rules</button>
        <button type="button" data-rules-filter="tagged" class="${rulesFilter === "tagged" ? "active" : ""}">Tagged only (${v.rules.filter((r) => r.tag).length})</button></div>
      ${admin && v.readable !== false ? '<button type="button" class="btn btn-sm btn-primary" data-rule-new>+ New rule</button>' : ""}
      <button type="button" class="btn btn-sm" data-rules-check>Check now</button>
      <button type="button" class="btn btn-sm" data-rules-close aria-label="Close">✕</button></div></div>
    ${error ? `<div class="alert alert-error">Check failed: ${esc(error)}</div>` : ""}
    ${admin ? `<label class="check rules-switch"><input type="checkbox" data-rules-tagged-only ${v.tagged_only ? "checked" : ""}>
      <span><b>Only NX rules tagged #critical, #alarm or #warning raise alarms</b> <span class="muted">(every site of this company; same as Settings → Alarm levels)</span></span></label>` : ""}
    ${v.readable === false ? `<div class="alert alert-error"><b>This site's NX account can't read rules</b> (NX answered 403), so #tags can't be seen${v.tagged_only
      ? " and <b>every event comes in</b>: tagged rules only can't be applied here" : ""}. Give the NX account rule-read rights (e.g. the Power Users group).</div>` : ""}
    ${shown.length ? `<div class="table-wrap"><table class="rules-table"><thead><tr><th>In the portal</th><th>NX rule (Title / Comment)</th><th>Event</th><th>Notes</th></tr></thead><tbody>
      ${shown.map((r) => `<tr class="${["ignore", "off"].includes(r.level) ? "rule-off" : ""}" data-rule-row="${esc(r.id)}">
        <td>${LEVEL_CHIP[r.level] || esc(r.level)}<div class="muted small-print">${esc(r.why)}</div></td>
        <td><b>${esc(r.name)}</b>${r.tag || r.always ? `<div class="small-print">${r.tag ? `<code>#${esc(r.tag)}</code> ` : ""}${r.always ? "<code>#24h</code>" : ""}</div>` : ""}</td>
        <td>${esc(r.event_label)}<div class="muted small-print mono">${esc(r.event_type)} → ${esc(r.action)}</div></td>
        <td class="small-print">${r.delayed ? `<span class="text-warn">Repeats held back up to ${r.interval_s} s (Interval of action)</span>` : ""}
          ${r.always && v.tagged_only && !r.tag ? '<span class="text-warn">#24h but no level tag: ignored</span>' : ""}
          ${admin && v.readable !== false ? `<div><button type="button" class="btn btn-sm" data-rule-edit="${esc(r.id)}">Edit</button></div>` : ""}</td></tr>
        ${editingRule === r.id ? editorRow(r) : ""}`).join("")}
      </tbody></table></div>` : `<p class="muted">${v.readable === false ? "" : rulesFilter === "tagged" && v.rules.length ? "No tagged rules on this site." : "No rules read yet. Press Check now."}</p>`}`;
}
async function openRules(siteId, check = false) {
  const s = sites.get(siteId);
  if (!s) return;
  if (!rulesDlg.open) rulesDlg.showModal();
  ruleEditor = null;
  rulesDlg.dataset.site = siteId;
  rulesDlg.innerHTML = `<p class="muted">${check ? "Checking with NX…" : "Loading…"}</p>`;
  try {
    if (check) {
      const r = await api(`/api/sites/${siteId}/check`, { method: "POST" });
      saveSiteData(r.site);
      paintRules(sites.get(siteId), r.rules, r.error);
    } else paintRules(s, await api(`/api/sites/${siteId}/rules`));
  } catch (err) { rulesDlg.innerHTML = `<div class="alert alert-error">${esc(err.message)}</div><button type="button" class="btn btn-sm" data-rules-close>Close</button>`; }
}
function saveSiteData(data) { if (data) { sites.set(data.id, { ...sites.get(data.id), ...data }); renderAll(); } }
// The full rule editor (rule_editor.js) takes over the dialog; "Quick edit" (tags, on/off, interval) is the fallback
// when the site's NX can't describe its rule engine.
let ruleEditor = null;
function openEditor(ruleId) {
  const { s } = rulesState;
  editingRule = null;
  const back = (v) => { ruleEditor = null; paintRules(sites.get(s.id) || s, v || rulesState.v); };
  ruleEditor = openRuleEditor(rulesDlg, { site: s, ruleId, onDone: (v) => back(v), onCancel: () => back(),
    onQuickEdit: (id) => { ruleEditor = null; editingRule = id; paintRules(rulesState.s, rulesState.v); } });
}
const editorDirty = () => ruleEditor?.dirty() && !confirm("Close without saving your changes to this rule?");
rulesDlg.addEventListener("cancel", (e) => { if (editorDirty()) e.preventDefault(); else ruleEditor = null; });
rulesDlg.addEventListener("click", async (e) => {
  if (e.target === rulesDlg && ruleEditor) { if (editorDirty()) return; ruleEditor = null; return rulesDlg.close(); }
  if (ruleEditor) return;                                    // the editor handles its own clicks
  if (e.target.closest("[data-rules-close]") || e.target === rulesDlg) { editingRule = null; return rulesDlg.close(); }
  if (e.target.closest("[data-rules-check]")) return openRules(Number(rulesDlg.dataset.site), true);
  const f = e.target.closest("[data-rules-filter]");
  if (f && rulesState) {
    rulesFilter = f.dataset.rulesFilter;
    try { localStorage.setItem(RULES_FILTER_KEY, rulesFilter); } catch { /* applied, just not remembered */ }
    return paintRules(rulesState.s, rulesState.v);
  }
  const ed = e.target.closest("[data-rule-edit]");
  if (ed && rulesState) return openEditor(ed.dataset.ruleEdit);
  if (e.target.closest("[data-rule-new]") && rulesState) return openEditor(null);
  if (e.target.closest("[data-re-cancel]") && rulesState) { editingRule = null; return paintRules(rulesState.s, rulesState.v); }
  const save = e.target.closest("[data-re-save]");
  if (save && rulesState) {
    const form = save.closest("[data-rule-form]"), id = form.dataset.ruleForm, r = rulesState.v.rules.find((x) => x.id === id);
    const val = (k) => form.querySelector(`[data-re="${k}"]`);
    const body = { level: val("level").value, always: val("always").checked, comment: val("comment").value.trim(), enabled: val("enabled").checked };
    if (!val("interval").disabled) body.interval_s = Math.max(0, Math.min(86400, Number(val("interval").value) || 0));
    if (!confirm(`Change this rule on ${rulesState.s.name}'s NX server?\n\n${r.name}`)) return;
    save.disabled = true; save.textContent = "Saving…";
    try {
      const res = await api(`/api/sites/${rulesState.s.id}/rules/${encodeURIComponent(id)}`, { method: "PATCH", body });
      editingRule = null;
      paintRules(sites.get(rulesState.s.id) || rulesState.s, res.rules);
      toast(res.changed.length ? `Saved in NX: ${esc(res.changed.join(", "))}` : "Nothing changed");
    } catch (err) { toast(esc(err.message), { kind: "error" }); save.disabled = false; save.textContent = "Save in NX"; }
  }
});
rulesDlg.addEventListener("change", async (e) => {
  if (!e.target.matches("[data-rules-tagged-only]") || !rulesState) return;
  const on = e.target.checked;
  if (!confirm(on ? "Only NX rules tagged #critical, #alarm or #warning will raise alarms, on every site of this company. Events from other rules are ignored. Turn it on?"
                  : "Every NX rule will raise alarms by its event type again. Turn tagged rules only off?")) { e.target.checked = !on; return; }
  try {
    await api("/api/settings/tagged-only", { method: "PUT", body: { on } });
    paintRules(rulesState.s, await api(`/api/sites/${rulesState.s.id}/rules`));
    toast(on ? "Tagged rules only: on" : "Tagged rules only: off");
  } catch (err) { toast(esc(err.message), { kind: "error" }); e.target.checked = !on; }
});
document.getElementById("site-detail").addEventListener("click", async (e) => {
  const s = sites.get(selectedId);
  if (!s) return;
  if (e.target.closest("[data-rules], [data-rules-open]")) return openRules(s.id);
  const btn = e.target.closest("[data-check]");
  if (!btn) return;
  const label = btn.querySelector("span") || btn;
  btn.disabled = true; label.textContent = "Checking…";
  try {
    const r = await api(`/api/sites/${s.id}/check`, { method: "POST" });
    saveSiteData(r.site);
    toast(r.error ? `${esc(s.name)}: ${esc(r.error)}` : `${esc(s.name)} checked: ${esc(STATUS[r.site.status] || r.site.status)}, ${r.site.camera_count} cameras, ${r.rules.rules.length} NX rules`,
          r.error ? { kind: "error" } : {});
  } catch (err) { toast(esc(err.message), { kind: "error" }); }
  finally { btn.disabled = false; label.textContent = "Check now"; }
});

// ---------------------------------------------------------------- right: live feed
const FEED_MAX = 100;
function feedRows() {
  return alarmQueue((a) => !selectedId || a.site_id === selectedId);       // critical first, then oldest first
}

function renderFeed() {
  const all = feedRows();
  const counts = { "": all.length, 1: 0, 2: 0, 3: 0 };
  all.forEach((a) => counts[a.priority]++);
  document.querySelectorAll("#feed-tabs button").forEach((b) => {
    b.classList.toggle("active", b.dataset.p === feedPriority);
    b.querySelector("span").textContent = counts[b.dataset.p] || "";
  });
  const filt = document.getElementById("feed-site-filter");
  const site = sites.get(selectedId);
  filt.hidden = !site;
  if (site) filt.innerHTML = `Showing <b>${esc(site.name)}</b> only <button class="btn btn-sm" id="clear-site">Show all sites</button>`;

  const rows = feedPriority ? all.filter((a) => String(a.priority) === feedPriority) : all;
  const ul = document.getElementById("feed-list");
  if (!rows.length) {
    ul.innerHTML = `<li class="empty feed-clear"><span class="dot dot--ok"></span> ${all.length ? "Nothing at this level." : "All clear. No open alarms."}</li>`;
    return;
  }
  // Draw the top FEED_MAX only (criticals first): a 500-card feed took ~2 s of layout per redraw, which
  // held back the critical pop-up by ~3 s. The tab counts above stay exact.
  const more = rows.length - FEED_MAX;
  ul.innerHTML = rows.slice(0, FEED_MAX).map((a) => `
    <li class="feed-item p${a.priority}" data-id="${a.id}" tabindex="0">
      <div class="feed-top"><span class="chip p${a.priority}">${PRIORITY[a.priority]}</span>${timerHtml(a.event_ts_ms)}</div>
      <div class="title">${esc(a.caption)}</div>
      <div class="feed-where">${tenantTag(a)}<b>${esc(a.site_name)}</b>${a.site_address ? ` · ${esc(a.site_address)}` : ""}</div>
      <div class="meta">${a.source_name ? `${esc(a.source_name)} · ` : ""}${fmtTime(a.event_ts_ms)}</div>
      <div class="feed-actions"><button class="btn btn-sm btn-primary" data-ack="${a.id}">Acknowledge</button>
        <button class="btn btn-sm" data-detail="${a.id}">Details</button></div>
    </li>`).join("") + (more > 0 ? `<li class="empty feed-more">+${more} more open. <a href="/alarms?state=open">See them all on the Alarms page</a></li>` : "");
}

// ---------------------------------------------------------------- map
function upsertMarker(site) {
  const existing = markers.get(site.id);
  if (site.lat == null || site.lng == null) {
    if (existing) { existing.remove(); markers.delete(site.id); }
    return;
  }
  const { c, marker } = stateOf(site);
  const icon = pinIcon(marker, c[1] + c[2] + c[3], site.name, site.id === selectedId, site.arming?.armed === false);
  const title = `${site.name}: ${marker === "offline" ? STATUS[site.status] || "Offline" : `${c[1] + c[2]} alarm(s), ${c[3]} warning(s)`}`;
  const z = (3 - SEVERITY[marker]) * 1000;
  if (existing) {
    existing.setLatLng([site.lat, site.lng]).setIcon(icon).setZIndexOffset(z);
    return;
  }
  const m = L.marker([site.lat, site.lng], { icon, title, zIndexOffset: z, keyboard: true, alt: site.name }).addTo(lmap);
  m.on("click", () => select(site.id));
  markers.set(site.id, m);
}

function fitToSites() {
  const pts = [...sites.values()].filter((s) => s.lat != null).map((s) => [s.lat, s.lng]);
  if (!pts.length) return lmap.setView(DEFAULT_CENTER, 4);
  if (pts.length === 1) return lmap.setView(pts[0], 13);
  lmap.fitBounds(pts, { padding: [60, 60], maxZoom: 15 });
}

function renderAll() {
  renderStats(); renderSites(); renderDetail(); renderFeed();
  for (const s of sites.values()) upsertMarker(s);
  globe?.update(); plan?.update(); renderHud();
  if (globe) paintModelBtn();
}

// Picking a site (list or pin) goes straight to its top-down view with the cameras (the user's ask,
// 2026-10-12); in the 3D view it flies there instead. A site without a location is only selected.
function select(id, pan = true) {
  selectedId = id;
  renderAll();
  models.siteChanged();
  const s = sites.get(id);
  if (!pan || s?.lat == null) return;
  if (globe) globe.flyTo(s);
  else openPlanView(s);
}

// Back to all sites: leave the top-down view for the street-map overview.
function deselect() {
  selectedId = null;
  if (plan) { closePlanView(); fitToSites(); }
  renderAll();
}

// ---------------------------------------------------------------- 3D view (over the map pane)
const $id = (x) => document.getElementById(x);
const mapCanvas = document.querySelector(".map-canvas"), globeEl = $id("globe"), globeBar = $id("globe-bar"), globeMsg = $id("globe-msg"), hud = $id("hud");
const camPanel = $id("cam-panel"), camPlace = $id("cam-place"), camsBtn = $id("globe-cams"), camHover = $id("cam-hover");
let hudClock = null, camLive = null, camOpen = null;   // camOpen = { siteId, deviceId }

const globePins = () => [...sites.values()].map((s) => {
  const { c, marker } = stateOf(s);
  return { id: s.id, name: s.name, lat: s.lat, lng: s.lng, state: marker, count: c[1] + c[2] + c[3], disarmed: s.arming?.armed === false };
});
const camAlarm = (siteId, deviceId) => [...openAlarms.values()].find((a) => a.site_id === siteId && a.device_id === deviceId && inView(a));
const globeModels = () => [...sites.values()].filter((s) => s.model?.ready).map((s) =>
  ({ siteId: s.id, url: s.model.ready.url, anchor: s.model.ready.anchor, placement: s.model.ready.placement }));
const globeCams = () => [...sites.values()].flatMap((s) => Object.entries(s.camera_places || {}).map(([deviceId, p]) =>
  ({ siteId: s.id, deviceId, ...p, alarm: !!camAlarm(s.id, deviceId) })));

// HUD text: the selected site, a UTC clock, counts, and where the view is centred.
const STATE_TXT = { ok: "NORMAL", warn: "WARNING", alarm: "ALARM", offline: "OFFLINE" };
function renderHud() {
  if (!globe) return;
  const s = sites.get(selectedId);
  if (s) {
    const { c, marker } = stateOf(s);
    const n = c[1] + c[2] + c[3];
    $id("hud-site").innerHTML = `${esc(s.name.toUpperCase())}<br><span class="hud-sub">${esc((s.address || "").toUpperCase())}</span><br>
      <span class="st ${marker}">${STATE_TXT[marker]}${n ? ` · ${n} OPEN` : ""}</span>${s.arming?.armed === false ? ' <span class="st warn">DISARMED</span>' : ""}`;
  } else $id("hud-site").textContent = "";
  const open = [...openAlarms.values()].filter(inView).length;
  const placed = [...sites.values()].reduce((n, x) => n + Object.keys(x.camera_places || {}).length, 0);
  $id("hud-counts").textContent = `SITES ${sites.size} · CAMERAS ${placed} · OPEN ALARMS ${open}`;
}
const dms = (v, pos, neg) => {
  const a = Math.abs(v), d = Math.floor(a), m = Math.floor((a - d) * 60), sec = ((a - d) * 60 - m) * 60;
  return `${d}°${String(m).padStart(2, "0")}'${sec.toFixed(1).padStart(4, "0")}"${v >= 0 ? pos : neg}`;
};
function renderCoords({ lat, lng, alt, heading }) {
  $id("hud-coords").innerHTML = `${dms(lat, "N", "S")}  ${dms(lng, "E", "W")}<br>
    ${lat.toFixed(5)}, ${lng.toFixed(5)}<br>ALT ${Math.round(alt).toLocaleString()} M · HDG ${String(Math.round(heading) % 360).padStart(3, "0")}°`;
}
const tickClock = () => { $id("hud-clock").textContent = new Date().toISOString().replace("T", " ").slice(0, 19) + " UTC"; };

async function openGlobeView(site) {
  if (globe) { globe.flyTo(site); return; }
  if (globeOpening) return;
  closePlanView();
  globeOpening = true;
  renderDetail();
  globeEl.hidden = false; globeBar.hidden = false;
  globeMsg.textContent = "Loading 3D view…"; globeMsg.hidden = false;
  try {
    const { openGlobe } = await import("./globe.js");
    globe = await openGlobe(globeEl, {
      pins: globePins, cameras: globeCams, models: globeModels, guards: () => guards.list(),
      onPick: (id) => select(id),
      onPickCamera: pickCamera,
      onView: renderCoords,
      onTooSlow: (why) => {
        closeGlobeView();
        toast(`3D view closed to keep alarms fast: ${esc(why)}. The 2D map is back.`, { kind: "warn", timeout: 10000 });
      },
    });
    window.__globeStats = globe.stats;
    window.__globe = globe.viewer;   // for tools/e2e/globe.py and debugging
    globeMsg.hidden = true;
    hud.hidden = false; camsBtn.hidden = !CFG.isAdmin;
    $id("globe-model").hidden = !CFG.isAdmin;
    globe.showModels(modelsOn); paintModelBtn();
    $id("globe-close").textContent = "2D map";
    $id("imagery-3d").textContent = globe.hasBuildings ? "3D" : "Satellite";
    showImagery("sat");
    tickClock(); hudClock = setInterval(tickClock, 1000);
    renderHud();
    globe.flyTo(sites.get(selectedId) || site, { fromSpace: true });
  } catch (err) {
    console.error("3D view failed", err);
    closeGlobeView();
    toast(`3D view isn't available on this screen (${esc(err.message || String(err))}). The 2D map and alarms are unaffected.`, { kind: "error", timeout: 8000 });
  } finally {
    globeOpening = false;
    renderDetail();
  }
}

function closeGlobeView() {
  if (cv) { cv.dirty = false; closeCamView(); }
  closeCamPanel(); closePlacing(); models.hide();
  $id("globe-model").hidden = true; $id("globe-model-show").hidden = true;
  try { globe?.destroy(); } catch (err) { console.error(err); }
  globe = null; delete window.__globe; delete window.__globeStats;
  clearInterval(hudClock); hudClock = null;
  globeEl.hidden = true; globeBar.hidden = true; globeMsg.hidden = true; hud.hidden = true;
  globeEl.replaceChildren();
  renderDetail();
}

// ---- top-down view: the 2D map itself, on aerial imagery and zoomed into the site, with cameras
// drawn flat (no WebGL, so no graphics check or watchdog needed; see plan.js)
// deviceId: centre on that camera instead of the whole site (opening an alarm).
async function openPlanView(site, deviceId = null) {
  const go = () => (deviceId ? plan.focusCamera(site, deviceId) : plan.flyTo(site));
  if (plan) { go(); return; }
  if (globe) closeGlobeView();
  try {
    const { openPlan } = await import("./plan.js");
    if (plan || globe || globeOpening) return;     // 3D was picked while this loaded: it wins
    plan = openPlan(lmap, { cameras: globeCams, onPickCamera: pickCamera, onHoverCamera: hoverCamera });
    plan.setCones(conesOn());
    window.__plan = plan;   // for tools/e2e/topdown.py
  } catch (err) {
    console.error("top-down view failed", err);
    toast(`Top-down view didn't open (${esc(err.message || String(err))}). The map and alarms are unaffected.`, { kind: "error", timeout: 8000 });
    return;
  }
  mapCanvas.classList.add("plan-open");
  globeBar.hidden = false; camsBtn.hidden = !CFG.isAdmin;
  $id("globe-close").textContent = "Exit top-down";
  paintConesBtn(); $id("plan-cones").hidden = false;
  $id("imagery-3d").textContent = "Satellite";
  showImagery("sat");
  go();
  renderDetail();
}

// "Map" in the site panel: leave top-down / 3D for the street map, still on this site.
function showMapView(site) {
  if (globe) closeGlobeView();
  closePlanView();
  if (site.lat != null) lmap.setView([site.lat, site.lng], Math.max(lmap.getZoom(), 16));
}

function closePlanView() {
  if (!plan) return;
  hideHover(); closeCamPanel(); closePlacing();
  try { plan.destroy(); } catch (err) { console.error(err); }
  plan = null; delete window.__plan;
  mapCanvas.classList.remove("plan-open");
  globeBar.hidden = true; $id("plan-cones").hidden = true;
  renderDetail();
}

// ---- site 3D models (globe.js draws them; model_panel.js uploads and places them, admins)
const models = modelPanel({
  panel: $id("model-panel"), button: $id("globe-model"), site: () => sites.get(selectedId) || null, globe: () => globe,
  onSite: (data) => { sites.set(data.id, { ...sites.get(data.id), ...data }); renderAll(); },
});
const MODELS_KEY = "twg-3d-models";
let modelsOn = (() => { try { return localStorage.getItem(MODELS_KEY) !== "0"; } catch { return true; } })();
function paintModelBtn() {
  const b = $id("globe-model-show");
  b.hidden = !globe || ![...sites.values()].some((s) => s.model?.ready);
  b.textContent = modelsOn ? "Model: on" : "Model: off";
  b.setAttribute("aria-pressed", String(modelsOn));
}
function toggleModels() {
  modelsOn = !modelsOn;
  try { localStorage.setItem(MODELS_KEY, modelsOn ? "1" : "0"); } catch { /* applied, just not remembered */ }
  globe?.showModels(modelsOn); paintModelBtn();
}

// View cones on/off (top-down), remembered in this browser when storage allows; default on.
const CONES_KEY = "twg-plan-cones";
let conesState = (() => { try { return localStorage.getItem(CONES_KEY) !== "0"; } catch { return true; } })();
const conesOn = () => conesState;
function paintConesBtn() {
  const b = $id("plan-cones");
  b.textContent = conesState ? "Cones: on" : "Cones: off";
  b.setAttribute("aria-pressed", String(conesState));
}
function toggleCones() {
  conesState = !conesState;
  try { localStorage.setItem(CONES_KEY, conesState ? "1" : "0"); } catch { /* applied, just not remembered */ }
  plan?.setCones(conesState); paintConesBtn();
}
const showImagery = (kind) => globeBar.querySelectorAll("[data-imagery]").forEach((b) => b.classList.toggle("active", b.dataset.imagery === kind));

// A camera was clicked on either view: edit it while placing that site's cameras, else watch it live.
function pickCamera(siteId, dev) {
  hideHover();
  if (placingSite === siteId) return startCamEdit(dev, sites.get(siteId)?.camera_places?.[dev]?.name || "Camera");
  openCamPanel(siteId, dev);
}

// ---- live preview on hover (top-down view). A short delay so sweeping the mouse across the map
// doesn't open streams; leaving the camera closes it (and its stream) after a moment, so moving
// from the dot to the cone doesn't flicker. Not while placing cameras or for the pinned one.
const HOVER_DELAY_MS = 250, HOVER_GRACE_MS = 200;
let hover = null;     // { key, siteId, deviceId, pt, showTimer, hideTimer, live }
function hoverCamera(siteId, deviceId, pt) {
  const key = `${siteId}:${deviceId}`;
  if (!pt) {
    if (hover?.key === key) { clearTimeout(hover.hideTimer); hover.hideTimer = setTimeout(hideHover, HOVER_GRACE_MS); }
    return;
  }
  if (hover?.key === key) { clearTimeout(hover.hideTimer); return; }
  hideHover();
  if (placingSite != null || (camOpen?.siteId === siteId && camOpen?.deviceId === deviceId)) return;
  hover = { key, siteId, deviceId, pt, showTimer: setTimeout(showHover, HOVER_DELAY_MS) };
}
async function showHover() {
  const h = hover, s = sites.get(h?.siteId), p = s?.camera_places?.[h?.deviceId];
  if (!p) return hideHover();
  const a = camAlarm(h.siteId, h.deviceId);
  camHover.classList.toggle("cam-alarm", !!a);
  camHover.innerHTML = `<div class="cam-head"><span><b>${esc(p.name || "Camera")}</b> <span class="muted">· ${a ? "ALARM" : "click to keep open"}</span></span></div>
    <div class="cam-live"></div>`;
  // Beside the mouse, flipped to stay inside the map.
  const W = 320, H = 222, M = 8, box = mapCanvas.getBoundingClientRect();
  let x = h.pt.x + 18, y = h.pt.y - H - 12;
  if (x + W > box.width - M) x = h.pt.x - 18 - W;
  if (y < M) y = h.pt.y + 18;
  camHover.style.left = `${Math.max(M, Math.min(x, box.width - W - M))}px`;
  camHover.style.top = `${Math.max(M, Math.min(y, box.height - H - M))}px`;
  camHover.hidden = false;
  const { mountLive } = await import("./live.js");
  if (hover !== h) return;
  h.live = mountLive(camHover.querySelector(".cam-live"), { id: 0, device_id: h.deviceId },
    { label: false, poster: true, base: `/media/sites/${h.siteId}/cameras/${encodeURIComponent(h.deviceId)}` });
}
function hideHover() {
  if (!hover) return;
  clearTimeout(hover.showTimer); clearTimeout(hover.hideTimer);
  try { hover.live?.destroy(); } catch (err) { console.error(err); }
  hover = null;
  camHover.hidden = true; camHover.replaceChildren();
}
lmap.on("movestart zoomstart", hideHover);

// ---- live camera window: click a camera on the 3D or top-down view
async function openCamPanel(siteId, deviceId) {
  const s = sites.get(siteId), p = s?.camera_places?.[deviceId];
  if (!p) return;
  closeCamPanel();
  camOpen = { siteId, deviceId };
  const a = camAlarm(siteId, deviceId);
  camPanel.classList.toggle("cam-alarm", !!a);
  camPanel.innerHTML = `<div class="cam-head"><span><b>${esc(p.name || "Camera")}</b> <span class="muted">· ${esc(s.name)}</span></span>
      <span>${a ? `<button type="button" data-cam-alarm="${a.id}" title="Open this alarm">ALARM ▸</button>` : ""}<button type="button" data-cam-close aria-label="Close">✕</button></span></div>
    <div class="cam-live"></div>
    ${globe && (p.fov || 60) < 180 ? `<div class="cam-actions"><button type="button" class="btn btn-sm" data-cam-view>View from camera</button>
      ${CFG.isAdmin ? '<button type="button" class="btn btn-sm" data-cam-calibrate title="Line the camera\'s picture up with the 3D view">Calibrate</button>' : ""}</div>` : ""}`;
  globe?.selectCamera(siteId, deviceId);
  camPanel.hidden = false;
  const { mountLive } = await import("./live.js");
  if (camOpen?.deviceId !== deviceId) return;
  camLive = mountLive(camPanel.querySelector(".cam-live"), { id: 0, device_id: deviceId },
    { label: false, base: `/media/sites/${siteId}/cameras/${encodeURIComponent(deviceId)}` });
}
function closeCamPanel() {
  globe?.selectCamera(null);
  try { camLive?.destroy(); } catch (err) { console.error(err); }
  camLive = null; camOpen = null; camPanel.hidden = true; camPanel.replaceChildren();
}
camPanel.addEventListener("click", (e) => {
  if (e.target.closest("[data-cam-close]")) return closeCamPanel();
  if (e.target.closest("[data-cam-view]") && camOpen) return openCamView(camOpen.siteId, camOpen.deviceId, false);
  if (e.target.closest("[data-cam-calibrate]") && camOpen) return openCamView(camOpen.siteId, camOpen.deviceId, true);
  const id = e.target.closest("[data-cam-alarm]")?.dataset.camAlarm;
  if (id) { const a = openAlarms.get(Number(id)); if (a) focusAlarm(a); }
});

// ---- View from camera + Calibrate (3D view). The 3D view jumps to the camera's pose (globe.viewFromCamera) and the
// camera's live picture lies over it at the same width, half see-through. Calibrate (admins): drag on the picture
// to turn / tilt, scroll to zoom (field of view), arrow keys for fine steps, numbers for height, roll and position,
// until the picture and the 3D scene line up; Save stores the pose (height_m, tilt, roll, heading, fov, lat/lng).
const camViewEl = $id("cam-view"), camViewImg = $id("cam-view-img"), camViewDrag = $id("cam-view-drag"), camCal = $id("cam-cal");
let cv = null;      // { siteId, deviceId, ctl, start, timer, calibrating, dirty }
const CAL_FIELDS = [["heading", "Turn", "°", 0.5], ["tilt", "Tilt down", "°", 0.25], ["fov", "Width (field of view)", "°", 0.5],
                    ["roll", "Roll", "°", 0.25], ["height_m", "Height above ground", "m", 0.1]];
function fitPicture() {
  if (!cv) return;
  const box = camViewEl.getBoundingClientRect(), aspect = camViewImg.naturalWidth ? camViewImg.naturalWidth / camViewImg.naturalHeight : 16 / 9;
  const h = box.width / aspect;              // the 3D view's field of view spans its full width: so does the picture
  Object.assign(camViewImg.style, { width: `${box.width}px`, height: `${h}px`, top: `${(box.height - h) / 2}px` });
  if (cv.ctl.pose.aspect !== aspect) cv.ctl.setPose({ aspect });
}
function refreshPicture() {
  if (!cv) return;
  const url = `/media/sites/${cv.siteId}/cameras/${encodeURIComponent(cv.deviceId)}/live.jpg?t=${Date.now()}`;
  const img = new Image();
  img.onload = () => { if (cv) { camViewImg.src = img.src; fitPicture(); } };
  img.src = url;
}
function openCamView(siteId, deviceId, calibrate) {
  const s = sites.get(siteId), p = s?.camera_places?.[deviceId];
  if (!globe || !p) return;
  closeCamView();
  const ctl = globe.viewFromCamera({ siteId, deviceId, ...p });
  cv = { siteId, deviceId, ctl, start: ctl.pose, calibrating: false, dirty: false, saved: "" };
  $id("cam-view-name").textContent = p.name || "Camera";
  $id("cam-view-cal").hidden = !CFG.isAdmin;
  camViewImg.style.opacity = String($id("cam-view-opacity").value / 100);
  camViewEl.hidden = false;
  camPanel.hidden = true; globeBar.hidden = true; hud.hidden = true;      // just the camera's view
  refreshPicture();
  cv.timer = setInterval(() => { if (!document.hidden) refreshPicture(); }, 1500);
  if (calibrate && CFG.isAdmin) startCalibrate();
}
function closeCamView() {
  if (!cv) return;
  if (cv.dirty && !confirm("Leave without saving the calibration?")) return false;
  clearInterval(cv.timer);
  cv.ctl.exit();
  cv = null;
  camViewEl.hidden = true; camCal.hidden = true; camViewDrag.hidden = true;
  camViewImg.removeAttribute("src");
  if (globe) { globeBar.hidden = false; hud.hidden = false; }
  if (camOpen) camPanel.hidden = false;
  return true;
}
function startCalibrate() {
  cv.calibrating = true;
  camViewDrag.hidden = false;
  $id("cam-view-cal").hidden = true;
  if (Number($id("cam-view-opacity").value) > 70 || Number($id("cam-view-opacity").value) < 30) { $id("cam-view-opacity").value = 55; camViewImg.style.opacity = "0.55"; }
  paintCal();
}
function paintCal() {
  if (!cv?.calibrating) return;
  const pz = cv.ctl.pose;
  camCal.hidden = false;
  camCal.innerHTML = `<div class="cam-cal-head"><b>Calibrate</b> <span class="muted">Drag the picture to turn / tilt · scroll to zoom · arrow keys for fine steps</span></div>
    <div class="cam-cal-grid">${CAL_FIELDS.map(([k, label, unit, step]) => `<label>${label}
      <span class="cam-cal-num"><button type="button" data-cal-step="${k}" data-by="${-step}" aria-label="Less">−</button>
      <input type="number" data-cal="${k}" step="${step}" value="${(+pz[k]).toFixed(k === "height_m" ? 2 : 2)}">${unit}
      <button type="button" data-cal-step="${k}" data-by="${step}" aria-label="More">+</button></span></label>`).join("")}
      <label>Move the camera <span class="cam-cal-nudge">${[["N", 0, 0.25], ["S", 0, -0.25], ["W", -0.25, 0], ["E", 0.25, 0]].map(([d, e, n]) =>
        `<button type="button" data-cal-move="${e},${n}" title="Move 25 cm ${d}">${d}</button>`).join("")}</span></label></div>
    <div class="cam-cal-foot"><span class="muted" id="cam-cal-say">${esc(cv.saved || (cv.dirty ? "Not saved yet" : "Line up edges, corners and posts in both."))}</span>
      <button type="button" class="btn btn-sm" data-cal-reset>Reset</button>
      <button type="button" class="btn btn-sm btn-primary" data-cal-save>Save</button></div>`;
}
const clampDeg = (k, v) => (k === "heading" ? ((v % 360) + 360) % 360 : k === "tilt" ? Math.max(-90, Math.min(90, v))
  : k === "roll" ? Math.max(-45, Math.min(45, v)) : k === "fov" ? Math.max(5, Math.min(170, v)) : Math.max(0, Math.min(300, v)));
function nudge(values) {
  if (!cv?.calibrating) return;
  const pz = cv.ctl.pose, next = {};
  for (const [k, v] of Object.entries(values)) next[k] = k === "lat" || k === "lng" ? v : clampDeg(k, v);
  cv.ctl.setPose(next);
  cv.dirty = true; cv.saved = "";
  for (const [k] of CAL_FIELDS) {
    const el = camCal.querySelector(`[data-cal="${k}"]`);
    if (el && document.activeElement !== el) el.value = (+cv.ctl.pose[k]).toFixed(2);
  }
  const say = $id("cam-cal-say"); if (say) say.textContent = "Not saved yet";
  void pz;
}
async function saveCalibration() {
  const pz = cv.ctl.pose, s = sites.get(cv.siteId), p = s?.camera_places?.[cv.deviceId] || {};
  const body = { lat: pz.lat, lng: pz.lng, heading: pz.heading, fov: Math.round(pz.fov * 100) / 100, range_m: p.range_m || pz.range,
                 height_m: Math.round(pz.height_m * 100) / 100, tilt: Math.round(pz.tilt * 100) / 100, roll: Math.round(pz.roll * 100) / 100 };
  try {
    await saveSite(await api(`/api/sites/${cv.siteId}/cameras/${encodeURIComponent(cv.deviceId)}`, { method: "PUT", body }));
    cv.dirty = false; cv.saved = "Saved ✓"; cv.start = cv.ctl.pose;
    paintCal();
    toast("Calibration saved");
  } catch (err) { toast(esc(err.message), { kind: "error" }); }
}
$id("cam-view-exit").addEventListener("click", () => closeCamView());
$id("cam-view-cal").addEventListener("click", () => cv && startCalibrate());
$id("cam-view-opacity").addEventListener("input", (e) => { camViewImg.style.opacity = String(e.target.value / 100); });
camCal.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b || !cv) return;
  if (b.dataset.calStep) { const k = b.dataset.calStep; nudge({ [k]: cv.ctl.pose[k] + Number(b.dataset.by) }); return; }
  if (b.dataset.calMove) {
    const [de, dn] = b.dataset.calMove.split(",").map(Number), pz = cv.ctl.pose;
    nudge({ lat: pz.lat + dn / 111320, lng: pz.lng + de / (111320 * Math.cos(pz.lat * Math.PI / 180)) });
    return;
  }
  if (b.matches("[data-cal-reset]")) { cv.ctl.setPose(cv.start); cv.dirty = false; paintCal(); return; }
  if (b.matches("[data-cal-save]")) saveCalibration();
});
camCal.addEventListener("change", (e) => {
  const k = e.target.dataset.cal, v = Number(e.target.value);
  if (k && Number.isFinite(v)) nudge({ [k]: v });
});
// Drag the picture: left/right turns, up/down tilts (the scene follows the picture). Scroll zooms.
let calDrag = null;
camViewDrag.addEventListener("pointerdown", (e) => { if (!cv) return; calDrag = { x: e.clientX, y: e.clientY, pose: cv.ctl.pose }; camViewDrag.setPointerCapture(e.pointerId); });
camViewDrag.addEventListener("pointermove", (e) => {
  if (!calDrag || !cv) return;
  const perPx = calDrag.pose.fov / camViewEl.getBoundingClientRect().width;      // degrees per screen pixel
  nudge({ heading: calDrag.pose.heading - (e.clientX - calDrag.x) * perPx, tilt: calDrag.pose.tilt - (e.clientY - calDrag.y) * perPx });
});
camViewDrag.addEventListener("pointerup", () => { calDrag = null; });
camViewDrag.addEventListener("wheel", (e) => { if (!cv) return; e.preventDefault(); nudge({ fov: cv.ctl.pose.fov * (e.deltaY > 0 ? 1.02 : 1 / 1.02) }); }, { passive: false });
document.addEventListener("keydown", (e) => {
  if (!cv) return;
  if (e.key === "Escape") { if (!document.querySelector("dialog[open]")) closeCamView(); return; }   // Esc on an alarm pop-up only closes it
  if (!cv.calibrating || e.target.matches("input, textarea")) return;
  const fine = e.shiftKey ? 0.05 : 0.25, pz = cv.ctl.pose;
  const k = { ArrowLeft: ["heading", -fine], ArrowRight: ["heading", fine], ArrowUp: ["tilt", -fine], ArrowDown: ["tilt", fine],
              "+": ["fov", -fine * 2], "=": ["fov", -fine * 2], "-": ["fov", fine * 2] }[e.key];
  if (k) { e.preventDefault(); nudge({ [k[0]]: pz[k[0]] + k[1] }); }
});
window.addEventListener("resize", fitPicture);

// ---- place and edit cameras (admins). The editor in globe.js / plan.js does the mouse work; this panel
// lists the site's cameras and shows/edits the numbers. Every finished change is saved at once.
let placingSite = null, editing = null;      // editing = { dev, name }
const MODE_HINT = {
  spot: (n) => `Click where ${n} is.`,
  aim: (n) => `Move the mouse to aim ${n}: the cone follows (direction and distance). Click to set it.`,
  edit: () => "Drag the white dot to move, the orange tip to aim and set distance, the yellow side dots to widen or narrow.",
};
const camRow = (c) => `<li data-dev="${esc(c.device_id)}" data-name="${esc(c.name)}">
  <span class="nm" title="${esc(c.name)}">${esc(c.name)}${c.place ? ' <span class="placed">✓ placed</span>' : ""}</span>
  <button type="button" class="go" data-edit>${c.place ? "Edit" : "Place"}</button>
  ${c.place ? '<button type="button" data-unplace title="Take it off the map">Remove</button>' : ""}</li>`;

async function openPlacing() {
  hideHover();
  const s = sites.get(selectedId);
  if (!s) { toast("Select a site first.", { kind: "warn" }); return; }
  placingSite = s.id;
  camsBtn.classList.add("active");
  camPlace.hidden = false;
  if (!camPlace.querySelector("ul")) camPlace.innerHTML = `<div class="hint">Loading ${esc(s.name)}'s cameras…</div>`;
  try {
    const cams = await api(`/api/sites/${s.id}/cameras`);
    if (placingSite !== s.id) return;
    camPlace.innerHTML = `<div class="cam-editor" id="cam-editor" hidden></div>
      <div class="hint">Pick a camera to place it, or click a camera on the map to edit it.</div>
      <ul>${cams.map(camRow).join("") || '<li class="nm">No cameras found on this site.</li>'}</ul>`;
    if (editing) renderEditor();
  } catch (err) {
    camPlace.innerHTML = `<div class="hint">Couldn't list cameras: ${esc(err.message)}</div>`;
  }
}
function closePlacing() {
  stopCamEdit();
  placingSite = null; camsBtn.classList.remove("active");
  camPlace.hidden = true; camPlace.replaceChildren();
}

function renderEditor(st = view()?.editing && editing ? editing.st : null, saveState = editing?.saved || "") {
  const box = $id("cam-editor");
  if (!box || !editing) return;
  box.hidden = false;
  const mode = view()?.editing?.mode || "spot";
  box.innerHTML = `<div class="ed-name"><b>${esc(editing.name)}</b><button type="button" data-ed-done>Done</button></div>
    <div class="ed-hint">${esc(MODE_HINT[mode](editing.name))}</div>
    ${st && mode !== "spot" ? `<div class="ed-nums">
      <label>Aim <input type="number" data-ed="heading" min="0" max="359" step="1" value="${Math.round(st.heading) % 360}">°</label>
      <label>Width <input type="number" data-ed="fov" min="5" max="360" step="1" value="${Math.round(st.fov)}">°</label>
      <label>Distance <input type="number" data-ed="range_m" min="2" max="500" step="1" value="${Math.round(st.range_m)}"> m</label>
      <button type="button" data-ed-dome>${st.fov >= 360 ? "Cone" : "360° dome"}</button>
    </div>` : ""}
    <div class="ed-save" id="ed-save">${esc(saveState)}</div>`;
}
// While dragging only the numbers change (rebuilding the inputs would steal focus).
function paintNumbers(st) {
  const box = $id("cam-editor");
  if (!box) return;
  for (const k of ["heading", "fov", "range_m"]) {
    const el = box.querySelector(`[data-ed="${k}"]`);
    if (el && document.activeElement !== el) el.value = Math.round(k === "heading" ? st[k] % 360 : st[k]);
  }
  const dome = box.querySelector("[data-ed-dome]");
  if (dome) dome.textContent = st.fov >= 360 ? "Cone" : "360° dome";
}

async function saveCam(siteId, dev, place) {
  // Kept on `editing` too: the list refresh after a first placement redraws the editor.
  const say = (t) => { if (editing?.dev === dev) editing.saved = t; const el = $id("ed-save"); if (el) el.textContent = t; };
  say("Saving…");
  try {
    await saveSite(await api(`/api/sites/${siteId}/cameras/${encodeURIComponent(dev)}`, { method: "PUT", body: place }));
    say("Saved ✓");
    const li = camPlace.querySelector(`li[data-dev="${CSS.escape(dev)}"]`);
    if (li && !li.querySelector(".placed")) openPlacing();          // first placement: refresh the list
  } catch (err) { say(`Not saved: ${err.message}`); toast(esc(err.message), { kind: "error" }); }
}

function startCamEdit(dev, name) {
  const site = sites.get(placingSite);
  if (!site || !view()) return;
  hideHover();
  stopCamEdit();
  const place = site.camera_places?.[dev];
  editing = { dev, name, st: place ? { ...place } : null };
  view().editCamera({ siteId: site.id, deviceId: dev, name, ...(place || {}) }, {
    onMode: (mode, st) => { editing.st = st; renderEditor(st); },
    onChange: (st) => { editing.st = st; paintNumbers(st); },
    onCommit: (place) => saveCam(site.id, dev, place),
  });
  renderEditor(editing.st);
  camPlace.querySelectorAll("li[data-dev]").forEach((li) => li.classList.toggle("editing", li.dataset.dev === dev));
}
function stopCamEdit() {
  view()?.stopEditing();
  editing = null;
  const box = $id("cam-editor");
  if (box) { box.hidden = true; box.replaceChildren(); }
  camPlace.querySelectorAll("li.editing").forEach((li) => li.classList.remove("editing"));
}
async function saveSite(data) { sites.set(data.id, { ...sites.get(data.id), ...data }); renderAll(); }

camPlace.addEventListener("click", async (e) => {
  if (!view() || placingSite == null) return;
  if (e.target.closest("[data-ed-done]")) return stopCamEdit();
  if (e.target.closest("[data-ed-dome]") && editing?.st) return editorSet({ fov: editing.st.fov >= 360 ? 90 : 360 });
  const li = e.target.closest("li[data-dev]");
  if (!li) return;
  const dev = li.dataset.dev, name = li.dataset.name;
  if (e.target.closest("[data-unplace]")) {
    if (editing?.dev === dev) stopCamEdit();
    await saveSite(await api(`/api/sites/${placingSite}/cameras/${encodeURIComponent(dev)}`, { method: "DELETE" }));
    return openPlacing();
  }
  if (e.target.closest("[data-edit]")) startCamEdit(dev, name);
});
// Typing a number applies it (and saves) when the field is left or Enter is pressed.
const editorSet = (values) => { if (editing) view()?.currentEditor()?.set(values); };
camPlace.addEventListener("change", (e) => {
  const k = e.target.dataset?.ed;
  if (!k) return;
  const v = Number(e.target.value);
  if (!Number.isFinite(v)) return;
  editorSet({ [k]: k === "heading" ? ((v % 360) + 360) % 360 : v });
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && editing && view()?.editing?.mode !== "edit") stopCamEdit();
});

globeBar.addEventListener("click", (e) => {
  const t = e.target;
  if (t.id === "globe-close") { if (plan) { closePlanView(); fitToSites(); } else closeGlobeView(); return; }
  if (t.id === "globe-recenter") return view()?.flyTo(sites.get(selectedId));
  if (t.id === "plan-cones") return toggleCones();
  if (t.id === "globe-cams") { models.hide(); return placingSite == null ? openPlacing() : closePlacing(); }
  if (t.id === "globe-model") { closePlacing(); return models.toggle(); }
  if (t.id === "globe-model-show") return toggleModels();
  const kind = t.dataset.imagery;
  if (kind && view()) { view().setImagery(kind); showImagery(kind); }
});

// An alarm opens its details, with the map behind on that site's top-down view at the alarm's camera.
function focusAlarm(a) {
  const s = sites.get(a.site_id);
  if (s) {
    select(s.id, false);
    if (s.lat != null && !globe) openPlanView(s, a.device_id);
    else if (globe) globe.flyTo(s);
  }
  openDrawer(a.id, a);
}

// ---------------------------------------------------------------- interactions
const siteList = document.getElementById("site-list");
siteList.addEventListener("click", (e) => { const li = e.target.closest(".site-item"); if (li) { closeDrawer(); select(Number(li.dataset.id)); } });
siteList.addEventListener("keydown", (e) => {
  const li = e.target.closest(".site-item");
  if (li && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); closeDrawer(); select(Number(li.dataset.id)); }
});
document.getElementById("site-detail").addEventListener("click", async (e) => {
  if (e.target.closest("#close-detail")) { closeDrawer(); deselect(); return; }
  const s = sites.get(selectedId);
  if (e.target.closest("[data-view-map]") && s) { closeDrawer(); showMapView(s); return; }
  if (e.target.closest("[data-globe]") && s) { closeDrawer(); openGlobeView(s); return; }
  if (e.target.closest("[data-plan]") && s) { closeDrawer(); openPlanView(s); return; }
  const op = e.target.dataset.arm;
  if (op && sites.has(selectedId)) { e.target.disabled = true; await setArmed(sites.get(selectedId), op === "arm"); e.target.disabled = false; }
});
document.getElementById("feed-tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  feedPriority = b.dataset.p; renderFeed();
});
document.getElementById("feed-site-filter").addEventListener("click", (e) => { if (e.target.id === "clear-site") deselect(); });
const feed = document.getElementById("feed-list");
feed.addEventListener("click", async (e) => {
  const ack = e.target.dataset.ack;
  if (ack) { e.stopPropagation(); await openAck(openAlarms.get(Number(ack))); return; }
  const li = e.target.closest(".feed-item");
  if (li) focusAlarm(openAlarms.get(Number(li.dataset.id)));
});
feed.addEventListener("keydown", (e) => {
  const li = e.target.closest(".feed-item");
  if (li && e.key === "Enter") focusAlarm(openAlarms.get(Number(li.dataset.id)));
});

// ---------------------------------------------------------------- active calls (a guard was sent; open until End call)
const calls = new Map();
const callLatest = new Map();        // alarm id -> the guards' status and newest update (call_updates.js), shown on the card
const callFlash = new Set();         // cards to flash once (a new guard update)
let callsSoon = null;
async function loadCalls() {
  try {
    const rows = await api("/api/alarms?state=active&limit=100");
    calls.clear();
    rows.filter(inView).forEach((a) => calls.set(a.id, a));
  } catch { return; }
  renderCalls();
  const missing = [...calls.keys()].filter((id) => !callLatest.has(id));     // first look: what each guard said last
  if (missing.length) {
    await Promise.all(missing.map((id) => guardUpdates(id).then((u) => callLatest.set(id, u)).catch(() => {})));
    renderCalls();
  }
}
on("call.update", ({ alarm, updates }) => { callLatest.set(alarm.id, updates); callFlash.add(alarm.id); reloadCalls(); });
function renderCalls() {
  const box = document.getElementById("calls");
  // Acknowledged calls only: one still waiting for an operator is in the live feed below (no duplicates).
  const rows = [...calls.values()].filter((a) => a.state !== "new").sort((a, b) => a.event_ts_ms - b.event_ts_ms);
  box.hidden = !rows.length;
  document.getElementById("calls-count").textContent = rows.length || "";
  const html = rows.map((a) => `
    <li class="feed-item call-item" data-call="${a.id}" tabindex="0">
      <div class="feed-top"><span class="chip call-chip">Call</span>${a.verdict === "real" ? '<span class="chip p1">Real event</span>' : ""}${timerHtml(a.event_ts_ms)}</div>
      <div class="title">${esc(a.caption)}</div>
      <div class="feed-where">${tenantTag(a)}<b>${esc(a.site_name)}</b></div>
      ${callLatestHtml(callLatest.get(a.id))}
      <div class="feed-actions"><button class="btn btn-sm btn-primary" data-call-open="${a.id}">Open call</button></div>
    </li>`).join("");
  const list = document.getElementById("calls-list");
  if (list.dataset.html !== html) { list.innerHTML = html; list.dataset.html = html; }      // only redraw on a change
  for (const id of callFlash) {
    const li = list.querySelector(`[data-call="${id}"]`);
    callFlash.delete(id);
    if (!li) continue;                                    // not acknowledged yet: it's in the live feed (the toast says it)
    li.classList.remove("call-flash"); void li.offsetWidth; li.classList.add("call-flash");
  }
}
// The guard's status (En route / On scene / Clear) stays on top; their newest other update (a note, opened the
// link, sharing location) under it.
function callLatestHtml(u) {
  if (!u?.last) return "";
  const at = (r) => new Date(r.ts).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
  const line = (r) => `<b>${esc(r.who)}: ${esc(r.what)}</b> <span class="muted">· ${esc(at(r))}</span>${r.detail && r.what === "Guard note" ? `<div class="muted small-print">“${esc(r.detail)}”</div>` : ""}`;
  const top = u.status || u.last;
  return `<div class="call-latest">&#128222; ${line(top)}${u.last !== top ? `<div class="call-latest-more">${line(u.last)}</div>` : ""}</div>`;
}
const reloadCalls = () => { clearTimeout(callsSoon); callsSoon = setTimeout(loadCalls, 300); };
["alarm.timeline", "alarm.updated", "alarm.acked", "reconnect", "store"].forEach((ev) => on(ev, reloadCalls));
document.getElementById("calls-list").addEventListener("click", (e) => {
  const li = e.target.closest("[data-call]");
  if (li) focusAlarm(calls.get(Number(li.dataset.call)));
});
document.getElementById("calls-list").addEventListener("keydown", (e) => {
  const li = e.target.closest("[data-call]");
  if (li && e.key === "Enter") focusAlarm(calls.get(Number(li.dataset.call)));
});
loadCalls();

// ---------------------------------------------------------------- data + live updates
async function loadSites() {
  const rows = await api("/api/sites");
  sites.clear();
  rows.forEach((s) => sites.set(s.id, s));
}

on("store", renderAll);
on("alarm.arrived", (a) => {
  // Draw the eye: flash the new card and the site's pin.
  requestAnimationFrame(() => document.querySelector(`.feed-item[data-id="${a.id}"]`)?.classList.add("fresh"));
});
on("site.status", ({ site_id, status, detail }) => {
  const s = sites.get(site_id);
  if (s) { s.status = status; s.status_detail = detail || ""; if (status === "online") s.last_seen_at = new Date().toISOString(); renderAll(); }
});
on("site.updated", (s) => { sites.set(s.id, { ...sites.get(s.id), ...s }); renderAll(); models.siteChanged(); });
on("site.push", ({ site_id, push }) => { const s = sites.get(site_id); if (s) { s.push = push; renderDetail(); } });
on("site.removed", (s) => { sites.delete(s.id); markers.get(s.id)?.remove(); markers.delete(s.id); renderAll(); });
on("reconnect", () => loadSites().then(renderAll).catch(() => {}));
on("focus-alarm", focusAlarm);
setInterval(() => { if (selectedId) renderDetail(); }, 30000);

await loadSites().catch((e) => { siteList.innerHTML = `<li class="empty">${esc(e.message)}</li>`; });
renderAll();
fitToSites();
const focusId = Number(new URLSearchParams(location.search).get("alarm"));
if (focusId) {
  history.replaceState(null, "", "/");
  const a = openAlarms.get(focusId);
  if (a) focusAlarm(a); else api(`/api/alarms/${focusId}`).then(focusAlarm).catch(() => {});
}
