// TWG's Companies page: every company's health, open it (support mode), create and edit companies.
import { api, esc, relTime, toast, can } from "./common.js";

const tbody = document.getElementById("company-rows");
const MANAGE = can("platform.manage");
let rows = [], editing = null;

const counts = (t) => [t.open_critical && `<span class="chip p1">${t.open_critical} critical</span>`,
  t.open_alarm && `<span class="chip p2">${t.open_alarm} alarm</span>`,
  t.open_warning && `<span class="chip p3">${t.open_warning} warning</span>`].filter(Boolean).join(" ") || '<span class="muted">None</span>';

function render() {
  tbody.innerHTML = rows.map((t) => `<tr>
    <td><div style="display:flex;gap:10px;align-items:center">
        ${t.has_logo ? `<span class="logo-chip logo-mini"><img src="/branding/${t.id}/logo" alt=""></span>` : ""}
        <div><b>${esc(t.label)}</b>${t.own ? ' <span class="chip">yours</span>' : ""}${t.kind === "platform" ? ' <span class="chip">platform</span>' : ""}
          ${t.label !== t.name ? `<div class="muted">${esc(t.name)}</div>` : ""}</div></div></td>
    <td>${t.sites}${t.sites_offline ? ` <span class="chip offline">${t.sites_offline} offline</span>` : ""}${t.sites_disarmed ? ` <span class="chip disarmed">${t.sites_disarmed} disarmed</span>` : ""}</td>
    <td>${counts(t)}</td>
    <td>${t.users}</td>
    <td>${t.last_alarm_ms ? relTime(t.last_alarm_ms) : '<span class="muted">never</span>'}</td>
    <td>${t.is_active ? '<span class="chip online">On</span>' : '<span class="chip offline">Off</span>'}</td>
    <td style="white-space:nowrap"><button class="btn btn-sm btn-primary" data-open="${t.own ? "own" : t.id}">Open</button>
      ${MANAGE ? `<button class="btn btn-sm" data-edit="${t.id}">Edit</button>` : ""}</td>
  </tr>`).join("") || '<tr><td colspan="7" class="empty">No companies.</td></tr>';
}

async function load() { rows = await api("/api/tenants"); render(); }

async function open(scope) {
  await api("/api/scope", { method: "POST", body: { scope: String(scope) } });
  location.href = "/";
}

tbody.addEventListener("click", (e) => {
  if (e.target.dataset.open) open(e.target.dataset.open).catch((err) => toast(esc(err.message), { kind: "error" }));
  if (e.target.dataset.edit) openEdit(rows.find((t) => t.id === Number(e.target.dataset.edit)));
});
document.getElementById("view-all").addEventListener("click", () => open("all"));

// ---------------------------------------------------------------- create
const cDlg = document.getElementById("company-dialog");
document.getElementById("new-company")?.addEventListener("click", () => { document.getElementById("company-form").reset(); document.getElementById("c-error").innerHTML = ""; cDlg.showModal(); });
document.getElementById("c-cancel").addEventListener("click", () => cDlg.close());
document.getElementById("company-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const $ = (id) => document.getElementById(id).value;
  try {
    await api("/api/tenants", { method: "POST", body: { name: $("c-name"), display_name: $("c-display"),
      admin_email: $("c-email"), admin_name: $("c-admin"), admin_password: $("c-pass") } });
    cDlg.close(); toast(`Company <b>${esc($("c-name"))}</b> created. Its admin can sign in now.`); await load();
  } catch (err) { document.getElementById("c-error").innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`; }
});

// ---------------------------------------------------------------- edit
const eDlg = document.getElementById("edit-dialog");
function paintLogo(t) {
  const img = document.getElementById("e-logo");
  img.hidden = !t.has_logo;
  if (t.has_logo) img.src = `/branding/${t.id}/logo?t=${Date.now()}`;
  document.getElementById("e-remove-logo").hidden = !t.has_logo;
}
function openEdit(t) {
  editing = t;
  document.getElementById("e-title").textContent = `Edit ${t.label}`;
  document.getElementById("e-display").value = t.display_name || "";
  document.getElementById("e-active").checked = t.is_active;
  document.getElementById("e-active-row").hidden = t.kind === "platform";
  document.getElementById("e-file").value = "";
  document.getElementById("e-error").innerHTML = "";
  paintLogo(t);
  eDlg.showModal();
}
document.getElementById("e-cancel").addEventListener("click", () => eDlg.close());
document.getElementById("e-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file || !editing) return;
  const fd = new FormData(); fd.append("file", file);
  const res = await fetch(`/api/tenants/${editing.id}/logo`, { method: "POST", body: fd, credentials: "same-origin",
    headers: { "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content } });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { document.getElementById("e-error").innerHTML = `<div class="alert alert-error">${esc(data.detail || `HTTP ${res.status}`)}</div>`; return; }
  editing.has_logo = true; paintLogo(editing); toast("Logo uploaded"); load();
});
document.getElementById("e-remove-logo").addEventListener("click", async () => {
  await api(`/api/tenants/${editing.id}/logo`, { method: "DELETE" });
  editing.has_logo = false; paintLogo(editing); load();
});
document.getElementById("edit-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const active = document.getElementById("e-active").checked;
  if (editing.kind !== "platform" && editing.is_active && !active &&
      !confirm(`Turn off sign-in for ${editing.label}? Everyone there is signed out. Their sites keep being monitored.`)) return;
  try {
    await api(`/api/tenants/${editing.id}`, { method: "PUT", body: { display_name: document.getElementById("e-display").value,
      ...(editing.kind === "platform" ? {} : { is_active: active }) } });
    eDlg.close(); toast("Saved"); await load();
  } catch (err) { document.getElementById("e-error").innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`; }
});

await load().catch((e) => { tbody.innerHTML = `<tr><td colspan="7" class="empty">${esc(e.message)}</td></tr>`; });
