// Alarm clip player: looping archive clip around the alarm, a timeline with the alarm
// marker and object-presence marks, time controls, and analytics bounding boxes drawn in sync.

import { CFG, api, esc } from "./common.js";

const STEP_S = 15;               // "earlier / later" widens the window by this much
const MAX_WINDOW_S = CFG.clip?.max || 300;
const BOX_HOLD_MS = 500;         // keep a box on screen this long after its last sample
const SPEEDS = [0.25, 0.5, 1, 2, 4];

const clock = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const clockTenths = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", fractionalSecondDigits: 1 });
const offset = (ms) => `${ms < 0 ? "−" : "+"}${(Math.abs(ms) / 1000).toFixed(1)}s`;

export function mountPlayer(root, alarm, { autoplay = true } = {}) {
  let pre = CFG.clip?.pre ?? 10, post = CFG.clip?.post ?? 20, quality = "sd";
  let info = null, tracks = [], showBoxes = true, live = false, destroyed = false;
  let pollTimer = null, liveTimer = null, raf = null;

  root.innerHTML = `
    <div class="player">
      <div class="player-stage">
        <img class="player-poster" alt="" src="/media/alarms/${alarm.id}/snapshot.jpg">
        <video muted playsinline loop preload="auto" hidden></video>
        <img class="player-live" alt="Live view" hidden>
        <canvas class="player-boxes"></canvas>
        <div class="player-msg"><span class="spinner"></span><span class="txt">Preparing clip…</span></div>
        <div class="player-clock" hidden></div>
      </div>
      <div class="player-timeline" hidden>
        <div class="tl-track">
          <div class="tl-objects"></div>
          <div class="tl-progress"></div>
          <div class="tl-alarm" title="Alarm time"></div>
          <input class="tl-range" type="range" min="0" max="1000" value="0" step="1" aria-label="Clip position">
        </div>
        <div class="tl-labels"><span class="tl-start"></span><span class="tl-legend"></span><span class="tl-end"></span></div>
      </div>
      <div class="player-controls">
        <button class="btn btn-sm" data-c="earlier" title="Add ${STEP_S}s before the clip">« ${STEP_S}s</button>
        <button class="btn btn-sm icon" data-c="back" title="Back 0.1s (←)" aria-label="Back one step"><svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" aria-hidden="true"><path d="M6 5h2v14H6zM20 5v14L9 12z"/></svg></button>
        <button class="btn btn-sm btn-primary icon" data-c="play" title="Play / pause (space)">❚❚</button>
        <button class="btn btn-sm icon" data-c="fwd" title="Forward 0.1s (→)" aria-label="Forward one step"><svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" aria-hidden="true"><path d="M16 5h2v14h-2zM4 5v14l11-7z"/></svg></button>
        <button class="btn btn-sm" data-c="later" title="Add ${STEP_S}s after the clip">${STEP_S}s »</button>
        <button class="btn btn-sm" data-c="alarm" title="Jump to the alarm moment">⚑ Alarm</button>
        <select class="player-speed" aria-label="Speed">${SPEEDS.map((s) => `<option value="${s}" ${s === 1 ? "selected" : ""}>${s}×</option>`).join("")}</select>
        <span class="player-toggles">
          <button class="btn btn-sm toggle on" data-c="loop" title="Loop the clip">Loop</button>
          <button class="btn btn-sm toggle on" data-c="boxes" title="Show analytics boxes" hidden>Boxes</button>
          <button class="btn btn-sm toggle" data-c="hd" title="Full-quality stream (slower to prepare)">HD</button>
          <button class="btn btn-sm toggle" data-c="live" title="Live view from this camera">Live</button>
          <a class="btn btn-sm" data-c="download" title="Download clip (MP4)" hidden>Download</a>
        </span>
      </div>
    </div>`;
  const $ = (s) => root.querySelector(s);
  const video = $("video"), canvas = $("canvas"), ctx = canvas.getContext("2d");
  const msg = $(".player-msg"), poster = $(".player-poster"), liveImg = $(".player-live");
  const range = $(".tl-range");
  poster.addEventListener("error", () => { poster.hidden = true; });

  function say(text, spin = true) {
    msg.hidden = !text;
    msg.querySelector(".txt").textContent = text || "";
    msg.querySelector(".spinner").hidden = !spin;
  }

  // ------------------------------------------------------------ loading
  async function load(keepAbsMs) {
    clearTimeout(pollTimer);
    if (destroyed) return;
    let r;
    try {
      r = await api(`/api/alarms/${alarm.id}/clip?pre=${pre}&post=${post}&quality=${quality}`);
    } catch (e) {
      return say(e.message, false);
    }
    if (destroyed) return;
    if (r.status === "pending") {
      say(`Recording in progress: clip ready in ${Math.ceil(r.ready_in_s)}s`);
      pollTimer = setTimeout(() => load(keepAbsMs), Math.min(3000, r.ready_in_s * 1000 + 200));
      return;
    }
    if (r.status === "processing") {
      say(quality === "hd" ? "Preparing HD clip (NX is converting it)…" : "Preparing clip…");
      pollTimer = setTimeout(() => load(keepAbsMs), 1500);
      return;
    }
    if (r.status === "error") return say(`No clip: ${r.message}`, false);
    info = r;
    video.src = r.url;
    const dl = $('[data-c="download"]');
    dl.href = r.url; dl.download = `alarm-${alarm.id}-${quality}.mp4`; dl.hidden = false;
    video.addEventListener("loadedmetadata", () => {
      const target = keepAbsMs ?? alarm.event_ts_ms - 3000;   // start just before the alarm
      video.currentTime = Math.max(0, Math.min(video.duration - 0.1, (target - info.start_ms) / 1000));
      video.playbackRate = Number($(".player-speed").value);
      video.hidden = false; poster.hidden = true; say("");
      $(".player-timeline").hidden = false; $(".player-clock").hidden = false;
      $(".tl-start").textContent = clock(info.start_ms);
      $(".tl-end").textContent = clock(info.start_ms + info.duration_ms);
      placeAlarmMarker();
      if (autoplay) video.play().catch(() => {});
      loadObjects();
    }, { once: true });
    video.addEventListener("error", () => say("This browser couldn't play the clip.", false), { once: true });
  }

  async function loadObjects() {
    try {
      tracks = await api(`/api/alarms/${alarm.id}/objects?from_ms=${info.start_ms}&to_ms=${info.start_ms + info.duration_ms}`);
    } catch (e) { tracks = []; }
    if (destroyed) return;
    $('[data-c="boxes"]').hidden = !tracks.length;
    const span = info.duration_ms;
    $(".tl-objects").innerHTML = tracks.map((t) => {
      const a = Math.max(t.boxes[0][0], info.start_ms), b = Math.min(t.boxes[t.boxes.length - 1][0] + BOX_HOLD_MS, info.start_ms + span);
      if (b <= a) return "";
      return `<span class="${t.primary ? "primary" : ""}" style="left:${((a - info.start_ms) / span) * 100}%;width:${Math.max(0.6, ((b - a) / span) * 100)}%" title="${esc(t.label)}"></span>`;
    }).join("");
    const labels = [...new Set(tracks.map((t) => t.label))];
    $(".tl-legend").textContent = tracks.length ? `${tracks.length} object${tracks.length > 1 ? "s" : ""}: ${labels.join(", ")}` : "No analytics objects";
    draw();
  }

  function placeAlarmMarker() {
    $(".tl-alarm").style.left = `${((alarm.event_ts_ms - info.start_ms) / info.duration_ms) * 100}%`;
  }

  // ------------------------------------------------------------ drawing
  const absNow = () => info.start_ms + video.currentTime * 1000;

  function boxAt(track, t) {
    const b = track.boxes;
    let lo = 0, hi = b.length - 1, idx = -1;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (b[mid][0] <= t) { idx = mid; lo = mid + 1; } else hi = mid - 1; }
    if (idx < 0) return null;
    const [ts, dur] = b[idx];
    return t - ts <= Math.max(dur, BOX_HOLD_MS) ? b[idx] : null;
  }

  function draw() {
    if (!info || destroyed) return;
    const t = absNow();
    // timeline + clock
    const frac = Math.min(1, Math.max(0, video.currentTime * 1000 / info.duration_ms));
    if (document.activeElement !== range) range.value = String(Math.round(frac * 1000));
    $(".tl-progress").style.width = `${frac * 100}%`;
    $(".player-clock").innerHTML = `${esc(clockTenths(t))} <span>${esc(offset(t - alarm.event_ts_ms))} from alarm</span>`;
    $('[data-c="play"]').textContent = video.paused ? "▶" : "❚❚";

    // boxes, letterboxed like object-fit: contain
    const W = canvas.clientWidth, H = canvas.clientHeight, dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) { canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr); }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    if (!showBoxes || live || !tracks.length || !video.videoWidth) return;
    const s = Math.min(W / video.videoWidth, H / video.videoHeight);
    const dw = video.videoWidth * s, dh = video.videoHeight * s, ox = (W - dw) / 2, oy = (H - dh) / 2;
    ctx.font = "600 12px Arial, Helvetica, sans-serif";
    for (const tr of tracks) {
      const b = boxAt(tr, t);
      if (!b) continue;
      const [, , x, y, w, h] = b;
      const rx = ox + x * dw, ry = oy + y * dh, rw = w * dw, rh = h * dh;
      const color = tr.primary ? "#E74C3C" : "#E08A30";
      ctx.lineWidth = 2; ctx.strokeStyle = color; ctx.strokeRect(rx, ry, rw, rh);
      const attr = Object.values(tr.attributes || {})[0];
      const label = attr ? `${tr.label} · ${attr}` : tr.label;
      const tw = ctx.measureText(label).width + 8;
      const ly = ry > 18 ? ry - 18 : ry + rh;
      ctx.fillStyle = color; ctx.fillRect(rx - 1, ly, tw, 18);
      ctx.fillStyle = "#fff"; ctx.fillText(label, rx + 3, ly + 13);
    }
  }
  function loop() { draw(); raf = requestAnimationFrame(loop); }
  raf = requestAnimationFrame(loop);

  // ------------------------------------------------------------ controls
  function setLive(on) {
    live = on;
    $('[data-c="live"]').classList.toggle("on", on);
    clearInterval(liveTimer);
    liveImg.hidden = !on;
    if (on) {
      video.pause();
      const tick = () => { liveImg.src = `/media/alarms/${alarm.id}/live.jpg?t=${Date.now()}`; };
      tick(); liveTimer = setInterval(tick, 1500);
      $(".player-clock").innerHTML = '<b class="live-dot">● LIVE</b>';
    } else if (info) {
      video.play().catch(() => {});
    }
  }

  function widen(which) {
    if (pre + post + STEP_S > MAX_WINDOW_S) return;
    const keep = info ? absNow() : undefined;
    if (which === "earlier") pre += STEP_S; else post += STEP_S;
    info = null; video.hidden = true; poster.hidden = false;
    load(keep);
  }

  root.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-c]");
    const c = btn?.dataset.c;
    if (!c) return;
    if (c === "download") return;          // let the link work
    e.preventDefault();
    if (c === "live") return setLive(!live);
    if (live) setLive(false);
    if (c === "earlier" || c === "later") return widen(c);
    if (c === "hd") {
      quality = quality === "hd" ? "sd" : "hd";
      btn.classList.toggle("on", quality === "hd");
      const keep = info ? absNow() : undefined; info = null; video.hidden = true; poster.hidden = false;
      return load(keep);
    }
    if (c === "loop") { video.loop = !video.loop; btn.classList.toggle("on", video.loop); return; }
    if (c === "boxes") { showBoxes = !showBoxes; btn.classList.toggle("on", showBoxes); return draw(); }
    if (!info) return;
    if (c === "play") return video.paused ? video.play().catch(() => {}) : video.pause();
    if (c === "back" || c === "fwd") { video.pause(); video.currentTime = Math.max(0, video.currentTime + (c === "fwd" ? 0.1 : -0.1)); return; }
    if (c === "alarm") { video.currentTime = Math.max(0, (alarm.event_ts_ms - info.start_ms) / 1000); draw(); }
  });
  $(".player-speed").addEventListener("change", (e) => { video.playbackRate = Number(e.target.value); });
  range.addEventListener("input", () => { if (info) { video.currentTime = (Number(range.value) / 1000) * (info.duration_ms / 1000); draw(); } });
  video.addEventListener("seeked", draw);
  root.addEventListener("keydown", (e) => {
    if (e.target.matches("textarea, input:not(.tl-range), select")) return;
    if (e.key === " ") { e.preventDefault(); root.querySelector('[data-c="play"]').click(); }
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); root.querySelector(`[data-c="${e.key === "ArrowLeft" ? "back" : "fwd"}"]`).click(); }
  });
  root.tabIndex = -1;

  if (alarm.device_id) load(); else say("This event has no camera attached.", false);

  return {
    destroy() {
      destroyed = true;
      clearTimeout(pollTimer); clearInterval(liveTimer); cancelAnimationFrame(raf);
      video.pause(); video.removeAttribute("src"); video.load();
      root.innerHTML = "";
    },
  };
}
