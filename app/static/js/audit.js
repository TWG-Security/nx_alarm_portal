import { api, esc, fmtTime } from "./common.js";

const params = new URLSearchParams(location.search);
const tbody = document.getElementById("audit-rows");
const actionSel = document.getElementById("f-action");
const siteSel = document.getElementById("f-site");
const PAGE = 100;
let rows = [], oldestId = null;
const alarmId = params.get("alarm_id");

const LABELS = {
  "alarm.received": "Alarm received", "alarm.acknowledged": "Alarm acknowledged",
  "site.created": "Site added", "site.updated": "Site edited", "site.enabled": "Site enabled", "site.disabled": "Site disabled",
  "site.archived": "Site archived", "site.armed": "Site armed", "site.disarmed": "Site disarmed", "alarm.raised": "Alarm raised (#24h)", "site.online": "Site online", "site.offline": "Site offline", "site.auth_error": "Site login failed",
  login: "Signed in", logout: "Signed out", "login.failed": "Failed sign-in", "user.created": "User created", "user.updated": "User edited",
};

function details(r) {
  const d = r.detail || {};
  switch (r.action) {
    case "alarm.acknowledged":
      return `Alarm #${r.alarm_id}${d.note ? ` · “${esc(d.note)}”` : ""}<div class="muted">NX: ${esc(d.nx?.method || "—")} ${d.nx?.ok === false ? `(failed: ${esc(d.nx.error)})` : d.nx?.ok ? "(ok)" : ""}</div>`;
    case "alarm.received":
      return `<a href="/alarms?state=all&open=${r.alarm_id}">#${r.alarm_id}</a> ${esc(d.caption || "")} <span class="muted">${esc(d.event_type || "")}</span>${d.site_disarmed ? ' <span class="chip disarmed">Site disarmed</span>' : ""}`;
    case "site.updated": return `Changed: ${esc((d.fields || []).join(", ") || "nothing")}`;
    case "site.armed": case "site.disarmed":
      return esc([d.source === "manual" ? "by hand" : d.source === "timer" ? "disarm timer ran out" : `by ${d.source}`,
                  d.until_ms ? `re-arms ${fmtTime(d.until_ms)}` : "", d.note ? `“${d.note}”` : ""].filter(Boolean).join(" · "))
        + (r.ip ? ` <span class="muted">${esc(r.ip)}</span>` : "");
    case "user.created": case "user.updated": return esc([d.email || d.target, d.role, (d.fields || []).join(", ")].filter(Boolean).join(" · "));
    default: return esc(d.detail_text || d.name || "") + (r.ip ? ` <span class="muted">${esc(r.ip)}</span>` : "");
  }
}

function render() {
  tbody.innerHTML = rows.length ? rows.map((r) => `<tr>
      <td style="white-space:nowrap">${fmtTime(r.ts)}</td>
      <td>${esc(LABELS[r.action] || r.action)}</td>
      <td>${esc(r.user)}</td>
      <td>${esc(r.site_name || "")}</td>
      <td>${details(r)}</td></tr>`).join("")
    : '<tr><td colspan="5" class="empty">No audit entries match.</td></tr>';
}

async function load(append = false) {
  const p = new URLSearchParams({ limit: PAGE });
  if (actionSel.value) p.set("action", actionSel.value);
  if (siteSel.value) p.set("site_id", siteSel.value);
  if (alarmId) p.set("alarm_id", alarmId);
  if (append && oldestId) p.set("before_id", oldestId);
  const data = await api(`/api/audit?${p}`);
  rows = append ? rows.concat(data) : data;
  oldestId = data.length === PAGE ? data[data.length - 1].id : null;
  document.getElementById("load-more").hidden = !oldestId;
  render();
}

actionSel.addEventListener("change", () => load());
siteSel.addEventListener("change", () => load());
document.getElementById("load-more").addEventListener("click", () => load(true));
if (alarmId) document.querySelector(".page-head h1").textContent = `Audit log · alarm #${alarmId}`;
api("/api/sites").then((sites) => {
  siteSel.innerHTML = '<option value="">All sites</option>' + sites.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
}).catch(() => {});
await load().catch((e) => { tbody.innerHTML = `<tr><td colspan="5" class="empty">${esc(e.message)}</td></tr>`; });
