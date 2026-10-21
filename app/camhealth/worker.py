"""Camera health worker: `python -m app.camhealth.worker`.

Runs in its own container (`vision` in docker-compose.yml, with the NVIDIA card and a CPU cap) so it can never slow
the alarm pipeline: it has its own NX logins, its own small database pool, and only reads frames from NX and writes
the camera_health tables. The web app picks the changes up from the database (app/routers/camhealth.py).

Every CAMHEALTH_INTERVAL_S each camera's live frame (CAMHEALTH_FRAME_WIDTH wide) goes through analyze.py, then
state.py decides what is shown. One frame at a time per site, CAMHEALTH_PARALLEL over all sites.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, select

from app.camhealth import analyze as A
from app.camhealth import state as S
from app.camhealth.library import Library
from app.config import get_settings
from app.db import init_engine, sessionmaker
from app.models import CameraHealth, CameraHealthEvent, CameraHealthImage, Site, VisionStatus
from app.security import decrypt
from app.services.arming import zone
from app.services.sites import describe_http_error, make_client

log = logging.getLogger("portal.camhealth")

SITES_EVERY_S = float(os.environ.get("CAMHEALTH_SITES_EVERY_S", 60))   # re-read the site list (e2e: faster)
DEVICES_EVERY_S = 600        # re-read each site's cameras from NX
REQUESTS_EVERY_S = 3         # look for "Check now" / "Accept view"
TICK_S = 1.0
EVENTS_KEEP_DAYS = 90
STATUS_EVERY_S = 30          # TWG's processing panel (vision_status)
GPU_HISTORY = 240            # samples kept for the panel's charts (2 h)
VIDEO_TYPES = {"camera", "multisensorcamera", "encoder"}
OFFLINE = {"Offline", "Unauthorized", "NotDefined"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stream_aspect(dev: dict) -> float:
    """Width / height of the camera's biggest stream ("1920x1080"), 16:9 if NX doesn't say."""
    best = (0, 16 / 9)
    for st in dev.get("mediaStreams") or []:
        try:
            w, h = (int(v) for v in str(st.get("resolution", "")).split("x"))
        except ValueError:
            continue
        if w * h > best[0] and h > 0:
            best = (w * h, w / h)
    return best[1]


def is_ptz(dev: dict) -> bool:
    """NX can steer it (moved / scene checks off by default; per-camera setting). Not by name: site 2's "Driveway PTZ"
    has no PTZ control in NX and the user tests moved / scene change with it (2026-10-20)."""
    caps = str((dev.get("ptz") or {}).get("capabilities") or "none").lower()
    return caps not in ("", "none", "nocapabilities")


@dataclass
class Cam:
    site_id: int
    tenant_id: int
    device_id: str
    name: str = ""
    nx_status: str = ""
    ptz: bool = False
    aspect: float = 16 / 9                 # the camera's own (primary stream), for the live-frame resolution
    next_at: float = 0.0
    busy: bool = False
    previous_small: object = None          # the last frame (small), for the frozen check
    candidate: A.Frame | None = None       # learning: a reference is taken once two frames in a row agree
    candidate_jpeg: bytes = b""
    request_seen: object = None            # the "Check now" already acted on (one check per request, even if it fails)


@dataclass
class SiteConn:
    site: Site
    client: object
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    devices_at: float = 0.0
    key: tuple = ()


class Worker:
    def __init__(self, ai=None):
        self.cfg = get_settings()
        self.ai = ai
        self.sites: dict[int, SiteConn] = {}
        self.cams: dict[tuple[int, str], Cam] = {}
        self.sem = asyncio.Semaphore(max(1, self.cfg.camhealth_parallel))
        self.refs: dict[tuple[int, str, str], tuple[datetime, A.Frame]] = {}
        self.library = Library(self.cfg.camhealth_dir)
        self.tasks: set[asyncio.Task] = set()
        self.pass_stats = {"n": 0, "ms": 0.0, "since": time.monotonic()}
        self.started = utcnow()
        self.recent: list[dict] = []          # the last checks, newest first (processing panel)
        self.gpu_history: list[dict] = []
        self.status_dirty = False              # a check finished: republish the queue soon (not only every 30 s)

    # ---- sites and cameras -------------------------------------------------------------------------------------
    async def refresh_sites(self) -> None:
        async with sessionmaker()() as db:
            sites = (await db.scalars(select(Site).where(Site.enabled.is_(True), Site.archived_at.is_(None)))).all()
        live = set()
        for s in sites:
            live.add(s.id)
            key = (s.host, s.nx_user, s.nx_pass_enc)
            conn = self.sites.get(s.id)
            if conn is None or conn.key != key:
                conn = SiteConn(s, make_client(s.host, s.nx_user, decrypt(s.nx_pass_enc)), key=key)
                self.sites[s.id] = conn
            conn.site = s
        for sid in [k for k in self.sites if k not in live]:
            self.sites.pop(sid)
            for k in [k for k in self.cams if k[0] == sid]:
                self.cams.pop(k)

    async def refresh_devices(self, conn: SiteConn) -> None:
        site = conn.site
        try:
            devices = await conn.client.list_devices() or []
        except Exception as exc:  # noqa: BLE001 - try again next round; the cameras keep their last result
            log.warning("site %s: camera list failed: %s", site.id, describe_http_error(exc))
            conn.devices_at = time.monotonic() - DEVICES_EVERY_S + 60
            return
        conn.devices_at = time.monotonic()
        seen = {}
        for d in devices:
            if (d.get("deviceType") or "").lower() not in VIDEO_TYPES:
                continue
            dev = (d.get("id") or "").strip("{}").lower()
            if dev:
                seen[dev] = d
        async with sessionmaker()() as db:
            rows = {r.device_id: r for r in (await db.scalars(select(CameraHealth).where(CameraHealth.site_id == site.id))).all()}
            now = utcnow()
            for dev, d in seen.items():
                row = rows.get(dev)
                status = d.get("status") or ""
                if row is None:
                    row = CameraHealth(tenant_id=site.tenant_id, site_id=site.id, device_id=dev, issues=[], state={},
                                       metrics={}, settings={}, changed_at=now)
                    db.add(row)
                changed = row.name != (d.get("name") or "") or row.nx_status != status or row.tenant_id != site.tenant_id
                row.name, row.nx_status, row.tenant_id = d.get("name") or "", status, site.tenant_id
                if status in OFFLINE and row.status != "offline":
                    row.status, changed = "offline", True
                elif status not in OFFLINE and row.status == "offline":
                    row.status, changed = "pending", True
                if changed:
                    row.changed_at = now
                cam = self.cams.get((site.id, dev))
                if cam is None:
                    last = row.checked_at.replace(tzinfo=row.checked_at.tzinfo or timezone.utc) if row.checked_at else None
                    due = last + timedelta(seconds=self.cfg.camhealth_interval_s) if last else now
                    wait = max(0.0, (due - now).total_seconds())
                    cam = Cam(site.id, site.tenant_id, dev, next_at=time.monotonic() + wait + random.uniform(0, 20))
                    self.cams[(site.id, dev)] = cam
                cam.name, cam.nx_status, cam.ptz, cam.tenant_id = row.name, status, is_ptz(d), site.tenant_id
                cam.aspect = stream_aspect(d)
            if devices:                            # cameras NX no longer has: gone from the page too
                gone = [dev for dev in rows if dev not in seen]
                for dev in gone:
                    await db.execute(delete(CameraHealth).where(CameraHealth.site_id == site.id, CameraHealth.device_id == dev))
                    await db.execute(delete(CameraHealthImage).where(CameraHealthImage.site_id == site.id,
                                                                     CameraHealthImage.device_id == dev))
                    self.cams.pop((site.id, dev), None)
            await db.commit()
        log.info("site %s: %d cameras (%d offline in NX)", site.id, len(seen), sum(1 for d in seen.values()
                                                                                     if (d.get("status") or "") in OFFLINE))

    # ---- one check ---------------------------------------------------------------------------------------------
    async def fetch(self, conn: SiteConn, cam: Cam) -> bytes:
        """The live frame. Recording cameras: NX's /image (cheap, fresh). The others: one frame off the live stream,
        because /image answers NX's cached last frame for them (the same bytes for 20+ minutes, 2026-10-20)."""
        width = self.cfg.camhealth_frame_width
        last = None
        for attempt in range(2):                 # the relay answers one-off 503s
            try:
                if cam.nx_status == "Recording":
                    return await conn.client.get_thumbnail(cam.device_id, -1, width=width)
                height = int(round(width / cam.aspect / 2)) * 2
                return await conn.client.grab_live_jpeg(cam.device_id, f"{width}x{height}")
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last = exc
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                    break
                await asyncio.sleep(1)
        raise last

    async def image(self, db, site_id: int, dev: str, kind: str) -> A.Frame | None:
        """A stored reference (ref_day / ref_night) as a Frame, decoded once per version."""
        img = await db.scalar(select(CameraHealthImage).where(CameraHealthImage.site_id == site_id,
                                                              CameraHealthImage.device_id == dev,
                                                              CameraHealthImage.kind == kind))
        if img is None:
            self.refs.pop((site_id, dev, kind), None)
            return None
        hit = self.refs.get((site_id, dev, kind))
        if hit and hit[0] == img.taken_at:
            return hit[1]
        frame = await asyncio.to_thread(A.load, img.jpeg)
        self.refs[(site_id, dev, kind)] = (img.taken_at, frame)
        return frame

    async def check(self, cam: Cam) -> None:
        conn = self.sites.get(cam.site_id)
        if conn is None:
            return
        async with self.sem, conn.lock:
            t0 = time.monotonic()
            jpeg, frame, error = b"", None, ""
            try:
                jpeg = await self.fetch(conn, cam)
                if jpeg[:2] != b"\xff\xd8":
                    error = "NX answered without a picture"
                else:
                    frame = await asyncio.to_thread(A.load, jpeg)
            except Exception as exc:  # noqa: BLE001
                error = str(describe_http_error(exc))
            fetch_ms = (time.monotonic() - t0) * 1000
            n_ai = len(self.ai.timings) if self.ai is not None else 0
            summary = await self._record(conn.site, cam, jpeg, frame, error, fetch_ms) or {}
            total = (time.monotonic() - t0) * 1000
            self.pass_stats["n"] += 1
            self.pass_stats["ms"] += total
            gpu = {}
            if self.ai is not None:
                for name, ms, _ in list(self.ai.timings)[n_ai:]:
                    gpu[name] = round(gpu.get(name, 0) + ms)
            self.recent.insert(0, {"at": int(time.time() * 1000), "site_id": cam.site_id, "camera": cam.name,
                                   "fetch_ms": round(fetch_ms), "total_ms": round(total), "gpu_ms": gpu,
                                   "how": "live stream" if cam.nx_status != "Recording" else "image", **summary})
            del self.recent[50:]
            self.status_dirty = True

    async def _record(self, site: Site, cam: Cam, jpeg: bytes, frame: A.Frame | None, error: str, fetch_ms: float) -> None:
        now = utcnow()
        async with sessionmaker()() as db:
            row = await db.scalar(select(CameraHealth).where(CameraHealth.site_id == cam.site_id,
                                                             CameraHealth.device_id == cam.device_id))
            if row is None:
                return
            settings = row.settings or {}
            if settings.get("skip"):
                return
            view_checks = settings.get("view_checks", not cam.ptz)
            found: list[dict] = []
            metrics: dict = {"fetch_ms": round(fetch_ms), "view_checks": view_checks}
            learning = row.learning
            if frame is None:
                found.append(A.issue("no_picture", detail=error))
            else:
                sun = A.sun_elevation(site.lat, site.lng, now) if site.lat is not None and site.lng is not None else None
                frame.mode = A.frame_mode(frame, sun)
                local = now.astimezone(zone(site.timezone))
                if self.library.reset_if(cam.site_id, cam.device_id, settings.get("library_reset")):
                    log.info("site %s camera %s: library cleared (view accepted)", cam.site_id, cam.name)
                ref = await self.image(db, cam.site_id, cam.device_id, f"ref_{frame.mode}")
                library = await asyncio.to_thread(self.library.frames, cam.site_id, cam.device_id, local, frame.mono)
                t = time.monotonic()
                res = await asyncio.to_thread(A.analyze, frame, ref, library=library, ai=self.ai, view_checks=view_checks,
                                              lat=site.lat, lng=site.lng, now=now)
                metrics.update(res.metrics, analyze_ms=round((time.monotonic() - t) * 1000),
                               library_slots=self.library.slots_learned(cam.site_id, cam.device_id))
                found = res.issues
                if A.frozen(frame, cam.previous_small):
                    found.append(A.issue("frozen", detail="The same picture as the last check, to the pixel"))
                cam.previous_small = frame.small
                if res.clean and await asyncio.to_thread(self.library.add, cam.site_id, cam.device_id, local, frame,
                                                         [o for o in res.objects if o["label"] != "person"]):
                    metrics["library_added"] = True
                retake = settings.get("retake") or {}
                if retake.get("mode") == frame.mode and res.usable_as_reference:
                    await self._store_image(db, cam, f"ref_{frame.mode}", jpeg, now, source="user", by=retake.get("by"))
                    db.add(CameraHealthEvent(tenant_id=row.tenant_id, site_id=row.site_id, device_id=row.device_id,
                                             at=now, kind="reference", change="reference",
                                             detail={"mode": frame.mode, "source": "user", "by": retake.get("label", "")}))
                    row.settings = {k: v for k, v in settings.items() if k != "retake"}
                    found = [i for i in found if i["kind"] not in S.REFERENCE_KINDS]
                    row.state = S.forget(row.state, S.REFERENCE_KINDS)
                    log.info("site %s camera %s: %s reference retaken", cam.site_id, cam.name, frame.mode)
                    ref = True
                if ref is None:
                    await self._learn(db, row, cam, frame, jpeg, res, now)
                learning = await self.image(db, cam.site_id, cam.device_id, f"ref_{frame.mode}") is None
                await self._store_image(db, cam, "last", jpeg, now)
            issues, row.state, events = S.update(row.state, found, int(now.timestamp() * 1000))
            if row.nx_status in OFFLINE:
                status = "offline"
            else:
                status = S.status(issues)
            row.issues, row.status, row.metrics, row.learning = issues, status, metrics, learning
            row.mode = frame.mode if frame is not None else row.mode
            row.ai = self.ai is not None and frame is not None and "ai_error" not in metrics
            row.error, row.checked_at, row.changed_at = error, now, now
            asked = row.check_requested_at
            if asked is not None and asked.replace(tzinfo=asked.tzinfo or timezone.utc) <= now:
                row.check_requested_at = None
            for e in events:
                db.add(CameraHealthEvent(tenant_id=row.tenant_id, site_id=row.site_id, device_id=row.device_id, at=now,
                                         kind=e["kind"], change=e["change"],
                                         detail={k: v for k, v in e.items() if k not in ("kind", "change")}))
                log.info("site %s camera %s (%s): %s %s", cam.site_id, cam.name, cam.device_id[:8], e["change"], e["title"])
            await db.commit()
            return {"status": status, "issues": [i["title"] for i in issues], "analyze_ms": metrics.get("analyze_ms"),
                    "library": metrics.get("library"), "mode": row.mode, "error": error[:120]}

    async def _learn(self, db, row: CameraHealth, cam: Cam, frame: A.Frame, jpeg: bytes, res: A.Result, now) -> None:
        """No reference for this mode yet: keep a candidate and take it once the next frame agrees with it (so a
        person or car passing through the first frame doesn't end up in the reference)."""
        if not res.usable_as_reference:
            cam.candidate = None
            return
        cand = cam.candidate
        if cand is not None and cand.mode == frame.mode and cand.shape == frame.shape:
            agree = await asyncio.to_thread(A.analyze, frame, cand, ai=self.ai)
            if not agree.issues:
                await self._store_image(db, cam, f"ref_{frame.mode}", jpeg, now)
                db.add(CameraHealthEvent(tenant_id=row.tenant_id, site_id=row.site_id, device_id=row.device_id, at=now,
                                         kind="reference", change="reference", detail={"mode": frame.mode, "source": "auto"}))
                cam.candidate = None
                log.info("site %s camera %s: learned its %s reference", cam.site_id, cam.name, frame.mode)
                return
        cam.candidate, cam.candidate_jpeg = frame, jpeg

    async def _store_image(self, db, cam: Cam, kind: str, jpeg: bytes, now, source: str = "auto",
                           by: int | None = None) -> None:
        img = await db.scalar(select(CameraHealthImage).where(CameraHealthImage.site_id == cam.site_id,
                                                              CameraHealthImage.device_id == cam.device_id,
                                                              CameraHealthImage.kind == kind))
        if img is None:
            db.add(CameraHealthImage(site_id=cam.site_id, device_id=cam.device_id, kind=kind, jpeg=jpeg, taken_at=now,
                                     source=source, by_user_id=by))
        else:
            img.jpeg, img.taken_at, img.source, img.by_user_id = jpeg, now, source, by

    # ---- the loop ----------------------------------------------------------------------------------------------
    async def requests(self) -> None:
        """'Check now' from the page: run those cameras at once."""
        async with sessionmaker()() as db:
            rows = (await db.execute(select(CameraHealth.site_id, CameraHealth.device_id, CameraHealth.check_requested_at)
                                     .where(CameraHealth.check_requested_at.is_not(None)))).all()
        for site_id, dev, asked in rows:
            cam = self.cams.get((site_id, dev))
            if cam is not None and not cam.busy and cam.request_seen != asked:
                cam.request_seen = asked
                cam.next_at = 0.0               # (cached references follow their taken_at; a learning candidate stays)

    def _start(self, cam: Cam) -> None:
        cam.busy = True
        cam.next_at = time.monotonic() + self.cfg.camhealth_interval_s

        async def go():
            try:
                if cam.nx_status not in OFFLINE:
                    await self.check(cam)
            except Exception:  # noqa: BLE001
                log.exception("site %s camera %s: check failed", cam.site_id, cam.device_id)
            finally:
                cam.busy = False

        t = asyncio.create_task(go())
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    async def write_status(self) -> None:
        """TWG's processing panel: the card, the models' timings, the queue, the recent checks."""
        from app.camhealth import ai as ai_mod
        gpu = await asyncio.to_thread(ai_mod.gpu_stats) if self.ai is not None else {}
        if gpu and "util" in gpu:
            self.gpu_history.append({"t": int(time.time() * 1000), "util": gpu["util"], "mem": gpu["mem_used_mb"],
                                     "temp": gpu.get("temp_c")})
            del self.gpu_history[:-GPU_HISTORY]
        hour_ago = time.time() - 3600
        models = {}
        if self.ai is not None:
            for name, ms, at in list(self.ai.timings):
                if at >= hour_ago:
                    models.setdefault(name, []).append(ms)
        timing = {name: {"model": self.ai.MODELS.get(name, name), "calls": len(v), "avg_ms": round(sum(v) / len(v)),
                         "p95_ms": round(sorted(v)[int(len(v) * 0.95) - 1 if len(v) > 1 else 0])}
                  for name, v in models.items()}
        recent_hour = [r for r in self.recent if r["at"] >= hour_ago * 1000]
        now = time.monotonic()
        cams = list(self.cams.values())
        checked = [c for c in cams if c.nx_status not in OFFLINE]
        async with sessionmaker()() as db:
            off = {(sid, dev) for sid, dev, st in (await db.execute(
                select(CameraHealth.site_id, CameraHealth.device_id, CameraHealth.settings))).all() if (st or {}).get("skip")}
        wall, mono = time.time(), time.monotonic()
        queue = []
        for c in cams:
            state = ("offline" if c.nx_status in OFFLINE else "off" if (c.site_id, c.device_id) in off
                     else "checking" if c.busy else "requested" if c.next_at == 0.0 else "waiting")
            queue.append({"site_id": c.site_id, "device_id": c.device_id, "camera": c.name,
                          "site": self.sites[c.site_id].site.name if c.site_id in self.sites else "",
                          "state": state, "next_ms": int((wall + max(0.0, c.next_at - mono)) * 1000)})
        order = {"checking": 0, "requested": 1, "waiting": 2, "off": 3, "offline": 4}
        queue.sort(key=lambda q: (order[q["state"]], q["next_ms"]))
        rss = 0
        try:
            rss = int(next(line for line in open("/proc/self/status") if line.startswith("VmRSS")).split()[1]) // 1024
        except (OSError, StopIteration, ValueError):
            pass
        data = {
            "started_at": self.started.isoformat(), "interval_s": self.cfg.camhealth_interval_s,
            "parallel": self.cfg.camhealth_parallel, "ai": self.ai is not None, "gpu": gpu, "gpu_history": self.gpu_history,
            "models": timing, "cameras": len(cams), "checked_cameras": len(checked),
            "busy": [c.name for c in cams if c.busy], "due_now": sum(1 for c in checked if c.next_at <= now and not c.busy),
            "next_in_s": round(max(0.0, min((c.next_at - now for c in checked if not c.busy), default=0))),
            "checks_last_hour": len(recent_hour) if len(self.recent) < 50 else None,
            "avg_check_ms": round(sum(r["total_ms"] for r in self.recent) / len(self.recent)) if self.recent else None,
            "errors_recent": sum(1 for r in self.recent if r.get("error")), "recent": self.recent[:25], "queue": queue,
            "process": {"rss_mb": rss, "cpu_s": round(sum(os.times()[:2]))},
        }
        async with sessionmaker()() as db:
            row = await db.get(VisionStatus, 1)
            if row is None:
                db.add(VisionStatus(id=1, data=data, updated_at=utcnow()))
            else:
                row.data, row.updated_at = data, utcnow()
            await db.commit()

    async def prune(self) -> None:
        async with sessionmaker()() as db:
            await db.execute(delete(CameraHealthEvent).where(CameraHealthEvent.at < utcnow() - timedelta(days=EVENTS_KEEP_DAYS)))
            await db.commit()

    async def run(self) -> None:
        log.info("camera health worker: every %ss, %s at once, AI %s", self.cfg.camhealth_interval_s,
                 self.cfg.camhealth_parallel, "on (GPU)" if self.ai is not None else "off")
        sites_at = req_at = status_at = 0.0
        prune_at = time.monotonic()
        while True:
            now = time.monotonic()
            try:
                if now - sites_at > SITES_EVERY_S:
                    sites_at = now
                    await self.refresh_sites()
                for conn in list(self.sites.values()):
                    if now - conn.devices_at > DEVICES_EVERY_S:
                        await self.refresh_devices(conn)
                if now - req_at > REQUESTS_EVERY_S:
                    req_at = now
                    await self.requests()
                if now - status_at > STATUS_EVERY_S or (self.status_dirty and now - status_at > 3):
                    status_at, self.status_dirty = now, False
                    await self.write_status()
                if now - prune_at > 86400:
                    prune_at = now
                    await self.prune()
            except Exception:  # noqa: BLE001 - the database or NX hiccuped: try again next tick
                log.exception("camera health worker: housekeeping failed")
            for cam in list(self.cams.values()):
                if not cam.busy and cam.next_at <= time.monotonic():
                    self._start(cam)
            st = self.pass_stats
            if time.monotonic() - st["since"] > self.cfg.camhealth_interval_s and st["n"]:
                log.info("checked %d frames in the last %d s, %.0f ms each on average", st["n"],
                         time.monotonic() - st["since"], st["ms"] / st["n"])
                self.pass_stats = {"n": 0, "ms": 0.0, "since": time.monotonic()}
            await asyncio.sleep(TICK_S)


def load_ai():
    cfg = get_settings()
    if cfg.camhealth_ai == "off":
        return None
    from app.camhealth import ai
    if not ai.available():
        log.info("no GPU: CPU checks only")
        return None
    t = time.monotonic()
    model = ai.AI("cuda")
    log.info("GPU models loaded in %.1f s", time.monotonic() - t)
    return model


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        os.nice(10)                      # below the web app even inside its own CPU cap
    except OSError:
        pass
    url = get_settings().database_url
    init_engine(url, **({"pool_size": 2, "max_overflow": 2} if url.startswith("postgresql") else {}))
    ai = await asyncio.to_thread(load_ai)
    await Worker(ai).run()


if __name__ == "__main__":
    asyncio.run(main())
