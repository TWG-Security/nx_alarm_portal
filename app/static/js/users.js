// Users page: accounts, their sign-in security (two-step, passkeys, signed-in browsers), groups & permissions.
import { api, esc, fmtTime, relTime, toast } from "./common.js";

const $ = (id) => document.getElementById(id);
const tbody = $("user-rows");
let users = [];
let groupData = { permissions: {}, groups: [] }, editing = null;

// ---------------------------------------------------------------- users
function twoStep(u) {
  const parts = [];
  if (u.totp_enabled) parts.push('<span class="chip online">App</span>');
  if (u.passkeys) parts.push(`<span class="chip online">${u.passkeys} passkey${u.passkeys === 1 ? "" : "s"}</span>`);
  return parts.join(" ") || '<span class="chip">Off</span>';
}

function render() {
  tbody.innerHTML = users.map((u) => `<tr>
    <td>${esc(u.email)}</td><td>${esc(u.display_name)}</td>
    <td><select data-role="${u.id}" aria-label="Role"><option value="operator" ${u.role === "operator" ? "selected" : ""}>Operator</option><option value="admin" ${u.role === "admin" ? "selected" : ""}>Admin</option></select></td>
    <td>${u.is_active ? '<span class="chip online">Active</span>' : '<span class="chip">Disabled</span>'}</td>
    <td>${twoStep(u)}</td>
    <td>${relTime(u.last_login_at)}${u.sessions ? `<div class="muted small-print">${u.sessions} browser${u.sessions === 1 ? "" : "s"} signed in</div>` : ""}</td>
    <td style="white-space:nowrap"><button class="btn btn-sm" data-toggle="${u.id}">${u.is_active ? "Disable" : "Enable"}</button>
      <button class="btn btn-sm" data-reset="${u.id}">Reset password</button>
      <button class="btn btn-sm" data-security="${u.id}">Sign-in security</button></td></tr>`).join("")
    || '<tr><td colspan="7" class="empty">No users.</td></tr>';
}

async function load() {
  users = await api("/api/users");
  render();
  if (groupData.groups.length) renderGroups();
}

async function update(id, body) {
  try { await api(`/api/users/${id}`, { method: "PUT", body }); toast("Saved"); }
  catch (e) { toast(esc(e.message), { kind: "error" }); }
  await load();
}

tbody.addEventListener("change", (e) => { if (e.target.dataset.role) update(e.target.dataset.role, { role: e.target.value }); });
tbody.addEventListener("click", (e) => {
  const d = e.target.dataset;
  if (d.toggle) update(d.toggle, { is_active: !users.find((u) => u.id === Number(d.toggle)).is_active });
  if (d.reset) {
    const pw = prompt("New password. It must be at least 12 characters with upper and lower case, a number and a symbol. Their browsers are signed out.");
    if (pw) update(d.reset, { password: pw });
  }
  if (d.security) openSecurity(users.find((u) => u.id === Number(d.security)));
});

$("user-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/users", { method: "POST", body: {
      email: $("u-email").value, display_name: $("u-name").value, password: $("u-pass").value, role: $("u-role").value } });
    e.target.reset(); toast("User created");
    await load();
  } catch (err) { toast(esc(err.message), { kind: "error" }); }
});

// ---------------------------------------------------------------- one user's sign-in security
const secDlg = $("sec-dialog");
let secUser = null;
$("sec-close").addEventListener("click", () => secDlg.close());

async function openSecurity(u) {
  secUser = u;
  $("sec-title").textContent = `Sign-in security: ${u.display_name || u.email}`;
  $("sec-body").innerHTML = '<span class="muted">Loading…</span>';
  if (!secDlg.open) secDlg.showModal();
  const s = await api(`/api/users/${u.id}/security`);
  $("sec-body").innerHTML = `
    <p>${s.mfa_required ? "Two-step sign-in is <b>required</b> in this company. " : ""}Authenticator app: <b>${s.totp_enabled ? `on (${s.recovery_left} recovery codes left)` : "off"}</b>.
      Passkeys: <b>${s.passkeys.length ? s.passkeys.map((k) => esc(k.name)).join(", ") : "none"}</b>.</p>
    ${s.totp_enabled || s.passkeys.length ? `<div class="card" style="margin-bottom:12px"><b>Lost their phone?</b>
      <p class="muted small-print" style="margin:4px 0 8px">Resetting turns off their authenticator app and recovery codes. ${s.mfa_required ? "They'll set it up again at their next sign-in." : ""}</p>
      ${s.passkeys.length ? `<label class="check"><input type="checkbox" id="sec-pk"> Also remove their ${s.passkeys.length} passkey${s.passkeys.length === 1 ? "" : "s"}</label>` : ""}
      <button class="btn btn-sm btn-danger" type="button" id="sec-reset">Reset two-step sign-in</button></div>` : ""}
    <h3>Signed in</h3>
    <div class="table-wrap"><table><thead><tr><th>Where</th><th>Address</th><th>Last active</th><th></th></tr></thead><tbody>
      ${s.sessions.map((x) => `<tr><td>${esc(x.user_agent.slice(0, 60) || "Unknown browser")}${x.current ? ' <span class="chip">you</span>' : ""}
          <div class="muted small-print">since ${esc(fmtTime(x.created_at))}</div></td>
        <td class="mono">${esc(x.ip)}</td><td>${esc(relTime(x.last_seen_at))}</td>
        <td>${x.current ? "" : `<button class="btn btn-sm" data-end="${esc(x.id)}">Sign out</button>`}</td></tr>`).join("")
        || '<tr><td colspan="4" class="empty">Not signed in anywhere.</td></tr>'}
    </tbody></table></div>
    ${s.sessions.length ? `<button class="btn btn-sm" type="button" id="sec-all" style="margin-top:10px">Sign them out everywhere</button>
      <p class="muted small-print">A signed-out screen shows a red "SIGNED OUT" banner and stops receiving alarms until someone signs in.</p>` : ""}`;
}

$("sec-body").addEventListener("click", async (e) => {
  try {
    if (e.target.id === "sec-reset") {
      if (!confirm(`Reset two-step sign-in for ${secUser.email}?`)) return;
      await api(`/api/users/${secUser.id}/reset-2fa`, { method: "POST", body: { passkeys: Boolean($("sec-pk")?.checked) } });
      toast("Two-step sign-in reset");
    } else if (e.target.id === "sec-all") {
      if (!confirm(`Sign ${secUser.email} out of every browser?`)) return;
      const r = await api(`/api/users/${secUser.id}/sessions`, { method: "DELETE" });
      toast(`Signed out of ${r.ended} browser${r.ended === 1 ? "" : "s"}`);
    } else if (e.target.dataset.end) {
      await api(`/api/users/${secUser.id}/sessions/${encodeURIComponent(e.target.dataset.end)}`, { method: "DELETE" });
      toast("Signed out there");
    } else return;
    await openSecurity(secUser);
    await load();
  } catch (err) { toast(esc(err.message), { kind: "error" }); }
});

// ---------------------------------------------------------------- groups & permissions
const gForm = $("group-form");

function renderGroups() {
  const list = $("group-list");
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
  $("g-new").hidden = true;
  $("group-form-title").textContent = g ? `Edit “${g.name}”` : "New group";
  $("g-name").value = g?.name || "";
  $("g-delete").hidden = !g;
  $("g-perms").innerHTML = Object.entries(groupData.permissions).map(([key, label]) => `
    <label class="radio"><input type="checkbox" value="${esc(key)}" ${g?.permissions.includes(key) ? "checked" : ""}>
      <span><b>${esc(label.split(":")[0])}</b><small>${esc(label.split(":").slice(1).join(":").trim())}</small></span></label>`).join("");
  $("g-members").innerHTML = users.filter((u) => u.role !== "admin").map((u) => `
    <label class="member"><input type="checkbox" value="${u.id}" ${g?.member_ids.includes(u.id) ? "checked" : ""}>
      ${esc(u.display_name || u.email)}${u.is_active ? "" : ' <span class="muted">(disabled)</span>'}</label>`).join("")
    || '<span class="muted">No operators yet.</span>';
  $("g-name").focus();
}
function closeGroup() { editing = null; gForm.hidden = true; $("g-new").hidden = false; }

async function loadGroups() { groupData = await api("/api/groups"); renderGroups(); }

$("g-new").addEventListener("click", () => openGroup(null));
$("g-cancel").addEventListener("click", closeGroup);
$("group-list").addEventListener("click", (e) => {
  const id = Number(e.target.dataset.editGroup);
  if (id) openGroup(groupData.groups.find((g) => g.id === id));
});
gForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    name: $("g-name").value,
    permissions: [...document.querySelectorAll("#g-perms input:checked")].map((c) => c.value),
    member_ids: [...document.querySelectorAll("#g-members input:checked")].map((c) => Number(c.value)),
  };
  try {
    groupData = await api(editing ? `/api/groups/${editing.id}` : "/api/groups", { method: editing ? "PUT" : "POST", body });
    toast("Group saved. Members get the new rights on their next page load"); closeGroup(); renderGroups();
  } catch (err) { toast(esc(err.message), { kind: "error" }); }
});
$("g-delete").addEventListener("click", async () => {
  if (!editing || !confirm(`Delete the group “${editing.name}”? Its members lose its permissions.`)) return;
  try { groupData = await api(`/api/groups/${editing.id}`, { method: "DELETE" }); closeGroup(); renderGroups(); toast("Group deleted"); }
  catch (err) { toast(esc(err.message), { kind: "error" }); }
});

await load();
await loadGroups().then(() => { $("g-new").disabled = false; })      // the form needs the users and permissions first
  .catch((e) => { $("group-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`; });
