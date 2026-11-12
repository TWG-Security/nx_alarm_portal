// Sites page → VPN connections (company admins; app/routers/tunnels.py): WireGuard client files and Tailscale auth
// keys. Both are write-only: the page only sees the endpoint / address / tailnet name and the live state.
import { api, esc, relTime, toast } from "./common.js";

const $ = (id) => document.getElementById(id);
const STATE = {
  connected: ["Connected", "var(--ok)"], connecting: ["Connecting", "var(--warn)"], starting: ["Starting", "var(--warn)"],
  failed: ["Not working", "var(--red-text)"], off: ["Switched off", "var(--text-muted)"],
};
let rows = [];

function what(t) {
  if (t.kind === "wireguard") return `${esc(t.info.endpoint || "")} <span class="muted">· this end ${esc(t.info.address || "")}</span>`;
  return esc(t.info.hostname || "");
}
function paint() {
  $("vpn-rows").innerHTML = rows.map((t) => {
    const [label, color] = STATE[t.status.state] || [t.status.state, "var(--text-muted)"];
    return `<tr data-id="${t.id}">
      <td><b>${esc(t.name)}</b> <span class="chip">${esc(t.kind_label)}</span><div class="small-print mono">${what(t)}</div></td>
      <td><b style="color:${color}">${label}</b>${t.status.since ? ` <span class="muted small-print">${esc(relTime(new Date(t.status.since).toISOString()))}</span>` : ""}
        ${t.status.detail ? `<div class="small-print ${t.status.state === "failed" ? "text-failed" : "muted"}">${esc(t.status.detail)}</div>` : ""}</td>
      <td>${t.sites.length ? t.sites.map((s) => `<a href="/sites/${s.id}/edit">${esc(s.name)}</a>`).join(", ") : '<span class="muted">no site yet</span>'}</td>
      <td class="actions">
        <button class="btn btn-sm" type="button" data-replace>${t.kind === "wireguard" ? "Replace file" : "New key"}</button>
        <label class="check" style="margin:0"><input type="checkbox" data-enabled ${t.enabled ? "checked" : ""}> On</label>
        <button class="btn btn-sm btn-danger" type="button" data-remove ${t.sites.length ? 'disabled title="Sites use it"' : ""}>Remove</button>
      </td></tr>`;
  }).join("") || '<tr><td colspan="4" class="empty">No VPN connections. Sites connect over the internet / Nx Cloud.</td></tr>';
}
async function load() {
  try { rows = await api("/api/tunnels"); paint(); } catch (err) { $("vpn-rows").innerHTML = `<tr><td colspan="4" class="empty">${esc(err.message)}</td></tr>`; }
}

function showKind() {
  for (const el of document.querySelectorAll("#vpn-form [data-for]")) el.hidden = el.dataset.for !== $("vpn-kind").value;
}
$("vpn-kind").addEventListener("change", showKind);
$("vpn-file").addEventListener("change", async () => {
  const f = $("vpn-file").files[0];
  if (f) {
    $("vpn-config").value = await f.text();
    if (!$("vpn-name").value) $("vpn-name").value = f.name.replace(/\.(conf|txt)$/i, "");
  }
});
$("vpn-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = e.submitter; btn.disabled = true; $("vpn-error").innerHTML = "";
  try {
    const kind = $("vpn-kind").value;
    const t = await api("/api/tunnels", { method: "POST", body: { kind, name: $("vpn-name").value,
      config: kind === "wireguard" ? $("vpn-config").value : "", auth_key: kind === "tailscale" ? $("vpn-key").value : "" } });
    toast(`VPN connection <b>${esc(t.name)}</b> added. Pick it in Edit site → Connection.`);
    $("vpn-form").reset(); showKind();
    await load();
  } catch (err) {
    $("vpn-error").innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`;
  } finally { btn.disabled = false; }
});

async function replace(t) {
  let body;
  if (t.kind === "wireguard") {
    const input = Object.assign(document.createElement("input"), { type: "file", accept: ".conf,.txt,text/plain" });
    const file = await new Promise((resolve) => { input.onchange = () => resolve(input.files[0]); input.click(); });
    if (!file) return;
    body = { config: await file.text() };
  } else {
    const key = prompt(`New Tailscale auth key for ${t.name}:`);
    if (!key) return;
    body = { auth_key: key };
  }
  await api(`/api/tunnels/${t.id}`, { method: "PUT", body });
  toast(`<b>${esc(t.name)}</b> updated: reconnecting`);
}
$("vpn-rows").addEventListener("click", async (e) => {
  const tr = e.target.closest("tr[data-id]");
  const t = tr && rows.find((x) => x.id === Number(tr.dataset.id));
  if (!t) return;
  try {
    if (e.target.closest("[data-replace]")) await replace(t);
    else if (e.target.closest("[data-remove]")) {
      if (!confirm(`Remove the VPN connection ${t.name}?${t.kind === "tailscale" ? " The portal also leaves your tailnet." : ""}`)) return;
      await api(`/api/tunnels/${t.id}`, { method: "DELETE" });
      toast(`VPN connection <b>${esc(t.name)}</b> removed`);
    } else return;
    await load();
  } catch (err) { toast(esc(err.message)); }
});
$("vpn-rows").addEventListener("change", async (e) => {
  const box = e.target.closest("[data-enabled]");
  const tr = box?.closest("tr[data-id]");
  if (!tr) return;
  try { await api(`/api/tunnels/${tr.dataset.id}`, { method: "PUT", body: { enabled: box.checked } }); } catch (err) { toast(esc(err.message)); }
  await load();
});

showKind();
load();
setInterval(() => { if (!document.hidden) load(); }, 10_000);
