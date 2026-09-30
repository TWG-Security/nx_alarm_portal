import { api, esc, toast } from "./common.js";
import { play } from "./sound.js";

const tbody = document.getElementById("level-rows");
const saveBtn = document.getElementById("save-levels");
const forceAck = document.getElementById("force-ack");
const LABELS = { critical: "Critical", alarm: "Alarm", warning: "Warning", ignore: "Ignore" };
let saved = null;

function render(data) {
  saved = data;
  forceAck.checked = data.force_ack_critical;
  const kinds = { security: "Security", system: "System health" };
  tbody.innerHTML = data.types.map((t) => `<tr>
      <td><b>${esc(t.label)}</b><div class="muted mono">${esc(t.type)}</div></td>
      <td>${kinds[t.category] || t.category}</td>
      <td><select data-type="${t.type}" aria-label="Level for ${esc(t.label)}">
        ${Object.entries(LABELS).map(([v, l]) => `<option value="${v}" ${t.level === v ? "selected" : ""}>${l}${t.default === v ? " (default)" : ""}</option>`).join("")}
      </select></td></tr>`).join("");
  saveBtn.disabled = true;
}

function dirty() {
  const changed = [...tbody.querySelectorAll("select")].some((s) => s.value !== saved.types.find((t) => t.type === s.dataset.type).level);
  saveBtn.disabled = !(changed || forceAck.checked !== saved.force_ack_critical);
}
tbody.addEventListener("change", dirty);
forceAck.addEventListener("change", dirty);

saveBtn.addEventListener("click", async () => {
  saveBtn.disabled = true;
  const levels = Object.fromEntries([...tbody.querySelectorAll("select")].map((s) => [s.dataset.type, s.value]));
  try {
    render(await api("/api/settings/alarm-levels", { method: "PUT", body: { levels, force_ack_critical: forceAck.checked } }));
    toast("Alarm levels saved. Open alarms have been re-leveled");
  } catch (e) { toast(esc(e.message), { kind: "error" }); dirty(); }
});

document.querySelectorAll("[data-test]").forEach((b) => b.addEventListener("click", () => play(b.dataset.test, { force: true })));
render(await api("/api/settings/alarm-levels"));
