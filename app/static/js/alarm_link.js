// The page a texted alarm link opens (/a/<token>): one alarm, its clip and live video, no sign-in.
import { CFG, fmtTime } from "./common.js";
import { mountPlayer } from "./player.js";

const a = CFG.alarm, token = CFG.token;
const when = document.getElementById("link-when");
when.textContent = new Date(a.event_ts_ms).toLocaleString([], { dateStyle: "medium", timeStyle: "medium" });
document.getElementById("link-expires").textContent = fmtTime(document.getElementById("link-expires").dataset.iso);
if (a.device_id) {
  mountPlayer(document.getElementById("link-player"), a, { apiBase: `/api/a/${token}`, mediaBase: `/media/a/${token}` });
}
