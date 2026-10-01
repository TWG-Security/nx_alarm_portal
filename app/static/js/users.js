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

async function load() { users = await api("/api/users"); render(); if (groupData.groups.length) renderGroups(); }

async function update(id, body) {
  try { await api(`/api/users/${id}`, { method: "PUT", body }); toast("Saved"); }
  catch (e) { toast(esc(e.message), { kind: "error" }); }
  // ---------------------------------------------------------------- groups & permissions
let groupData = { permissions: {}, groups: [] }, editing = null;
const gForm = document.getElementById("group-form");

function renderGroups() {
  const list = document.getElementById("group-list");
  const name = (id) => { const u = users.find((x) => x.id === id); return u ? u.display_name || u.email : `#${id}`; };
  list.innerHTML = groupData.groups.length ? groupData.groups.map((g) => `
    <div class="group-row">
      <div><b>${esc(g.name)}</b>
        <div class="muted">${g.permissions.length ? g.permissions.map((p) => esc(groupData.permissions[p]?.split(":")[0] || p)).join(", ") : "No permissions"}</div>
        <div class="group-members">${g.member_ids.length ? g.member_ids.map((id) => `<span class="chip">${esc(name(id))}</span>`).join("") : '<span class="muted">No members</span>'}</div></div>
      <button class="btn btn-sm" data-edit-group="${g.id}" type="button">Edit</button>
    </div>`).join("") : '<div class="empty muted">No groups yet.</div>';
}

function openGroup(g) {
  editing = g;
  gForm.hidden = false;
  document.getElementById("g-new").hidden = true;
  document.getElementById("group-form-title").textContent = g ? `Edit “${g.name}”` : "New group";
  document.getElementById("g-name").value = g?.name || "";
  document.getElementById("g-delete").hidden = !g;
  document.getElementById("g-perms").innerHTML = Object.entries(groupData.permissions).map(([key, label]) => `
    <label class="radio"><input type="checkbox" value="${esc(key)}" ${g?.permissions.includes(key) ? "checked" : ""}>
      <span><b>${esc(label.split(":")[0])}</b><small>${esc(label.split(":").slice(1).join(":").trim())}</small></span></label>`).join("");
  document.getElementById("g-members").innerHTML = users.filter((u) => u.role !== "admin").map((u) => `
    <label class="member"><input type="checkbox" value="${u.id}" ${g?.member_ids.includes(u.id) ? "checked" : ""}>
      ${esc(u.display_name || u.email)}${u.is_active ? "" : ' <span class="muted">(disabled)</span>'}</label>`).join("")
    || '<span class="muted">No operators yet.</span>';
  document.getElementById("g-name").focus();
}
function closeGroup() { editing = null; gForm.hidden = true; document.getElementById("g-new").hidden = false; }

async function loadGroups() { groupData = await api("/api/groups"); renderGroups(); }

document.getElementById("g-new").addEventListener("click", () => openGroup(null));
document.getElementById("g-cancel").addEventListener("click", closeGroup);
document.getElementById("group-list").addEventListener("click", (e) => {
  const id = Number(e.target.dataset.editGroup);
  if (id) openGroup(groupData.groups.find((g) => g.id === id));
});
gForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    name: document.getElementById("g-name").value,
    permissions: [...document.querySelectorAll("#g-perms input:checked")].map((c) => c.value),
    member_ids: [...document.querySelectorAll("#g-members input:checked")].map((c) => Number(c.value)),
  };
  try {
    groupData = await api(editing ? `/api/groups/${editing.id}` : "/api/groups", { method: editing ? "PUT" : "POST", body });
    toast("Group saved. Members get the new rights on their next page load"); closeGroup(); renderGroups();
  } catch (err) { toast(esc(err.message), { kind: "error" }); }
});
document.getElementById("g-delete").addEventListener("click", async () => {
  if (!editing || !confirm(`Delete the group “${editing.name}”? Its members lose its permissions.`)) return;
  try { groupData = await api(`/api/groups/${editing.id}`, { method: "DELETE" }); closeGroup(); renderGroups(); toast("Group deleted"); }
  catch (err) { toast(esc(err.message), { kind: "error" }); }
});

await load();
await loadGroups().catch((e) => { document.getElementById("group-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`; });
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
    e.target.reset(); toast("User created"); // ---------------------------------------------------------------- groups & permissions
let groupData = { permissions: {}, groups: [] }, editing = null;
const gForm = document.getElementById("group-form");

function renderGroups() {
  const list = document.getElementById("group-list");
  const name = (id) => { const u = users.find((x) => x.id === id); return u ? u.display_name || u.email : `#${id}`; };
  list.innerHTML = groupData.groups.length ? groupData.groups.map((g) => `
    <div class="group-row">
      <div><b>${esc(g.name)}</b>
        <div class="muted">${g.permissions.length ? g.permissions.map((p) => esc(groupData.permissions[p]?.split(":")[0] || p)).join(", ") : "No permissions"}</div>
        <div class="group-members">${g.member_ids.length ? g.member_ids.map((id) => `<span class="chip">${esc(name(id))}</span>`).join("") : '<span class="muted">No members</span>'}</div></div>
      <button class="btn btn-sm" data-edit-group="${g.id}" type="button">Edit</button>
    </div>`).join("") : '<div class="empty muted">No groups yet.</div>';
}

function openGroup(g) {
  editing = g;
  gForm.hidden = false;
  document.getElementById("g-new").hidden = true;
  document.getElementById("group-form-title").textContent = g ? `Edit “${g.name}”` : "New group";
  document.getElementById("g-name").value = g?.name || "";
  document.getElementById("g-delete").hidden = !g;
  document.getElementById("g-perms").innerHTML = Object.entries(groupData.permissions).map(([key, label]) => `
    <label class="radio"><input type="checkbox" value="${esc(key)}" ${g?.permissions.includes(key) ? "checked" : ""}>
      <span><b>${esc(label.split(":")[0])}</b><small>${esc(label.split(":").slice(1).join(":").trim())}</small></span></label>`).join("");
  document.getElementById("g-members").innerHTML = users.filter((u) => u.role !== "admin").map((u) => `
    <label class="member"><input type="checkbox" value="${u.id}" ${g?.member_ids.includes(u.id) ? "checked" : ""}>
      ${esc(u.display_name || u.email)}${u.is_active ? "" : ' <span class="muted">(disabled)</span>'}</label>`).join("")
    || '<span class="muted">No operators yet.</span>';
  document.getElementById("g-name").focus();
}
function closeGroup() { editing = null; gForm.hidden = true; document.getElementById("g-new").hidden = false; }

async function loadGroups() { groupData = await api("/api/groups"); renderGroups(); }

document.getElementById("g-new").addEventListener("click", () => openGroup(null));
document.getElementById("g-cancel").addEventListener("click", closeGroup);
document.getElementById("group-list").addEventListener("click", (e) => {
  const id = Number(e.target.dataset.editGroup);
  if (id) openGroup(groupData.groups.find((g) => g.id === id));
});
gForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    name: document.getElementById("g-name").value,
    permissions: [...document.querySelectorAll("#g-perms input:checked")].map((c) => c.value),
    member_ids: [...document.querySelectorAll("#g-members input:checked")].map((c) => Number(c.value)),
  };
  try {
    groupData = await api(editing ? `/api/groups/${editing.id}` : "/api/groups", { method: editing ? "PUT" : "POST", body });
    toast("Group saved. Members get the new rights on their next page load"); closeGroup(); renderGroups();
  } catch (err) { toast(esc(err.message), { kind: "error" }); }
});
document.getElementById("g-delete").addEventListener("click", async () => {
  if (!editing || !confirm(`Delete the group “${editing.name}”? Its members lose its permissions.`)) return;
  try { groupData = await api(`/api/groups/${editing.id}`, { method: "DELETE" }); closeGroup(); renderGroups(); toast("Group deleted"); }
  catch (err) { toast(esc(err.message), { kind: "error" }); }
});

await load();
await loadGroups().catch((e) => { document.getElementById("group-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`; });
  } catch (err) { toast(esc(err.message), { kind: "error" }); }
});

// ---------------------------------------------------------------- groups & permissions
let groupData = { permissions: {}, groups: [] }, editing = null;
const gForm = document.getElementById("group-form");

function renderGroups() {
  const list = document.getElementById("group-list");
  const name = (id) => { const u = users.find((x) => x.id === id); return u ? u.display_name || u.email : `#${id}`; };
  list.innerHTML = groupData.groups.length ? groupData.groups.map((g) => `
    <div class="group-row">
      <div><b>${esc(g.name)}</b>
        <div class="muted">${g.permissions.length ? g.permissions.map((p) => esc(groupData.permissions[p]?.split(":")[0] || p)).join(", ") : "No permissions"}</div>
        <div class="group-members">${g.member_ids.length ? g.member_ids.map((id) => `<span class="chip">${esc(name(id))}</span>`).join("") : '<span class="muted">No members</span>'}</div></div>
      <button class="btn btn-sm" data-edit-group="${g.id}" type="button">Edit</button>
    </div>`).join("") : '<div class="empty muted">No groups yet.</div>';
}

function openGroup(g) {
  editing = g;
  gForm.hidden = false;
  document.getElementById("g-new").hidden = true;
  document.getElementById("group-form-title").textContent = g ? `Edit “${g.name}”` : "New group";
  document.getElementById("g-name").value = g?.name || "";
  document.getElementById("g-delete").hidden = !g;
  document.getElementById("g-perms").innerHTML = Object.entries(groupData.permissions).map(([key, label]) => `
    <label class="radio"><input type="checkbox" value="${esc(key)}" ${g?.permissions.includes(key) ? "checked" : ""}>
      <span><b>${esc(label.split(":")[0])}</b><small>${esc(label.split(":").slice(1).join(":").trim())}</small></span></label>`).join("");
  document.getElementById("g-members").innerHTML = users.filter((u) => u.role !== "admin").map((u) => `
    <label class="member"><input type="checkbox" value="${u.id}" ${g?.member_ids.includes(u.id) ? "checked" : ""}>
      ${esc(u.display_name || u.email)}${u.is_active ? "" : ' <span class="muted">(disabled)</span>'}</label>`).join("")
    || '<span class="muted">No operators yet.</span>';
  document.getElementById("g-name").focus();
}
function closeGroup() { editing = null; gForm.hidden = true; document.getElementById("g-new").hidden = false; }

async function loadGroups() { groupData = await api("/api/groups"); renderGroups(); }

document.getElementById("g-new").addEventListener("click", () => openGroup(null));
document.getElementById("g-cancel").addEventListener("click", closeGroup);
document.getElementById("group-list").addEventListener("click", (e) => {
  const id = Number(e.target.dataset.editGroup);
  if (id) openGroup(groupData.groups.find((g) => g.id === id));
});
gForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    name: document.getElementById("g-name").value,
    permissions: [...document.querySelectorAll("#g-perms input:checked")].map((c) => c.value),
    member_ids: [...document.querySelectorAll("#g-members input:checked")].map((c) => Number(c.value)),
  };
  try {
    groupData = await api(editing ? `/api/groups/${editing.id}` : "/api/groups", { method: editing ? "PUT" : "POST", body });
    toast("Group saved. Members get the new rights on their next page load"); closeGroup(); renderGroups();
  } catch (err) { toast(esc(err.message), { kind: "error" }); }
});
document.getElementById("g-delete").addEventListener("click", async () => {
  if (!editing || !confirm(`Delete the group “${editing.name}”? Its members lose its permissions.`)) return;
  try { groupData = await api(`/api/groups/${editing.id}`, { method: "DELETE" }); closeGroup(); renderGroups(); toast("Group deleted"); }
  catch (err) { toast(esc(err.message), { kind: "error" }); }
});

await load();
await loadGroups().catch((e) => { document.getElementById("group-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`; });
