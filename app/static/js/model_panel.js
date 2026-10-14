// "Site model" panel in the 3D view (admins): upload a 3D model of the selected site (drone
// photogrammetry such as DJI Terra, CAD, ...), watch it convert, then move / turn / raise / resize it.
// The server converts in a background process (app/routers/site_models.py); this file only uploads
// in chunks and edits the placement through globe.js's model editor.
import { api, esc, toast } from "./common.js";

const ACCEPT = ".zip,.obj,.glb,.gltf,.ply,.stl";
const fmtMB = (b) => (b >= 1024 ** 3 ? `${(b / 1024 ** 3).toFixed(1)} GB` : `${Math.max(0.1, b / 1024 ** 2).toFixed(1)} MB`);
const csrf = () => document.querySelector("meta[name=csrf-token]")?.content || "";

/**
 * panel: the panel element; button: the bar button that opens it.
 * site(): the selected site (with .model); globe(): the open 3D view or null;
 * onSite(data): merge a changed site { id, model } into the page's state.
 */
export function modelPanel({ panel, button, site, globe, onSite }) {
  let open = false, upload = null, editing = null, poll = null, saveTimer = null;   // editing = { siteId, st }

  const model = () => site()?.model || null;

  function show() {
    if (!site()) { toast("Select a site first.", { kind: "warn" }); return; }
    open = true; button.classList.add("active"); panel.hidden = false;
    render();
    refresh();
  }
  function hide() {
    stopEdit();
    open = false; button.classList.remove("active"); panel.hidden = true; panel.replaceChildren();
    clearTimeout(poll);
  }

  // Server state, polled while a conversion runs (progress isn't pushed).
  async function refresh() {
    const s = site();
    if (!open || !s) return;
    clearTimeout(poll);
    try {
      const m = await api(`/api/sites/${s.id}/model`);
      onSite({ id: s.id, model: m.ready || m.job ? m : null });
      render(m.job);
      if (m.job?.status === "converting") poll = setTimeout(refresh, 1500);
    } catch (err) { panel.querySelector("[data-msg]")?.replaceChildren(err.message); }
  }

  function placementForm(r) {
    const st = editing?.st || r.placement;
    const size = r.size_m ? `${r.size_m[0]} × ${r.size_m[1]} × ${r.size_m[2]} m` : "";
    return `<div class="mp-sec">
      <div class="mp-line"><b title="${esc(r.name)}">${esc(r.name)}</b></div>
      <div class="mp-line muted">${fmtMB(r.bytes || 0)}${r.faces ? ` · ${r.faces.toLocaleString()} faces` : ""}${size ? ` · ${size}` : ""}</div>
      <div class="mp-line muted">${r.georef ? `Positioned from ${esc(r.georef)}` : "Placed by hand"}</div>
    </div>
    <div class="mp-sec">
      ${editing ? `<div class="mp-hint">Drag the <b>white dot</b> to move the model, the <b>orange dot</b> on the ring to turn it. Changes save as you go.</div>`
                : '<button type="button" class="go" data-mp="edit">Move / turn</button>'}
      <div class="mp-nums">
        <label>Turn <input type="number" data-mpn="heading" step="0.5" value="${(+st.heading).toFixed(1)}">°</label>
        <span class="mp-nudge"><button type="button" data-nudge="heading" data-by="-1" title="Turn left 1°">⟲</button><button type="button" data-nudge="heading" data-by="1" title="Turn right 1°">⟳</button></span>
        <label>Height <input type="number" data-mpn="height" step="0.1" value="${(+st.height).toFixed(2)}"> m</label>
        <span class="mp-nudge"><button type="button" data-nudge="height" data-by="-0.25" title="Down 25 cm">▼</button><button type="button" data-nudge="height" data-by="0.25" title="Up 25 cm">▲</button></span>
        <label>Size <input type="number" data-mpn="scale" step="1" min="1" value="${Math.round(st.scale * 1000) / 10}"> %</label>
      </div>
      <div class="mp-save" data-save></div>
      <div class="mp-row">
        ${editing ? '<button type="button" data-mp="done">Done</button>' : ""}
        <button type="button" data-mp="fly">Fly to model</button>
        <button type="button" data-mp="reset" title="Back to where the file put it">Reset position</button>
      </div>
    </div>
    <div class="mp-sec mp-row">
      ${r.kind === "mesh" && r.textured !== false && !r.lod ? `<button type="button" class="go" data-mp="rebuild" data-up="${r.up || "auto"}" title="Convert it again with levels of detail: far views load a light copy, close-ups load only what's near">Rebuild for smoother 3D</button>` : ""}
      ${r.kind === "mesh" ? `<button type="button" data-mp="rebuild" data-up="${r.up === "y" ? "z" : "y"}" title="Convert it again with the other axis pointing up">Lying on its side? Rebuild ${r.up === "y" ? "Z" : "Y"}-up</button>` : ""}
      <button type="button" data-mp="replace">Replace…</button>
      <button type="button" class="danger" data-mp="delete">Delete model</button>
    </div>`;
  }

  function uploadForm(replace) {
    return `<div class="mp-sec">
      ${replace ? '<div class="mp-line"><b>Replace the model</b> <button type="button" data-mp="keep">Cancel</button></div>' : ""}
      <label class="mp-drop" data-drop><input type="file" accept="${ACCEPT}" data-file hidden>
        <b>Choose a 3D model</b> or drop it here</label>
      <div class="mp-hint">
        <b>DJI Terra:</b> export <i>OBJ</i> (or <i>B3DM</i>), then zip the whole output folder, including <code>metadata.xml</code>, so it lands in the right spot by itself.<br>
        Also: 3D Tiles (a folder with <code>tileset.json</code>), glTF / GLB, OBJ with its .mtl and textures (zipped), PLY and STL meshes. Up to 3 GB.
      </div>
      <details class="mp-adv"><summary>Up axis</summary>
        <label><input type="radio" name="mp-up" value="auto" checked> Automatic</label>
        <label><input type="radio" name="mp-up" value="y"> Y is up (Blender, SketchUp, most OBJ)</label>
        <label><input type="radio" name="mp-up" value="z"> Z is up (drone / survey / CAD)</label></details>
    </div>`;
  }

  let replacing = false;
  function render(job = model()?.job) {
    if (!open) return;
    const s = site(), m = model(), r = m?.ready;
    if (!s) { hide(); return; }
    let body = "";
    if (upload) {
      const pct = Math.floor((100 * upload.sent) / upload.size);
      body = `<div class="mp-sec"><div class="mp-line"><b>Uploading ${esc(upload.name)}</b></div>
        <div class="mp-bar"><i style="width:${pct}%"></i></div>
        <div class="mp-line muted">${fmtMB(upload.sent)} of ${fmtMB(upload.size)} (${pct}%)${upload.rate ? ` · ${fmtMB(upload.rate)}/s` : ""}</div>
        <button type="button" data-mp="cancel">Cancel</button></div>`;
    } else if (job?.status === "converting" || job?.status === "uploading") {
      body = `<div class="mp-sec"><div class="mp-line"><b>Converting ${esc(job.name)}</b></div>
        <div class="mp-bar"><i style="width:${job.pct || 0}%"></i></div>
        <div class="mp-line muted">${esc(job.step || (job.status === "uploading" ? "Uploading (another browser)" : "Starting"))}</div>
        <div class="mp-hint">This runs in the background at low priority; alarms aren't affected. You can close this panel.</div></div>`;
    } else if (job?.status === "failed") {
      body = `<div class="mp-sec mp-error"><b>${esc(job.name)} couldn't be used</b><div>${esc(job.error || "")}</div>
        <div class="mp-row">${job.retry ? '<button type="button" class="go" data-mp="retry" title="Convert the same upload again">Try again</button>' : ""}
        <button type="button" data-mp="dismiss">${job.retry ? "Discard" : "OK"}</button></div></div>`;
    }
    if (!upload && job?.status !== "converting" && job?.status !== "uploading") {
      body += r && !replacing ? placementForm(r) : uploadForm(!!r);
    } else if (r) body += placementForm(r);
    panel.innerHTML = `<div class="hint">3D model · ${esc(s.name)}</div>${body}<div class="mp-line muted" data-msg></div>`;
  }

  // ---- placement editing
  function startEdit() {
    const s = site(), r = model()?.ready, g = globe();
    if (!s || !r || !g) return;
    editing = { siteId: s.id, st: { ...r.placement } };
    g.editModel(s.id, r.placement, {
      onChange: (st) => { if (editing) { editing.st = st; paintNums(st); } },
      onCommit: (p) => save(s.id, p),
    });
    render();
  }
  function stopEdit() {
    if (!editing) return;
    editing = null;
    globe()?.stopEditing();
    render();
  }
  function paintNums(st) {
    for (const [k, v] of [["heading", (+st.heading).toFixed(1)], ["height", (+st.height).toFixed(2)], ["scale", Math.round(st.scale * 1000) / 10]]) {
      const el = panel.querySelector(`[data-mpn="${k}"]`);
      if (el && document.activeElement !== el) el.value = v;
    }
  }
  const say = (t) => { const el = panel.querySelector("[data-save]"); if (el) el.textContent = t; };
  function save(siteId, p) {
    say("Saving…");
    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
      try {
        const m = await api(`/api/sites/${siteId}/model/placement`, { method: "PUT", body: p });
        onSite({ id: siteId, model: m });
        say("Saved ✓");
      } catch (err) { say(`Not saved: ${err.message}`); toast(esc(err.message), { kind: "error" }); }
    }, 350);
  }
  // Numbers and nudges: through the editor when it's on (so the model moves at once), else saved directly.
  function setValues(values) {
    const s = site(), r = model()?.ready;
    if (!s || !r) return;
    const ed = editing && globe()?.currentEditor();
    if (ed?.set) { ed.set(values); return; }
    const p = { ...r.placement, ...values };
    onSite({ id: s.id, model: { ...model(), ready: { ...r, placement: p } } });
    save(s.id, p);
  }

  // ---- chunked upload (Cloudflare limits one request to 100 MB; a failed chunk is retried)
  async function send(file) {
    const s = site();
    if (!s || upload) return;
    const up = panel.querySelector('input[name="mp-up"]:checked')?.value || "auto";
    let start;
    try {
      start = await api(`/api/sites/${s.id}/model/uploads`, { method: "POST", body: { name: file.name, size: file.size, up } });
    } catch (err) { toast(esc(err.message), { kind: "error", timeout: 10000 }); return; }
    stopEdit();
    replacing = false;
    upload = { siteId: s.id, id: start.upload_id, name: file.name, size: file.size, sent: 0, rate: 0, ctl: new AbortController() };
    const t0 = performance.now();
    render();
    const base = `/api/sites/${s.id}/model/uploads/${start.upload_id}`;
    try {
      while (upload.sent < file.size) {
        const end = Math.min(file.size, upload.sent + start.chunk_bytes);
        let tries = 0;
        for (;;) {
          const r = await fetch(`${base}?offset=${upload.sent}`, { method: "PUT", body: file.slice(upload.sent, end), signal: upload.ctl.signal,
            headers: { "X-CSRF-Token": csrf(), "Content-Type": "application/octet-stream" }, credentials: "same-origin" }).catch((e) => e);
          if (r instanceof Response && r.ok) { upload.sent = (await r.json()).received; break; }
          if (r instanceof Response && r.status === 409) {
            const d = (await r.json().catch(() => ({}))).detail;
            if (d?.received != null) { upload.sent = d.received; break; }
          }
          if (r?.name === "AbortError") throw r;
          if (r instanceof Response && r.status < 500 && r.status !== 409) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
          if (++tries >= 4) throw new Error("The upload kept failing. Check the connection and try again.");
          await new Promise((ok) => setTimeout(ok, 1000 * tries));
        }
        upload.rate = upload.sent / ((performance.now() - t0) / 1000);
        render();
      }
      upload = null;
      toast("Uploaded. Converting the model…");
      refresh();
    } catch (err) {
      const cancelled = err?.name === "AbortError";
      upload = null;
      if (cancelled) await api(base, { method: "DELETE" }).catch(() => {});
      else toast(`Upload failed: ${esc(err.message)}`, { kind: "error", timeout: 10000 });
      refresh();
    }
  }

  panel.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-mp], [data-nudge]");
    if (!b) return;
    const s = site(), r = model()?.ready;
    if (b.dataset.nudge) {
      const k = b.dataset.nudge, cur = (editing?.st || r?.placement || {})[k] || 0;
      return setValues({ [k]: k === "heading" ? (((cur + Number(b.dataset.by)) % 360) + 360) % 360 : Math.round((cur + Number(b.dataset.by)) * 100) / 100 });
    }
    const act = b.dataset.mp;
    if (act === "edit") startEdit();
    else if (act === "done") stopEdit();
    else if (act === "fly") globe()?.flyToModel(s.id);
    else if (act === "reset" && r) setValues({ ...r.origin });
    else if (act === "cancel") upload?.ctl.abort();
    else if (act === "replace") { stopEdit(); replacing = true; render(); }
    else if (act === "keep") { replacing = false; render(); }
    else if (act === "dismiss") {
      await api(`/api/sites/${s.id}/model?job_only=1`, { method: "DELETE" }).catch(() => {});
      refresh();
    } else if (act === "retry") {
      try { onSite({ id: s.id, model: await api(`/api/sites/${s.id}/model/retry`, { method: "POST" }) }); }
      catch (err) { toast(esc(err.message), { kind: "error" }); }
      refresh();
    } else if (act === "rebuild") {
      stopEdit();
      try { onSite({ id: s.id, model: await api(`/api/sites/${s.id}/model/rebuild`, { method: "POST", body: { up: b.dataset.up } }) }); }
      catch (err) { toast(esc(err.message), { kind: "error" }); }
      refresh();
    } else if (act === "delete") {
      if (!confirm(`Delete the 3D model of ${s.name}? It's removed for everyone.`)) return;
      stopEdit();
      try { await api(`/api/sites/${s.id}/model`, { method: "DELETE" }); onSite({ id: s.id, model: null }); toast("Model deleted"); }
      catch (err) { toast(esc(err.message), { kind: "error" }); }
      render();
    }
  });
  panel.addEventListener("change", (e) => {
    if (e.target.matches("[data-file]") && e.target.files[0]) return send(e.target.files[0]);
    const k = e.target.dataset?.mpn;
    if (!k) return;
    const v = Number(e.target.value);
    if (!Number.isFinite(v)) return;
    setValues({ [k]: k === "scale" ? Math.max(0.01, v / 100) : k === "heading" ? ((v % 360) + 360) % 360 : v });
  });
  panel.addEventListener("dragover", (e) => { if (e.target.closest("[data-drop]")) { e.preventDefault(); e.target.closest("[data-drop]").classList.add("over"); } });
  panel.addEventListener("dragleave", (e) => e.target.closest?.("[data-drop]")?.classList.remove("over"));
  panel.addEventListener("drop", (e) => {
    const zone = e.target.closest("[data-drop]");
    if (!zone) return;
    e.preventDefault(); zone.classList.remove("over");
    if (e.dataTransfer.files[0]) send(e.dataTransfer.files[0]);
  });

  return {
    toggle: () => (open ? hide() : show()),
    hide,
    get open() { return open; },
    get editing() { return !!editing; },
    stopEdit,
    siteChanged: () => { if (open && !upload) { if (editing && editing.siteId !== site()?.id) stopEdit(); render(); } },
  };
}
