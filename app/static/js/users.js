import { api, esc, relTime, toast } from "./common.js";

const tbody = document.getElementById("user-rows");
let users = [];

function render() {
  tbody.innerHTML = users.map((u) => `<tr>
    <td>${esc(u.email)}</td><td>${esc(u.display_name)}</td>
    <td><select data-role="${u.id}" aria-label="Role"><option value="operator" ${u.role === "operator" ? "selected" : ""}>Operator</option><option value="admin" ${u.role === "admin" ? "selected" : ""}>Admin</option></select></td>
    <td>${u.is_active ? '<span class="chip online">Active</span>' : '<span class="chip">Disabled</span>'}</td>
    <td>${relTime(u.last_login_at)}</td>
    <td style="white-space:nowrap"><button class="btn btn-sm" data-toggle="${u.id}">${u.is_active ? "Disable" : "Enable"}</button>
      <button class="btn btn-sm" data-reset="${u.id}">Reset password</button></td></tr>`).join("");
}

async function load() { users = await api("/api/users"); render(); }

async function update(id, body) {
  try { await api(`/api/users/${id}`, { method: "PUT", body }); toast("Saved"); }
  catch (e) { toast(esc(e.message), { kind: "error" }); }
  await load();
}

tbody.addEventListener("change", (e) => { if (e.target.dataset.role) update(e.target.dataset.role, { role: e.target.value }); });
tbody.addEventListener("click", (e) => {
  const t = e.target.dataset.toggle, r = e.target.dataset.reset;
  if (t) update(t, { is_active: !users.find((u) => u.id === Number(t)).is_active });
  if (r) {
    const pw = prompt("New password (at least 12 characters):");
    if (pw) update(r, { password: pw });
  }
});

document.getElementById("user-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/users", { method: "POST", body: {
      email: document.getElementById("u-email").value, display_name: document.getElementById("u-name").value,
      password: document.getElementById("u-pass").value, role: document.getElementById("u-role").value } });
    e.target.reset(); toast("User created"); await load();
  } catch (err) { toast(esc(err.message), { kind: "error" }); }
});

await load();
