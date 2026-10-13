// The guard's location-sharing page (/g/<token>, app/routers/share.py). The sharing itself is share_core.js.
import { askLocationNow, createSharer } from "./share_core.js";

const { token, everyS } = window.PORTAL;
const $ = (id) => document.getElementById(id);
const KEY = "twg-share-on";                 // reopening the page resumes sharing

const sharer = createSharer(token, {
  everyS,
  onState(kind, text, facts) {
    $("share-state").className = `share-state share-state--${kind === "gone" || kind === "blocked" ? "off" : kind}`;
    if (kind === "blocked") document.querySelector(".share-help").open = true;
    $("share-state").textContent = text;
    $("share-facts").textContent = facts;
    const on = !["off", "gone", "blocked"].includes(kind);
    $("share-start").hidden = on; $("share-stop").hidden = !on;
    try { if (on) localStorage.setItem(KEY, "1"); else localStorage.removeItem(KEY); } catch { /* not remembered */ }
  },
});
$("share-start").addEventListener("click", () => { askLocationNow(); sharer.start(); });
$("share-stop").addEventListener("click", () => sharer.stop());
try { if (localStorage.getItem(KEY) === "1") sharer.start(); } catch { /* not remembered */ }
