// Live view of an alarm's camera: NX's live WebM relayed by the portal (/media/alarms/<id>/live.webm,
// ~0.8 Mbit/s). Kept near real time, paused while the tab is hidden, and falling back to a still
// every 1.5 s if the stream fails.

const MAX_BEHIND_S = 1.5;        // jump to the newest frame if playback falls this far behind
const STALL_MS = 8000;           // no new frames this long = treat the stream as dead
const STILL_EVERY_MS = 1500;

export function mountLive(root, alarm, { label = true } = {}) {
  root.innerHTML = `
    <div class="live-pane">
      <div class="live-stage">
        <video muted playsinline autoplay></video>
        <img class="live-still" alt="Live still" hidden>
        <div class="live-badge"><span class="live-dot">●</span> LIVE</div>
        <div class="live-clock"></div>
        <div class="player-msg"><span class="spinner"></span><span class="txt">Connecting to the camera…</span></div>
      </div>
      ${label ? `<div class="live-bar"><span class="live-state muted">Connecting…</span>
        <button class="btn btn-sm" data-l="pause" type="button">Pause</button>
        <button class="btn btn-sm" data-l="retry" type="button" hidden>Retry video</button></div>` : ""}
    </div>`;
  const $ = (s) => root.querySelector(s);
  const video = $("video"), still = $(".live-still"), msg = $(".player-msg");
  let destroyed = false, paused = false, mode = "off", lastProgress = 0, stillTimer = null, watch = null;

  const state = (text) => { const el = $(".live-state"); if (el) el.textContent = text; };
  const say = (text) => { msg.hidden = !text; msg.querySelector(".txt").textContent = text || ""; };

  function stopVideo() {
    video.pause(); video.removeAttribute("src"); video.load();
  }
  function stopAll() {
    stopVideo(); clearInterval(stillTimer); stillTimer = null; mode = "off";
  }

  function startVideo() {
    if (destroyed || paused) return;
    stopAll();
    mode = "video"; still.hidden = true; video.hidden = false;
    say("Connecting to the camera…"); state("Connecting…");
    $('[data-l="retry"]')?.setAttribute("hidden", "");
    lastProgress = Date.now();
    video.src = `/media/alarms/${alarm.id}/live.webm?t=${Date.now()}`;
    video.play().catch(() => {});
  }

  function startStills(why) {
    if (destroyed || paused) return;
    stopVideo(); clearInterval(stillTimer);
    mode = "stills"; video.hidden = true; still.hidden = false;
    say(""); state(`${why}: showing a still every ${STILL_EVERY_MS / 1000} s`);
    $('[data-l="retry"]')?.removeAttribute("hidden");
    const tick = () => { still.src = `/media/alarms/${alarm.id}/live.jpg?t=${Date.now()}`; };
    tick(); stillTimer = setInterval(tick, STILL_EVERY_MS);
  }

  video.addEventListener("playing", () => { say(""); state("Live video"); lastProgress = Date.now(); });
  video.addEventListener("timeupdate", () => {
    lastProgress = Date.now();
    const b = video.buffered;
    if (b.length && b.end(b.length - 1) - video.currentTime > MAX_BEHIND_S) video.currentTime = b.end(b.length - 1) - 0.2;
  });
  video.addEventListener("error", () => { if (mode === "video") startStills("Live video unavailable"); });
  still.addEventListener("error", () => { if (mode === "stills") state("Camera not reachable"); });
  watch = setInterval(() => {
    $(".live-clock").textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    if (mode === "video" && Date.now() - lastProgress > STALL_MS) startStills("Live video stalled");
  }, 1000);

  root.addEventListener("click", (e) => {
    const l = e.target.dataset.l;
    if (l === "retry") startVideo();
    if (l === "pause") {
      paused = !paused;
      e.target.textContent = paused ? "Resume" : "Pause";
      if (paused) { stopAll(); state("Paused"); say(""); } else startVideo();
    }
  });
  // Don't stream to a tab nobody is looking at; pick up again when it's visible.
  const onVis = () => {
    if (document.visibilityState === "hidden") { if (mode !== "off") { stopAll(); state("Paused while the tab is hidden"); } }
    else if (!paused && mode === "off") startVideo();
  };
  document.addEventListener("visibilitychange", onVis);

  if (!alarm.device_id) { say(""); state("This event has no camera"); } else startVideo();

  return {
    destroy() {
      destroyed = true;
      stopAll(); clearInterval(watch);
      document.removeEventListener("visibilitychange", onVis);
      root.innerHTML = "";
    },
  };
}
