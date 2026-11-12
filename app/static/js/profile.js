// Client profile (the SOC, 2026-10-25; app/services/profile.py): the Client card in the map's site panel, the
// Response box in the alarm details (call the police / the client / a contact; open the client in Ajax), and the
// admins' editor. Call buttons are tel: links (the PC's softphone dials) and are logged on the alarm's timeline.
import { CFG, api, esc, toast, emit } from "./common.js";

export const PACKAGE = { ground_response: "Ground response", monitoring_only: "Monitoring only" };
const PACKAGE_TIP = {
  ground_response: "This client pays for ground response: send a guard when it's real",
  monitoring_only: "Monitoring only: this client has NO ground response. Don't send a guard; call the client / police",
};
const telHref = (n) => `tel:${String(n || "").replace(/[^0-9+*#,;]/g, "")}`;
const hasPhone = (n) => /\d/.test(n || "");

export function packageChip(pkg, { big = false } = {}) {
  if (!PACKAGE[pkg]) return "";
  return `<span class="chip pkg pkg--${pkg}${big ? " pkg--big" : ""}" title="${esc(PACKAGE_TIP[pkg])}">${PACKAGE[pkg]}</span>`;
}

const callLink = (to, name, number, label, alarmId, cls = "btn btn-sm") =>
  `<a class="${cls}" href="${esc(telHref(number))}" data-called="${to}" data-name="${esc(name)}" data-number="${esc(number)}"
     ${alarmId ? `data-alarm="${alarmId}"` : ""} title="Dial ${esc(number)} on this computer's phone app">${label}</a>`;

// The map's site panel: who the client is and how to reach them.
export function clientCardHtml(site) {
  const p = site?.profile || {};
  const any = p.client_name || p.phone || (p.contacts || []).length || p.package || p.notes || p.passcode || p.ajax_url;
  if (!any) {
    return CFG.isAdmin ? `<div class="client-card client-card--empty"><span class="muted">No client profile yet.</span>
      <button type="button" class="btn btn-sm" data-profile-edit="${site.id}">Add client profile</button></div>` : "";
  }
  const contacts = (p.contacts || []).map((c) => `<li><span><b>${esc(c.name || "Contact")}</b>${c.role ? ` <span class="muted">· ${esc(c.role)}</span>` : ""}</span>
      ${hasPhone(c.phone) ? callLink("contact", c.name, c.phone, esc(c.phone), null, "client-tel") : ""}</li>`).join("");
  return `<div class="client-card">
    <div class="client-head">${packageChip(p.package, { big: true }) || '<span class="chip">Package not set</span>'}
      ${CFG.isAdmin ? `<button type="button" class="btn btn-sm" data-profile-edit="${site.id}">Edit</button>` : ""}</div>
    ${p.client_name ? `<div class="client-name">${esc(p.client_name)}</div>` : ""}
    ${hasPhone(p.phone) ? `<div class="client-line">${callLink("client", p.client_name, p.phone, esc(p.phone), null, "client-tel")}</div>` : ""}
    ${contacts ? `<ul class="client-contacts">${contacts}</ul>` : ""}
    ${p.passcode ? `<div class="client-line"><span class="muted">Passcode</span> <button type="button" class="btn btn-sm" data-reveal="${esc(p.passcode)}">Show</button></div>` : ""}
    ${p.notes ? `<div class="client-notes">${esc(p.notes)}</div>` : ""}
    ${p.ajax_url ? `<a class="btn btn-sm" href="${esc(p.ajax_url)}" target="_blank" rel="noopener noreferrer">Open in Ajax ↗</a>` : ""}
  </div>`;
}

// The alarm details' Response box.
export function responseHtml(site, alarm) {
  if (!site) return "";
  const p = site.profile || {};
  const police = hasPhone(p.police_phone) ? p.police_phone : "911";
  const btns = [
    callLink("police", "Police", police, `Call police <span class="muted">${esc(police)}</span>`, alarm.id, "btn btn-call btn-call--police"),
    hasPhone(p.phone) ? callLink("client", p.client_name || "Client", p.phone, `Call client <span class="muted">${esc(p.client_name || p.phone)}</span>`, alarm.id, "btn btn-call") : "",
    ...(p.contacts || []).filter((c) => hasPhone(c.phone)).slice(0, 3)
      .map((c) => callLink("contact", c.name, c.phone, `Call ${esc(c.name || c.phone)}${c.role ? ` <span class="muted">${esc(c.role)}</span>` : ""}`, alarm.id, "btn btn-call")),
    p.ajax_url ? `<a class="btn btn-call" href="${esc(p.ajax_url)}" target="_blank" rel="noopener noreferrer" data-ajax>Open in Ajax ↗</a>` : "",
  ].filter(Boolean).join("");
  const note = hasPhone(p.police_phone) ? "" : `<span class="muted resp-note" title="A 911 call from this computer reaches the 911 centre for the monitoring station's registered address, not the client's. Add the police dispatch number for the client's area to the client profile.">911 from here reaches the 911 centre for <b>this office's</b> address: give the client's address. ${CFG.isAdmin ? "Set the local police number in the client profile." : ""}</span>`;
  return `<div class="response-box">
    <div class="response-head"><span class="response-lead">Response</span>${packageChip(p.package, { big: true }) || '<span class="chip">Package not set</span>'}
      ${p.passcode ? `<button type="button" class="btn btn-sm" data-reveal="${esc(p.passcode)}">Passcode</button>` : ""}
      ${CFG.isAdmin ? `<button type="button" class="btn btn-sm" data-profile-edit="${site.id}">Client profile</button>` : ""}</div>
    <div class="response-btns">${btns}</div>${note}
    ${p.notes ? `<div class="client-notes">${esc(p.notes)}</div>` : ""}</div>`;
}

// ---------------------------------------------------------------- clicks: log calls, reveal the passcode, edit
document.addEventListener("click", (e) => {
  const call = e.target.closest("[data-called]");
  if (call && call.dataset.alarm) {        // the tel: link still opens the softphone; this only logs it
    api(`/api/alarms/${call.dataset.alarm}/called`, { method: "POST",
      body: { to: call.dataset.called, name: call.dataset.name || "", number: call.dataset.number || "" } }).catch(() => {});
  }
  const rev = e.target.closest("[data-reveal]");
  if (rev) { e.preventDefault(); rev.outerHTML = `<b class="mono passcode">${esc(rev.dataset.reveal)}</b>`; return; }
  const ed = e.target.closest("[data-profile-edit]");
  if (ed) { e.preventDefault(); openProfileEditor(Number(ed.dataset.profileEdit)); }
});

// ---------------------------------------------------------------- editor (administrators)
const dlg = document.createElement("dialog");
dlg.className = "profile-dialog";
document.body.append(dlg);

const contactRow = (c = {}) => `<div class="pf-contact">
  <input type="text" maxlength="120" placeholder="Name" value="${esc(c.name || "")}" data-c="name" aria-label="Contact name">
  <input type="text" maxlength="80" placeholder="Role (spouse, keyholder…)" value="${esc(c.role || "")}" data-c="role" aria-label="Contact role">
  <input type="tel" maxlength="40" placeholder="Phone" value="${esc(c.phone || "")}" data-c="phone" aria-label="Contact phone">
  <button type="button" class="btn btn-sm" data-pf-del aria-label="Remove this contact">✕</button></div>`;

export async function openProfileEditor(siteId) {
  let site;
  try { site = await api(`/api/sites/${siteId}`); } catch (e) { toast(esc(e.message), { kind: "error" }); return; }
  const p = site.profile || {};
  dlg.innerHTML = `<form method="dialog" class="pf-form">
    <h2>Client profile · ${esc(site.name)}</h2>
    <div class="field"><label>Package</label><div class="seg pf-pkg" role="radiogroup">
      ${[["ground_response", "Ground response"], ["monitoring_only", "Monitoring only"], ["", "Not set"]].map(([v, l]) =>
        `<label><input type="radio" name="pf-package" value="${v}" ${(p.package || "") === v ? "checked" : ""}> ${l}</label>`).join("")}</div>
      <p class="muted">Shown in large on every alarm from this site, so operators know whether to send a guard.</p></div>
    <div class="pf-grid">
      <div class="field"><label for="pf-name">Client name</label><input id="pf-name" type="text" maxlength="200" value="${esc(p.client_name || "")}"></div>
      <div class="field"><label for="pf-phone">Client phone</label><input id="pf-phone" type="tel" maxlength="40" value="${esc(p.phone || "")}"></div>
      <div class="field"><label for="pf-police">Police dispatch number for this area</label><input id="pf-police" type="tel" maxlength="40" placeholder="911" value="${esc(p.police_phone || "")}">
        <p class="muted">911 from the monitoring station reaches the 911 centre for the station's address, not the client's.</p></div>
      <div class="field"><label for="pf-pass">Passcode / verbal password</label><input id="pf-pass" type="text" maxlength="120" autocomplete="off" value="${esc(p.passcode || "")}"></div>
    </div>
    <div class="field"><label>Other contacts</label><div class="pf-contacts">${(p.contacts || []).map(contactRow).join("")}</div>
      <button type="button" class="btn btn-sm" data-pf-add>+ Add contact</button></div>
    <div class="field"><label for="pf-notes">Notes for operators</label><textarea id="pf-notes" maxlength="4000" rows="3" placeholder="Access, gate code, pets, vehicles, when they're away…">${esc(p.notes || "")}</textarea></div>
    <div class="field"><label for="pf-ajax">Ajax link</label><input id="pf-ajax" type="text" maxlength="500" placeholder="https://… (this client in Ajax PRO / Ajax web)" value="${esc(p.ajax_url || "")}"></div>
    <p class="pf-error text-warn" hidden></p>
    <div class="dialog-actions"><button type="button" class="btn" value="cancel" data-pf-cancel>Cancel</button>
      <button type="submit" class="btn btn-primary" value="save">Save</button></div></form>`;
  const $ = (s) => dlg.querySelector(s);
  dlg.onclick = (e) => {
    if (e.target.closest("[data-pf-add]") && dlg.querySelectorAll(".pf-contact").length < 8) $(".pf-contacts").insertAdjacentHTML("beforeend", contactRow());
    if (e.target.closest("[data-pf-del]")) e.target.closest(".pf-contact").remove();
    if (e.target.closest("[data-pf-cancel]")) dlg.close();
  };
  $(".pf-form").onsubmit = async (e) => {
    e.preventDefault();
    const body = {
      package: dlg.querySelector("input[name=pf-package]:checked")?.value || "",
      client_name: $("#pf-name").value, phone: $("#pf-phone").value, police_phone: $("#pf-police").value,
      passcode: $("#pf-pass").value, notes: $("#pf-notes").value, ajax_url: $("#pf-ajax").value,
      contacts: [...dlg.querySelectorAll(".pf-contact")].map((r) => Object.fromEntries([...r.querySelectorAll("[data-c]")].map((i) => [i.dataset.c, i.value]))),
    };
    try {
      const s = await api(`/api/sites/${siteId}/profile`, { method: "PUT", body });
      emit("site.updated", s);
      toast("Client profile saved");
      dlg.close();
    } catch (err) { const el = $(".pf-error"); el.hidden = false; el.textContent = err.message; }
  };
  dlg.showModal();
}
