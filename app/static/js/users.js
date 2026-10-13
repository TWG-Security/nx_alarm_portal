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

const MAILED = { sent: "email sent", failed: "email failed", disabled: "email off" };
function inviteNote(u) {
  const m = u.invite_email;
  return m ? `<div class="muted small-print" title="${esc(m.error || "")}">${esc(MAILED[m.outcome] || m.outcome)} ${esc(relTime(m.ts))}</div>` : "";
}
const statusChip = (u) => (!u.is_active ? '<span class="chip">Disabled</span>'
  : u.invited ? '<span class="chip disarmed">Invited</span>' : '<span class="chip online">Active</span>');

// The setup link, shown once so the admin can pass it on (also when email is off or failed).
export function linkBox(r) {
  const note = r.email === "sending" ? "An invite email is on its way." : "Email is off, so nothing was sent: pass this link on yourself.";
  return `<div class="alert alert-ok"><b>Setup link</b> (valid until ${esc(fmtTime(r.expires_at))}). ${note}
    <div class="row" style="margin-top:8px;align-items:center"><input type="text" readonly value="${esc(r.setup_link)}" class="mono" onfocus="this.select()">
    <button class="btn btn-sm" type="button" data-copy="${esc(r.setup_link)}" style="flex:0 0 auto">Copy</button></div></div>`;
}
document.addEventListener("click", (e) => {
  if (e.target.dataset.copy) navigator.clipboard.writeText(e.target.dataset.copy).then(() => toast("Link copied"));
});

// ---------------------------------------------------------------- alarm texts
const fmtPhone = (p) => (/^\+1\d{10}$/.test(p || "") ? `(${p.slice(2, 5)}) ${p.slice(5, 8)}-${p.slice(8)}` : p || "");
const LEVEL = { off: "No texts", critical: "Critical only", alarm: "Critical + Alarm" };
const WHEN = { raised: "", confirmed: "Only when confirmed", both: "+ when confirmed" };
const TEXTED = { sent: ["text sent", ""], delivered: ["text delivered", ""], failed: ["text FAILED", "text-failed"],
  undelivered: ["text NOT DELIVERED", "text-failed"], disabled: ["texts are off", ""] };
const CONSENT = {
  none: ["Not asked yet", ""], pending: ["Waiting for YES", "disarmed"], yes: ["Opted in", "online"], stopped: ["Replied STOP", "offline"],
};
function consentNote(u) {
  return u.sms_consent === "pending" ? `Opt-in request sent ${relTime(u.sms_optin_sent_at)}. Alarm texts start when they reply YES.`
    : u.sms_consent === "yes" ? `Replied YES ${relTime(u.sms_consent_at)}.` : u.sms_consent === "stopped"
    ? "They replied STOP. They can text START or YES to the portal's number to sign up again."
    : "No texts until they reply YES to an opt-in request.";
}
function lastText(u) {
  const t = u.last_text;
  if (!t) return "";
  const [word, cls] = TEXTED[t.outcome] || [t.outcome, ""];
  return `<span class="muted small-print ${cls}" title="${esc(t.error || "")}">${t.purpose === "test" ? "test " : t.purpose === "optin" ? "opt-in " : ""}${esc(word)} ${esc(relTime(t.ts))}${cls && t.error ? `: ${esc(t.error.slice(0, 90))}` : ""}</span>`;
}
// ---------------------------------------------------------------- which sites text them
let sites = [];
const siteName = (id) => sites.find((x) => x.id === id)?.name || `Site ${id}`;
const sitesLabel = (u) => (u.sms_sites == null ? "All sites" : u.sms_sites.length === 1 ? siteName(u.sms_sites[0]) : `${u.sms_sites.length} sites`);
// prefix "u" (Add) or "ed" (Edit): radio All / Only these, with a checkbox per site.
function paintSites(prefix, chosen) {
  const some = chosen != null;
  document.querySelector(`input[name="${prefix}-scope"][value="${some ? "some" : "all"}"]`).checked = true;
  $(`${prefix}-sites`).hidden = !some;
  $(`${prefix}-sites`).innerHTML = sites.map((x) => `<label class="member"><input type="checkbox" value="${x.id}" ${some && chosen.includes(x.id) ? "checked" : ""}>
    ${esc(x.name)}</label>`).join("") || '<span class="muted">No sites yet.</span>';
}
function readSites(prefix) {
  if (document.querySelector(`input[name="${prefix}-scope"]:checked`).value === "all") return null;
  return [...$(`${prefix}-sites`).querySelectorAll("input:checked")].map((c) => Number(c.value));
}
document.querySelectorAll(".sites-pick").forEach((box) => box.addEventListener("change", (e) => {
  if (e.target.type === "radio") $(`${box.dataset.prefix}-sites`).hidden = e.target.value !== "some";
}));
const sameSites = (a, b) => (a == null || b == null ? a == b : a.length === b.length && a.every((x, i) => x === b[i]));

// Table cell: number, then what they get and whether they've opted in. Editing happens in the Edit dialog.
function textSummary(u) {
  if (!u.phone) return '<span class="muted">No number</span>';
  const [cWord, cCls] = CONSENT[u.sms_consent] || CONSENT.none;
  return `<div class="nowrap">${esc(fmtPhone(u.phone))}</div>
    <div class="chips"><span class="chip">${u.sms_level !== "off" && u.sms_when === "confirmed" ? "Any level" : LEVEL[u.sms_level] || u.sms_level}</span>
      ${u.sms_level === "off" || !WHEN[u.sms_when] ? "" : `<span class="chip" title="Texted when an operator marks an alarm Real event">${WHEN[u.sms_when]}</span>`}
      ${u.sms_level === "off" ? "" : `<span class="chip" title="${esc(u.sms_sites == null ? "Every site, including ones added later" : u.sms_sites.map(siteName).join(", "))}">${esc(sitesLabel(u))}</span>`}
      <span class="chip ${cCls}" title="${esc(consentNote(u))}">${cWord}</span></div>
    ${["failed", "undelivered"].includes(u.last_text?.outcome) ? lastText(u) : ""}`;
}

function render() {
  tbody.innerHTML = users.map((u) => `<tr>
    <td><b>${esc(u.display_name || u.email)}</b>${u.display_name ? `<div class="muted small-print">${esc(u.email)}</div>` : ""}</td>
    <td>${u.role === "admin" ? "Admin" : "Operator"}</td>
    <td>${statusChip(u)}${u.invited ? inviteNote(u) : ""}</td>
    <td>${textSummary(u)}</td>
    <td>${twoStep(u)}</td>
    <td class="nowrap">${relTime(u.last_login_at)}${u.sessions ? `<div class="muted small-print">${u.sessions} browser${u.sessions === 1 ? "" : "s"}</div>` : ""}</td>
    <td class="actions"><button class="btn btn-sm" type="button" data-edit="${u.id}">Edit</button></td></tr>`).join("")
    || '<tr><td colspan="7" class="empty">No users.</td></tr>';
}

async function load() {
  [users, sites] = await Promise.all([api("/api/users"), sites.length ? sites : api("/api/sites").catch(() => [])]);
  render();
  if (groupData.groups.length) renderGroups();
  if (edDlg.open && edUser) { edUser = users.find((u) => u.id === edUser.id) || edUser; paintEditExtras(); }
}

tbody.addEventListener("click", (e) => {
  const id = Number(e.target.dataset.edit);
  if (id) openEdit(users.find((u) => u.id === id));
});

// ---------------------------------------------------------------- Edit user (dialog)
const edDlg = $("edit-dialog");
let edUser = null;
$("ed-cancel").addEventListener("click", () => edDlg.close());

function openEdit(u) {
  edUser = u;
  $("ed-title").textContent = u.display_name || u.email;
  $("ed-email").textContent = u.email;
  $("ed-name").value = u.display_name; $("ed-role").value = u.role;
  $("ed-phone").value = fmtPhone(u.phone); $("ed-sms").value = u.sms_level; $("ed-when").value = u.sms_when || "raised";
  paintSites("ed", u.sms_sites);
  $("ed-link").innerHTML = "";
  paintEditExtras();
  if (!edDlg.open) edDlg.showModal();
}

// The parts that change without Save: opt-in state and the account buttons.
function paintEditExtras() {
  const u = edUser;
  const [cWord, cCls] = CONSENT[u.sms_consent] || CONSENT.none;
  const btn = !u.phone ? "" : u.sms_consent === "yes"
    ? `<button class="btn btn-sm" type="button" data-test-text="${u.id}">Send a test text</button>`
    : u.sms_consent === "stopped" ? "" : `<button class="btn btn-sm" type="button" data-opt-in="${u.id}">${u.sms_consent === "pending" ? "Resend opt-in" : "Send opt-in"}</button>`;
  $("ed-texts").innerHTML = u.phone ? `<div class="ed-line"><span class="chip ${cCls}">${cWord}</span><span class="muted small-print">${esc(consentNote(u))}</span></div>
    <div class="ed-line">${btn}${lastText(u)}</div>` : '<p class="muted small-print">Add a mobile number to text them alarms. They get one text asking them to reply YES.</p>';
  $("ed-account").innerHTML = `<div class="ed-line">${statusChip(u)}${u.invited ? inviteNote(u) : ""}
      <span class="muted small-print">Last sign-in ${esc(relTime(u.last_login_at))}${u.sessions ? `, ${u.sessions} browser${u.sessions === 1 ? "" : "s"} signed in` : ""}</span></div>
    <div class="ed-line">
      <button class="btn btn-sm" type="button" data-toggle="${u.id}">${u.is_active ? "Disable" : "Enable"}</button>
      ${u.invited ? `<button class="btn btn-sm" type="button" data-invite="${u.id}">New invite link</button>` : `<button class="btn btn-sm" type="button" data-reset="${u.id}">Reset password</button>`}
      <button class="btn btn-sm" type="button" data-security="${u.id}">Sign-in security…</button>
      <button class="btn btn-sm btn-danger" type="button" data-delete="${u.id}" style="margin-left:auto">Delete user</button></div>`;
}

async function act(promise, ok) {
  try { const r = await promise; if (ok) toast(ok); return r; }
  catch (err) { toast(esc(err.message), { kind: "error", timeout: 12000 }); }
  finally { await load().catch(() => {}); }
}

$("edit-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const u = edUser, body = {};
  const phone = $("ed-phone").value.trim(), level = $("ed-sms").value;
  if ($("ed-name").value.trim() !== u.display_name) body.display_name = $("ed-name").value.trim();
  if ($("ed-role").value !== u.role) body.role = $("ed-role").value;
  if (phone !== fmtPhone(u.phone)) body.phone = phone;
  // A first number with "No texts" picked defaults to Critical + Alarm (that's why it was added).
  const lvl = !u.phone && phone && level === "off" ? "alarm" : level;
  if (lvl !== u.sms_level) body.sms_level = lvl;
  if ($("ed-when").value !== (u.sms_when || "raised")) body.sms_when = $("ed-when").value;
  const picked = readSites("ed");
  if (!sameSites(picked, u.sms_sites)) Object.assign(body, picked == null ? { sms_all_sites: true } : { sms_sites: picked });
  if (!Object.keys(body).length) { edDlg.close(); return; }
  try {
    const r = await api(`/api/users/${u.id}`, { method: "PUT", body });
    toast(r.sms_consent === "pending" && u.sms_consent !== "pending" ? "Saved. Opt-in text sent: alarm texts start when they reply YES." : "Saved");
    edDlg.close();
  } catch (err) { toast(esc(err.message), { kind: "error", timeout: 9000 }); }
  await load();
});

$("edit-dialog").addEventListener("click", async (e) => {
  const d = e.target.dataset, u = edUser;
  if (!u || !(d.toggle || d.reset || d.invite || d.security || d.optIn || d.testText || d.delete)) return;
  if (d.toggle) {
    if (u.is_active && !confirm(`Disable ${u.email}? They're signed out and get no more alarm texts.`)) return;
    await act(api(`/api/users/${u.id}`, { method: "PUT", body: { is_active: !u.is_active } }), u.is_active ? "Disabled" : "Enabled");
  } else if (d.delete) {
    if (!confirm(`Delete ${u.display_name || u.email}? This can't be undone.\n\nThey're signed out everywhere, removed from groups and get no more alarm texts. `
      + "Alarms they handled and the audit log still show their name. You can add the same email again later.")) return;
    if (await act(api(`/api/users/${u.id}`, { method: "DELETE" }), "User deleted")) edDlg.close();
  } else if (d.reset) {
    const pw = prompt("New password. It must be at least 12 characters with upper and lower case, a number and a symbol. Their browsers are signed out.");
    if (pw) await act(api(`/api/users/${u.id}`, { method: "PUT", body: { password: pw } }), "Password reset");
  } else if (d.invite) {
    const r = await act(api(`/api/users/${u.id}/invite`, { method: "POST" }));
    if (r) { $("ed-link").innerHTML = linkBox(r); if (r.email === "sending") setTimeout(() => load().catch(() => {}), 4000); }
  } else if (d.security) {
    edDlg.close(); openSecurity(u);
  } else if (d.optIn) {
    e.target.disabled = true;
    await act(api(`/api/users/${u.id}/opt-in`, { method: "POST" }), "Opt-in text sent. Alarm texts start when they reply YES.");
    setTimeout(() => load().catch(() => {}), 2000);
  } else if (d.testText) {
    e.target.disabled = true;
    const r = await act(api(`/api/users/${u.id}/test-text`, { method: "POST" }));
    if (r) r.outcome === "sent" ? toast("Test text sent") : toast(esc(r.error || r.outcome), { kind: "error", timeout: 12000 });
  }
});

// ---------------------------------------------------------------- Add user (dialog)
const addDlg = $("add-dialog");
$("add-user").addEventListener("click", () => {
  $("user-form").reset(); $("u-pass-row").hidden = true; $("u-link").innerHTML = ""; $("u-submit").hidden = false;
  paintSites("u", null);
  addDlg.showModal(); $("u-email").focus();
});
$("u-cancel").addEventListener("click", () => addDlg.close());
const how = () => document.querySelector('input[name="u-how"]:checked').value;
document.querySelectorAll('input[name="u-how"]').forEach((r) => r.addEventListener("change", () => {
  $("u-pass-row").hidden = how() !== "password"; $("u-pass").required = how() === "password";
}));
$("user-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("u-link").innerHTML = "";
  try {
    const r = await api("/api/users", { method: "POST", body: { email: $("u-email").value, display_name: $("u-name").value,
      password: how() === "password" ? $("u-pass").value : "", role: $("u-role").value,
      phone: $("u-phone").value, sms_level: $("u-sms").value, sms_when: $("u-when").value, sms_sites: readSites("u") } });
    toast(r.setup_link ? "User invited" : "User created");
    if (r.setup_link) { $("u-link").innerHTML = linkBox(r); $("u-submit").hidden = true; }     // keep it open: the admin may copy the link
    else addDlg.close();
    await load();
    if (r.email === "sending") setTimeout(() => load().catch(() => {}), 4000);     // show whether the email went
  } catch (err) { toast(esc(err.message), { kind: "error", timeout: 9000 }); }
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
