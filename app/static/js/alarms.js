import { CFG, api, esc, fmtTime, relTime, on, openAck, PRIORITY, toast } from "./common.js";

const params = new URLSearchParams(location.search);
const filt = {
  state: params.get("state") || "open",
  site_id: params.get("site_id") || "",
  category: params.get("category") || "",
  q: params.get("q") || "",
};
const PAGE = 100;
let rows = [];            // alarms currently shown, newest first
let oldestId = null, current = null, liveTimer = null;
const listEl = document.getElementById("alarm-list");

function syncUrl() {
  const p = new URLSearchParams();
  Object.entries(filt).forEach(([k, v]) => { if (v && !(k === "state" && v === "open")) p.set(k, v); });
  history.replaceState(null, "", `/alarms${p.toString() ? "?" + p : ""}`);
}

function matches(a) {
  if (filt.state === "open" && a.state !== "new") return false;
  if (filt.state === "acknowledged" && a.state !== "acknowledged") return false;
  if (filt.site_id && String(a.site_id) !== filt.site_id) return false;
  if (filt.category && a.category !== filt.category) return false;
  if (filt.q) {
    const q = filt.q.toLowerCase();
    if (![a.caption, a.source_name, a.description].some((v) => (v || "").toLowerCase().includes(q))) return false;
  }
  return true;
}

function rowHtml(a, fresh = false) {
  const acked = a.state === "acknowledged";
  return `
  <li class="alarm p${a.priority} ${acked ? "acked" : ""} ${fresh ? "fresh" : ""}" data-id="${a.id}" tabindex="0">
    <span class="stripe"></span>
    <div>
      <div class="title">${esc(a.caption)}</div>
      <div class="meta">
        <span>${esc(a.site_name)}</span>
        ${a.source_name ? `<span>${esc(a.source_name)}</span>` : ""}
        <span>${fmtTime(a.event_ts_ms)}</span>
        ${acked ? `<span>Ack by ${esc(a.acked_by || "—")} · ${relTime(a.acked_at)}</span>` : ""}
      </div>
    </div>
    <div class="right">
      <span class="chip p${a.priority}">${PRIORITY[a.priority]}</span>
      ${acked ? '<span class="chip acked">Acknowledged</span>' : `<button class="btn btn-sm btn-primary" data-ack="${a.id}">Acknowledge</button>`}
    </div>
  </li>`;
}

function render() {
  listEl.innerHTML = rows.length ? rows.map((a) => rowHtml(a)).join("")
    : `<li class="empty">${filt.state === "open" ? "No open alarms. All clear." : "No alarms match these filters."}</li>`;
  document.getElementById("alarm-count").textContent = rows.length ? `${rows.length}${oldestId ? "+" : ""} shown` : "";
}

async function load(append = false) {
  const p = new URLSearchParams({ state: filt.state, limit: PAGE });
  if (filt.site_id) p.set("site_id", filt.site_id);
  if (filt.category) p.set("category", filt.category);
  if (filt.q) p.set("q", filt.q);
  if (append && oldestId) p.set("before_id", oldestId);
  const data = await api(`/api/alarms?${p}`);
  rows = append ? rows.concat(data) : data;
  oldestId = data.length === PAGE ? data[data.length - 1].id : null;
  document.getElementById("load-more").hidden = !oldestId;
  render();
}

// ---------------------------------------------------------------- drawer
function closeDrawer() {
  current = null;
  clearInterval(liveTimer);
  document.getElementById("drawer").hidden = true;
  document.getElementById("drawer-backdrop").hidden = true;
}

function renderDrawer(a) {
  const d = document.getElementById("drawer");
  const acked = a.state === "acknowledged";
  const nx = a.nx_ack_result;
  d.innerHTML = `
    <div class="drawer-head">
      <div><span class="chip p${a.priority}">${PRIORITY[a.priority]}</span>
        <h1 style="margin-top:8px">${esc(a.caption)}</h1>
        <div class="muted">${esc(a.site_name)}${a.source_name ? " · " + esc(a.source_name) : ""}</div></div>
      <button class="btn btn-sm" id="drawer-close" aria-label="Close">✕</button>
    </div>
    ${a.has_snapshot ? `
      <div class="snapshot"><img id="snap" alt="Camera frame at alarm time" src="/media/alarms/${a.id}/snapshot.jpg">
        <div class="snap-msg" id="snap-msg" hidden></div></div>
      <div class="seg" role="group" aria-label="Frame" style="margin-bottom:6px">
        <button type="button" class="active" data-frame="event">At alarm time</button>
        <button type="button" data-frame="live">Live</button>
      </div>` : '<p class="muted">This event has no camera attached.</p>'}
    <dl class="kv">
      <dt>Event time</dt><dd>${fmtTime(a.event_ts_ms)}</dd>
      <dt>Received</dt><dd>${fmtTime(a.received_at)}</dd>
      <dt>Type</dt><dd>${esc(a.event_type)}${a.event_subtype ? ` <span class="muted mono">${esc(a.event_subtype)}</span>` : ""}</dd>
      ${a.description ? `<dt>Description</dt><dd style="white-space:pre-wrap">${esc(a.description)}</dd>` : ""}
      <dt>NX ack rule</dt><dd>${a.nx_ack_required ? "Yes: acknowledging clears it in NX" : "No: a bookmark is added in NX"}</dd>
    </dl>
    <div class="ack-box">
      ${acked ? `
        <h2>Acknowledged</h2>
        <dl class="kv">
          <dt>By</dt><dd>${esc(a.acked_by || "—")}</dd>
          <dt>At</dt><dd>${fmtTime(a.acked_at)}</dd>
          <dt>Note</dt><dd style="white-space:pre-wrap">${esc(a.ack_note || "—")}</dd>
          <dt>NX write-back</dt><dd>${nx ? (nx.ok ? `<span class="chip acked">${esc(nx.method)}</span>` : `<span class="chip offline">failed</span> ${esc(nx.error || "")}`) : "—"}</dd>
        </dl>` : `
        <div class="field"><label for="drawer-note">Disposition note</label>
          <textarea id="drawer-note" maxlength="4000" placeholder="What did you see / do?"></textarea></div>
        <button class="btn btn-primary" id="drawer-ack">Acknowledge</button>`}
      <a class="btn btn-sm" style="margin-left:8px" href="/audit?alarm_id=${a.id}">Audit trail</a>
    </div>`;
  const img = d.querySelector("#snap");
  if (img) {
    img.addEventListener("error", () => {
      img.classList.add("failed");
      const m = d.querySelector("#snap-msg");
      m.hidden = false;
      m.textContent = "No frame available (archive not recorded at that time, or the site is unreachable).";
    });
    img.addEventListener("load", () => { img.classList.remove("failed"); d.querySelector("#snap-msg").hidden = true; });
  }
}

async function openDrawer(id) {
  let a = rows.find((r) => r.id === id);
  try { a = await api(`/api/alarms/${id}`); } catch (e) { if (!a) { toast(esc(e.message), { kind: "error" }); return; } }
  current = a;
  clearInterval(liveTimer);
  renderDrawer(a);
  document.getElementById("drawer").hidden = false;
  document.getElementById("drawer-backdrop").hidden = false;
  document.getElementById("drawer-close").focus();
}

function replaceRow(a) {
  const i = rows.findIndex((r) => r.id === a.id);
  if (i >= 0) {
    if (matches(a)) rows[i] = a; else rows.splice(i, 1);
    render();
  }
  if (current?.id === a.id) { current = a; renderDrawer(a); }
}

document.getElementById("drawer").addEventListener("click", async (e) => {
  if (e.target.id === "drawer-close") return closeDrawer();
  if (e.target.id === "drawer-ack") {
    e.target.disabled = true;
    try {
      const updated = await api(`/api/alarms/${current.id}/ack`, { method: "POST", body: { note: document.getElementById("drawer-note").value } });
      if (updated.nx_ack_result && !updated.nx_ack_result.ok) toast(`Acknowledged, but NX write-back failed: ${esc(updated.nx_ack_result.error)}`, { kind: "error", timeout: 9000 });
      else toast("Alarm acknowledged");
      replaceRow(updated);
    } catch (err) {
      toast(esc(err.message), { kind: "error" });
      e.target.disabled = false;
    }
    return;
  }
  const frame = e.target.dataset.frame;
  if (frame) {
    e.target.parentElement.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === e.target));
    const img = document.getElementById("snap");
    clearInterval(liveTimer);
    if (frame === "live") {
      const tick = () => { img.src = `/media/alarms/${current.id}/live.jpg?t=${Date.now()}`; };
      tick();
      liveTimer = setInterval(tick, 2000);
    } else {
      img.src = `/media/alarms/${current.id}/snapshot.jpg`;
    }
  }
});
document.getElementById("drawer-backdrop").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && current) closeDrawer(); });

// ---------------------------------------------------------------- list interactions
listEl.addEventListener("click", async (e) => {
  const ackId = e.target.dataset.ack;
  if (ackId) {
    e.stopPropagation();
    const updated = await openAck(rows.find((r) => r.id === Number(ackId)));
    if (updated) replaceRow(updated);
    return;
  }
  const li = e.target.closest(".alarm");
  if (li) openDrawer(Number(li.dataset.id));
});
listEl.addEventListener("keydown", (e) => {
  const li = e.target.closest(".alarm");
  if (li && e.key === "Enter") openDrawer(Number(li.dataset.id));
});
document.getElementById("load-more").addEventListener("click", () => load(true));

document.querySelectorAll("#state-seg button").forEach((b) => {
  b.classList.toggle("active", b.dataset.state === filt.state);
  b.addEventListener("click", () => {
    filt.state = b.dataset.state;
    document.querySelectorAll("#state-seg button").forEach((x) => x.classList.toggle("active", x === b));
    syncUrl(); load();
  });
});
const siteSel = document.getElementById("f-site");
const catSel = document.getElementById("f-category");
const qIn = document.getElementById("f-q");
catSel.value = filt.category;
qIn.value = filt.q;
siteSel.addEventListener("change", () => { filt.site_id = siteSel.value; syncUrl(); load(); });
catSel.addEventListener("change", () => { filt.category = catSel.value; syncUrl(); load(); });
let qTimer;
qIn.addEventListener("input", () => { clearTimeout(qTimer); qTimer = setTimeout(() => { filt.q = qIn.value.trim(); syncUrl(); load(); }, 300); });

// ---------------------------------------------------------------- live
on("alarm.new", (a) => {
  if (!matches(a) || rows.some((r) => r.id === a.id)) return;
  rows.unshift(a);
  render();
  listEl.querySelector(`[data-id="${a.id}"]`)?.classList.add("fresh");
});
on("alarm.acked", (a) => replaceRow(a));
on("reconnect", () => load().catch(() => {}));

// ---------------------------------------------------------------- init
api("/api/sites").then((sites) => {
  siteSel.innerHTML = '<option value="">All sites</option>' + sites.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
  siteSel.value = filt.site_id;
}).catch(() => {});
await load().catch((e) => { listEl.innerHTML = `<li class="empty">${esc(e.message)}</li>`; });
const openId = Number(params.get("open"));
if (openId) openDrawer(openId);
