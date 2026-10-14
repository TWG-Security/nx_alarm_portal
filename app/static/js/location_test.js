// "Request location" on the guard's pages (location page /g/, alarm page /a/): one location request, made in the
// tap, and a report of exactly what the browser answered: secure page, the permission state it claims (Safari
// misreports it), the error code and message, and how long it took (Safari refusing in a few ms means a setting
// blocks the site; no prompt was shown). The report shows on the phone and goes to the portal's audit log
// ("location.test") so TWG can see why a phone won't share. It doesn't start or change sharing.
const CODES = { 1: "PERMISSION_DENIED", 2: "POSITION_UNAVAILABLE", 3: "TIMEOUT" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function permState() {
  try { return (await navigator.permissions.query({ name: "geolocation" })).state; } catch { return "unsupported"; }
}

function verdict(r) {
  if (!r.secure) return "This page isn't treated as secure, so the phone won't give it a location. Open the link exactly as it was texted (https://).";
  if (!r.has_geo) return "This browser can't share a location.";
  if (r.outcome === "ok") return `Location works (accurate to ${Math.round(r.accuracy)} m). Tap Start sharing / Share my location now.`;
  if (r.code === 1 && r.ms < 400) return "The phone refused at once, without asking: a setting blocks location for this site in Safari. See “Location blocked?” below (steps 1-3), then try again.";
  if (r.code === 1) return "Location was refused (Don't Allow was tapped, or a phone setting). See “Location blocked?” below, then try again.";
  if (r.code === 2) return "The phone couldn't work out where it is. Check that Location Services is on, step outside or near a window, and try again.";
  if (r.code === 3) return "The phone took too long to find its position. Try again outside or near a window.";
  return "Something unexpected happened. The details went to TWG.";
}

/** postUrl: where the report goes (/api/g/<token>/diag or /api/a/<token>/diag). */
export function mountLocationTest(postUrl) {
  const btn = document.getElementById("loc-test"), out = document.getElementById("loc-test-out");
  if (!btn || !out) return;
  btn.hidden = false;
  btn.addEventListener("click", async () => {
    const r = { ua: navigator.userAgent.slice(0, 400), secure: !!window.isSecureContext, has_geo: "geolocation" in navigator,
                framed: window.top !== window.self, standalone: navigator.standalone ?? null, visibility: document.visibilityState,
                perm_before: "", perm_after: "", outcome: "", code: null, message: "", ms: 0, accuracy: null };
    btn.disabled = true;
    out.innerHTML = '<p class="muted">Asking the phone for its location…</p>';
    const t0 = performance.now();
    const answer = r.has_geo ? new Promise((resolve) => {
      // In the tap itself, before anything is awaited: Safari only asks "Allow location?" for a request made in a tap.
      navigator.geolocation.getCurrentPosition(
        (p) => resolve({ outcome: "ok", accuracy: p.coords.accuracy }),
        (e) => resolve({ outcome: "error", code: e.code, message: String(e.message || CODES[e.code] || "").slice(0, 200) }),
        { enableHighAccuracy: true, maximumAge: 10000, timeout: 20000 });     // a fix up to 10 s old still proves the permission
    }) : Promise.resolve({ outcome: "unavailable" });
    r.perm_before = await permState();
    Object.assign(r, await answer);
    r.ms = Math.round(performance.now() - t0);
    r.perm_after = await permState();
    const rows = [["Secure page", r.secure ? "yes" : "NO"], ["Location available", r.has_geo ? "yes" : "NO"],
      ["Permission (as the browser reports it)", `${r.perm_before} → ${r.perm_after}`],
      ["Answer", r.outcome === "ok" ? `position, accurate to ${Math.round(r.accuracy)} m` : r.outcome === "error" ? `${CODES[r.code] || r.code}: ${r.message}` : r.outcome],
      ["Time to answer", `${r.ms} ms`]];
    out.innerHTML = `<div class="loc-test-result loc-test-result--${r.outcome === "ok" ? "ok" : "bad"}"><b>${esc(verdict(r))}</b>
      <dl>${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl><p class="muted" data-sent>Sending this to TWG…</p></div>`;
    try {
      const res = await fetch(postUrl, { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "omit", body: JSON.stringify(r) });
      out.querySelector("[data-sent]").textContent = res.ok ? "Sent to TWG for troubleshooting ✓" : `Couldn't send it to TWG (HTTP ${res.status}).`;
    } catch { out.querySelector("[data-sent]").textContent = "Couldn't send it to TWG (no connection)."; }
    btn.disabled = false;
  });
}
