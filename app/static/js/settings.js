import { api, esc, toast, CFG, can } from "./common.js";
import { play } from "./sound.js";

const tbody = document.getElementById("level-rows");
const saveBtn = document.getElementById("save-levels");
const forceAck = document.getElementById("force-ack");
const LABELS = { critical: "Critical", alarm: "Alarm", warning: "Warning", ignore: "Ignore" };
let saved = null;

function render(data) {
  saved = data;
  forceAck.checked = data.force_ack_critical;
  const kinds = { security: "Security", system: "System health" };
  tbody.innerHTML = data.types.map((t) => `<tr>
      <td><b>${esc(t.label)}</b><div class="muted mono">${esc(t.type)}</div></td>
      <td>${kinds[t.category] || t.category}</td>
      <td><select data-type="${t.type}" aria-label="Level for ${esc(t.label)}">
        ${Object.entries(LABELS).map(([v, l]) => `<option value="${v}" ${t.level === v ? "selected" : ""}>${l}${t.default === v ? " (default)" : ""}</option>`).join("")}
      </select></td></tr>`).join("");
  saveBtn.disabled = true;
}

function dirty() {
  const changed = [...tbody.querySelectorAll("select")].some((s) => s.value !== saved.types.find((t) => t.type === s.dataset.type).level);
  saveBtn.disabled = !(changed || forceAck.checked !== saved.force_ack_critical);
}
tbody.addEventListener("change", dirty);
forceAck.addEventListener("change", dirty);

saveBtn.addEventListener("click", async () => {
  saveBtn.disabled = true;
  const levels = Object.fromEntries([...tbody.querySelectorAll("select")].map((s) => [s.dataset.type, s.value]));
  try {
    render(await api("/api/settings/alarm-levels", { method: "PUT", body: { levels, force_ack_critical: forceAck.checked } }));
    toast("Alarm levels saved. Open alarms have been re-leveled");
  } catch (e) { toast(esc(e.message), { kind: "error" }); dirty(); }
});

document.querySelectorAll("[data-test]").forEach((b) => b.addEventListener("click", () => play(b.dataset.test, { force: true })));
render(await api("/api/settings/alarm-levels"));


// ---------------------------------------------------------------- company branding (own company, or TWG managing it)
(() => {
  const sc = CFG.scope || {};
  const tid = sc.tenantId;
  if (!tid || sc.mode === "all" || !(CFG.isAdmin && (tid === CFG.tenantId || can("platform.manage")))) return;
  const card = document.getElementById("branding-card");
  const img = document.getElementById("b-logo"), remove = document.getElementById("b-remove");
  const paint = (b) => {
    img.hidden = !b.has_logo; remove.hidden = !b.has_logo;
    if (b.has_logo) img.src = `/branding/${tid}/logo?t=${Date.now()}`;
  };
  api(`/api/tenants/${tid}/branding`).then((b) => {
    card.hidden = false;
    document.getElementById("b-display").value = b.display_name || b.name;
    paint(b);
  }).catch(() => {});
  document.getElementById("b-save").addEventListener("click", async () => {
    try { await api(`/api/tenants/${tid}`, { method: "PUT", body: { display_name: document.getElementById("b-display").value } }); toast("Saved. Reload to see it in the top bar"); }
    catch (e) { toast(esc(e.message), { kind: "error" }); }
  });
  document.getElementById("b-file").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const fd = new FormData(); fd.append("file", file);
    const res = await fetch(`/api/tenants/${tid}/logo`, { method: "POST", body: fd, credentials: "same-origin",
      headers: { "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content } });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) return toast(esc(data.detail || `HTTP ${res.status}`), { kind: "error" });
    paint({ has_logo: true }); toast("Logo uploaded. Reload to see it in the top bar");
  });
  remove.addEventListener("click", async () => { await api(`/api/tenants/${tid}/logo`, { method: "DELETE" }); paint({ has_logo: false }); });
})();

// ---------------------------------------------------------------- sign-in security (app/routers/account.py)
(async () => {
  const card = document.getElementById("security-card"), box = document.getElementById("sec-2fa");
  let sec;
  try { sec = await api("/api/settings/security"); } catch { return; }
  card.hidden = false;
  document.getElementById("sec-company").textContent = sec.company;
  box.checked = sec.require_2fa;
  if (sec.decided_by === "platform") {
    box.disabled = true;
    document.getElementById("sec-note").textContent = sec.require_2fa
      ? "TWG Security requires two-step sign-in here (Platform settings)." : "TWG Security sets this on the Platform page.";
  }
  box.addEventListener("change", async () => {
    try { await api("/api/settings/security", { method: "PUT", body: { require_2fa: box.checked } });
      toast(box.checked ? "Two-step sign-in is now required. It applies at each person's next sign-in." : "Two-step sign-in is optional again"); }
    catch (err) { box.checked = !box.checked; toast(esc(err.message), { kind: "error" }); }
  });
})();
