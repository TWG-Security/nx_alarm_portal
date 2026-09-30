// Alarm sounds (Web Audio, no files).
//   critical: siren every 4 s while any critical alarm is open
//   alarm:    3-beep chime on arrival, repeated every 30 s while alarms are open
//   warning:  one soft tone on arrival
// "Silence 2 min" pauses the repeats (a NEW critical breaks the silence). Mute turns everything off.
// Browsers block audio until the user interacts with the page; a banner asks for that click.
// With several portal tabs open, only one "leader" tab plays, so sounds don't stack.

import { openAlarms, openCounts, on } from "./common.js";

const SILENCE_MS = 120_000;
const REPEAT_MS = { 1: 4_000, 2: 30_000 };
const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage blocked */ } },
};

let ctx = null;
try { ctx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { /* no audio */ }
const running = () => ctx && ctx.state === "running";

let muted = store.get("twg-muted") === "1";
const silencedUntil = () => Number(store.get("twg-silenced-until") || 0);
export const isSilenced = () => Date.now() < silencedUntil();
export function silence(ms = SILENCE_MS) { store.set("twg-silenced-until", String(Date.now() + ms)); paint(); }
function unsilence() { store.set("twg-silenced-until", "0"); paint(); }

// ---------------------------------------------------------------- tones
function tone(freq, start, dur, { type = "sine", gain = 0.22, freqEnd } = {}) {
  const o = ctx.createOscillator(), g = ctx.createGain();
  o.type = type;
  o.frequency.setValueAtTime(freq, start);
  if (freqEnd) o.frequency.linearRampToValueAtTime(freqEnd, start + dur);
  g.gain.setValueAtTime(0.0001, start);
  g.gain.exponentialRampToValueAtTime(gain, start + 0.02);
  g.gain.setValueAtTime(gain, start + dur - 0.04);
  g.gain.exponentialRampToValueAtTime(0.0001, start + dur);
  o.connect(g).connect(ctx.destination);
  o.start(start);
  o.stop(start + dur + 0.02);
}

export function play(level, { force = false } = {}) {
  if (!ctx || (!force && (muted || !isLeader()))) return;
  if (!running()) { ctx.resume().catch(() => {}); if (!force) return; }
  const t = ctx.currentTime + 0.02;
  if (level === "critical") {
    for (let i = 0; i < 4; i++) tone(i % 2 ? 1250 : 650, t + i * 0.36, 0.34, { type: "square", gain: 0.16, freqEnd: i % 2 ? 650 : 1250 });
  } else if (level === "alarm") {
    [0, 0.2, 0.4].forEach((d) => tone(988, t + d, 0.13, { gain: 0.28 }));
  } else if (level === "connection") {
    [0, 0.3, 0.6].forEach((d, i) => tone(784 - i * 150, t + d, 0.25, { type: "triangle", gain: 0.2 }));
  } else {
    tone(587, t, 0.18, { gain: 0.14 });
    tone(440, t + 0.2, 0.26, { gain: 0.12 });
  }
}

// Losing the live connection means alarms could be late: say so out loud, every 10 s.
let lastConnTone = 0;
on("connection", ({ down, loud }) => {
  if (down && loud && Date.now() - lastConnTone >= 10_000) { play("connection"); lastConnTone = Date.now(); }
  if (!down) lastConnTone = 0;
});

// ---------------------------------------------------------------- one tab plays
const TAB_ID = Math.random().toString(36).slice(2);
function isLeader() {
  const now = Date.now();
  let lead = {};
  try { lead = JSON.parse(store.get("twg-sound-leader") || "{}"); } catch (e) { /* ignore */ }
  if (!lead.id || lead.id === TAB_ID || now - (lead.ts || 0) > 3000) {
    store.set("twg-sound-leader", JSON.stringify({ id: TAB_ID, ts: now }));
    return true;
  }
  return false;
}
window.addEventListener("beforeunload", () => {
  try { if (JSON.parse(store.get("twg-sound-leader") || "{}").id === TAB_ID) localStorage.removeItem("twg-sound-leader"); } catch (e) { /* ignore */ }
});

// ---------------------------------------------------------------- repeat loop
const lastPlayed = { 1: 0, 2: 0 };
setInterval(() => {
  const leader = isLeader();   // also keeps this tab's leadership fresh
  if (!leader || muted || !running() || isSilenced()) return;
  const c = openCounts();
  const now = Date.now();
  if (c[1] > 0) {
    if (now - lastPlayed[1] >= REPEAT_MS[1]) { play("critical"); lastPlayed[1] = now; }
  } else if (c[2] > 0 && now - lastPlayed[2] >= REPEAT_MS[2]) {
    play("alarm"); lastPlayed[2] = now;
  }
}, 500);

on("alarm.arrived", (a) => {
  if (a.priority === 1) { unsilence(); lastPlayed[1] = 0; return; }   // the loop sounds the siren right away
  const now = Date.now();
  if (a.priority === 2) { play("alarm"); lastPlayed[2] = now; }
  else play("warning");
});

// ---------------------------------------------------------------- UI: banner, mute, silence
const banner = document.getElementById("sound-banner");
const muteBtn = document.getElementById("mute-toggle");
const silenceBtn = document.getElementById("silence-btn");

function paint() {
  const c = openCounts();
  const loud = c[1] + c[2] > 0;
  if (banner) banner.hidden = muted || running() || !ctx;
  if (muteBtn) {
    muteBtn.querySelector(".sound-on")?.toggleAttribute("hidden", muted);
    muteBtn.querySelector(".sound-off")?.toggleAttribute("hidden", !muted);
    muteBtn.title = muted ? "Sound is muted. Click to unmute" : "Mute all alarm sound";
    muteBtn.classList.toggle("attention", muted && loud);
  }
  if (silenceBtn) {
    silenceBtn.hidden = muted || !loud;
    const left = silencedUntil() - Date.now();
    silenceBtn.classList.toggle("active", left > 0);
    silenceBtn.textContent = left > 0 ? `Silenced ${Math.floor(left / 60000)}:${String(Math.floor(left / 1000) % 60).padStart(2, "0")}` : "Silence 2 min";
  }
}
setInterval(paint, 1000);
on("store", paint);

function unlock() { if (ctx && !running()) ctx.resume().then(paint).catch(() => {}); }
["pointerdown", "keydown", "touchstart"].forEach((ev) => document.addEventListener(ev, unlock, { capture: true }));
if (ctx) ctx.onstatechange = paint;
banner?.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") unlock(); });

muteBtn?.addEventListener("click", () => {
  muted = !muted;
  store.set("twg-muted", muted ? "1" : "0");
  paint();
});
silenceBtn?.addEventListener("click", () => { isSilenced() ? unsilence() : silence(); });
paint();
