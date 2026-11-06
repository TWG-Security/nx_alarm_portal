// Sites page → Webhooks to NX (company admins; app/routers/webhooks.py): an address that sends a generic event or fires
// a soft trigger on one site's NX. The address (its token) is shown once, when made or rotated.
import { api, esc, relTime, toast } from "./common.js";

const $ = (id) => document.getElementById(id);
let rows = [], sites = [], triggers = [], editing = null;

function sends(h) {
  const c = h.config || {};
  if (h.kind === "trigger") return `<b>${esc(c.trigger_name || "Soft trigger")}</b> <span class="muted">· ${esc(c.state)}</span>`;
  const bits = [["Source", c.source], ["Caption", c.caption], ["Description", c.description]].filter(([, v]) => v);
  return bits.length ? bits.map(([k, v]) => `${k}: <code>${esc(v)}</code>`).join("<br>") : '<span class="muted">what the caller sends</span>';
}
function lastCall(h) {
  const r = (h.recent || [])[0];
  if (!r) return '<span class="muted">never called</span>';
  return `<b style="color:${r.ok ? "var(--ok)" : "var(--red-text)"}">${r.ok ? "Sent" : "Failed"}</b>
    <span class="muted small-print">${esc(relTime(new Date(r.at * 1000).toISOString()))} · ${esc(r.from)}${r.ms != null ? ` · ${r.ms} ms` : ""}</span>
    ${r.detail ? `<div class="small-print text-failed">${esc(r.detail)}</div>` : ""}<div class="muted small-print">${h.uses} call${h.uses === 1 ? "" : "s"}</div>`;
}
function paint() {
  $("wh-rows").innerHTML = rows.map((h) => `<tr data-id="${h.id}">
      <td><b>${esc(h.name)}</b> <span class="chip">${esc(h.kind_label)}</span>${h.button ? ' <span class="chip">Button</span>' : ""}${h.config?.on_arm ? ' <span class="chip">On arm</span>' : ""}${h.config?.on_disarm ? ' <span class="chip">On disarm</span>' : ""}
        <div class="small-print">${esc(h.site_name)}</div><div class="small-print mono muted">${esc(h.url_hint)}</div></td>
      <td class="small-print">${sends(h)}</td>
      <td>${lastCall(h)}</td>
      <td class="actions">
        <button class="btn btn-sm" type="button" data-test>Send test</button>
        <button class="btn btn-sm" type="button" data-edit>Edit</button>
        <button class="btn btn-sm" type="button" data-rotate title="A new address; the old one stops working">New address</button>
        <label class="check" style="margin:0"><input type="checkbox" data-enabled ${h.enabled ? "checked" : ""}> On</label>
        <button class="btn btn-sm btn-danger" type="button" data-remove>Remove</button>
      </td></tr>`).join("") || '<tr><td colspan="4" class="empty">No webhooks yet.</td></tr>';
}
async function load() {
  try { rows = await api("/api/webhooks"); paint(); } catch (err) { $("wh-rows").innerHTML = `<tr><td colspan="4" class="empty">${esc(err.message)}</td></tr>`; }
}

// The address, once: copy it, and how to call it.
function showMade(h) {
  const box = $("wh-made");
  box.hidden = false;
  box.innerHTML = `<div class="alert"><b>${esc(h.name)}</b>: copy this address now; it isn't shown again (New address makes another).
    <div class="wh-url"><input type="text" readonly class="mono" value="${esc(h.url)}"><button type="button" class="btn btn-sm" data-copy>Copy</button></div>
    <details class="small-print"><summary>How to call it</summary>
      <pre class="mono">curl -X POST '${esc(h.url)}' \\
  -H 'Content-Type: application/json' \\
  -d '{"event": "Intrusion", "zone": "Warehouse"}'</pre>
      <p>Or a GET with the fields on the address (<code>?zone=Warehouse</code>) for devices that can only open a link.
      The answer is JSON: <code>{"ok": true, "ms": …}</code>, or HTTP 502 with <code>detail</code> when NX refused it.</p></details></div>`;
  box.querySelector("[data-copy]").onclick = async () => {
    const input = box.querySelector("input");
    try { await navigator.clipboard.writeText(input.value); toast("Copied"); } catch { input.select(); document.execCommand("copy"); }
  };
}

// --------------------------------------------------------------- the form
function showKind() {
  for (const el of document.querySelectorAll("#wh-form [data-for]")) el.hidden = el.dataset.for !== $("wh-kind").value;
}
async function loadSite() {
  const id = Number($("wh-site").value);
  $("wh-cam").innerHTML = '<option value="">None</option>';
  $("wh-trigger").innerHTML = '<option value="">Loading…</option>';
  triggers = [];
  if (!id) return;
  try {
    const cams = await api(`/api/sites/${id}/cameras`);
    $("wh-cam").innerHTML += cams.filter((c) => c.in_nx).map((c) => `<option value="${esc(c.device_id)}">${esc(c.name)}</option>`).join("");
  } catch { /* camera list optional */ }
  try {
    triggers = await api(`/api/sites/${id}/nx-triggers`);
    $("wh-trigger").innerHTML = triggers.length
      ? '<option value="">Pick a soft trigger</option>' + triggers.map((t) => `<option value="${esc(t.id)}">${esc(t.name)}${t.prolonged ? " (prolonged)" : ""}</option>`).join("")
      : '<option value="">This site has no soft triggers in NX</option>';
  } catch (err) { $("wh-trigger").innerHTML = `<option value="">${esc(err.message)}</option>`; }
  pickTrigger();
}
function pickTrigger(keepCam = "") {
  const t = triggers.find((x) => x.id === $("wh-trigger").value);
  $("wh-tcam").innerHTML = t ? t.cameras.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("") : "";
  if (keepCam) $("wh-tcam").value = keepCam;
  if (t && !keepCam) $("wh-tstate").value = t.prolonged ? "started" : "instant";
}
$("wh-kind").addEventListener("change", showKind);
$("wh-site").addEventListener("change", loadSite);
$("wh-trigger").addEventListener("change", () => pickTrigger());

function formBody() {
  const kind = $("wh-kind").value;
  const t = triggers.find((x) => x.id === $("wh-trigger").value);
  const on = { on_arm: $("wh-on-arm").checked, on_disarm: $("wh-on-disarm").checked };
  const config = kind === "generic"
    ? { source: $("wh-source").value, caption: $("wh-caption").value, description: $("wh-description").value, state: $("wh-gstate").value,
        device_ids: $("wh-cam").value ? [$("wh-cam").value] : [], ...on }
    : { trigger_id: $("wh-trigger").value, trigger_name: t?.name || "", device_id: $("wh-tcam").value, state: $("wh-tstate").value, ...on };
  return { site_id: Number($("wh-site").value), name: $("wh-name").value, kind, config, button: $("wh-button").checked };
}
function resetForm() {
  editing = null;
  $("wh-form").reset();
  $("wh-title").textContent = "Add a webhook"; $("wh-submit").textContent = "Add webhook"; $("wh-cancel").hidden = true;
  $("wh-site").disabled = false; $("wh-kind").disabled = false;
  showKind(); loadSite();
}
async function startEdit(h) {
  editing = h;
  $("wh-title").textContent = `Edit ${h.name}`; $("wh-submit").textContent = "Save"; $("wh-cancel").hidden = false;
  $("wh-site").value = String(h.site_id); $("wh-site").disabled = true;
  $("wh-kind").value = h.kind; $("wh-kind").disabled = true;
  $("wh-name").value = h.name; $("wh-button").checked = h.button;
  $("wh-on-arm").checked = !!h.config?.on_arm; $("wh-on-disarm").checked = !!h.config?.on_disarm;
  showKind();
  await loadSite();
  const c = h.config || {};
  if (h.kind === "generic") {
    $("wh-source").value = c.source || ""; $("wh-caption").value = c.caption || ""; $("wh-description").value = c.description || "";
    $("wh-gstate").value = c.state || "instant"; $("wh-cam").value = (c.device_ids || [])[0] || "";
  } else {
    $("wh-trigger").value = c.trigger_id || ""; pickTrigger(c.device_id); $("wh-tstate").value = c.state || "instant";
  }
  $("wh-form").scrollIntoView({ block: "center" });
}
$("wh-cancel").addEventListener("click", resetForm);
$("wh-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = e.submitter; btn.disabled = true; $("wh-error").innerHTML = "";
  try {
    const body = formBody();
    if (editing) {
      await api(`/api/webhooks/${editing.id}`, { method: "PUT", body: { name: body.name, config: body.config, button: body.button } });
      toast(`<b>${esc(body.name)}</b> saved`);
    } else {
      const h = await api("/api/webhooks", { method: "POST", body });
      showMade(h);
    }
    resetForm();
    await load();
  } catch (err) {
    $("wh-error").innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`;
  } finally { btn.disabled = false; }
});

$("wh-rows").addEventListener("click", async (e) => {
  const tr = e.target.closest("tr[data-id]");
  const h = tr && rows.find((x) => x.id === Number(tr.dataset.id));
  if (!h) return;
  const b = e.target.closest("button");
  try {
    if (e.target.closest("[data-test]")) {
      if (!confirm(`Send ${h.name} to ${h.site_name}'s NX now?\n\nNX rules on it will run (sirens, bookmarks, notifications…).`)) return;
      b.disabled = true;
      const r = await api(`/api/webhooks/${h.id}/send`, { method: "POST", body: { fields: {} } });
      toast(`Sent to NX in ${r.ms} ms`);
    } else if (e.target.closest("[data-edit]")) return startEdit(h);
    else if (e.target.closest("[data-rotate]")) {
      if (!confirm(`Make a new address for ${h.name}? The current one stops working at once.`)) return;
      showMade(await api(`/api/webhooks/${h.id}/rotate`, { method: "POST" }));
    } else if (e.target.closest("[data-remove]")) {
      if (!confirm(`Remove the webhook ${h.name}? Its address stops working.`)) return;
      await api(`/api/webhooks/${h.id}`, { method: "DELETE" });
      toast(`Webhook <b>${esc(h.name)}</b> removed`);
    } else return;
  } catch (err) { toast(esc(err.message), { kind: "error" }); }
  finally { if (b) b.disabled = false; }
  await load();
});
$("wh-rows").addEventListener("change", async (e) => {
  const box = e.target.closest("[data-enabled]");
  const tr = box?.closest("tr[data-id]");
  if (!tr) return;
  try { await api(`/api/webhooks/${tr.dataset.id}`, { method: "PUT", body: { enabled: box.checked } }); } catch (err) { toast(esc(err.message)); }
  await load();
});

(async () => {
  try {
    sites = (await api("/api/sites")).filter((s) => !s.archived_at);
    $("wh-site").innerHTML = sites.map((s) => `<option value="${s.id}">${esc(s.name)}${s.tenant_name ? ` · ${esc(s.tenant_name)}` : ""}</option>`).join("");
  } catch { /* the Sites table shows the error */ }
  showKind();
  loadSite();
  load();
  setInterval(() => { if (!document.hidden && !editing) load(); }, 15_000);
})();
