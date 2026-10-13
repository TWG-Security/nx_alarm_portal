"""3D models of a site (drone photogrammetry, CAD, ...) shown in the 3D view.

Admins upload from the 3D view's "Site model" panel in chunks (big files; Cloudflare caps one request at
100 MB). The finished upload is converted by app/services/model_build.py in a separate process with
low CPU/IO priority, a memory cap and a time limit, one at a time, so it can't slow the portal's alarm
handling. The result is a 3D Tiles set served from /media/sites/<id>/model/<version>/...

sites.model (JSON):
  ready: the model on show {version, tileset, name, bytes, kind, pieces, faces, up, size_m, georef, anchor,
         origin (where the file says it goes), placement {lat, lng, height, heading, scale}, uploaded_*}
  job:   an upload or conversion in progress or failed {status, version, name, bytes, error, started_at, by}
  next_version: folder numbers are never reused (browsers cache the files for good)
A new model replaces the old one only once it's converted; until then the old one stays on show.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import resource
import secrets
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.config import get_settings
from app.db import get_db, sessionmaker
from app.deps import client_ip, csrf_protect, require_admin
from app.models import Site, User
from app.scope import Scope, get_scope
from app.services.bus import bus

log = logging.getLogger("portal.models")
router = APIRouter(dependencies=[Depends(csrf_protect)])

UPLOAD_EXTS = {".zip", ".obj", ".glb", ".gltf", ".ply", ".stl"}
CHUNK_BYTES = 8 * 1024 * 1024            # the browser sends this much per request
CHUNK_MAX = 16 * 1024 * 1024
# Only these are ever served from a model folder (an uploaded zip could hold anything, e.g. HTML).
SERVE_TYPES = {".json": "application/json", ".b3dm": "application/octet-stream", ".i3dm": "application/octet-stream",
               ".pnts": "application/octet-stream", ".cmpt": "application/octet-stream", ".subtree": "application/octet-stream",
               ".glb": "model/gltf-binary", ".gltf": "model/gltf+json", ".bin": "application/octet-stream",
               ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".ktx2": "image/ktx2"}

FAILED_KEEP_DAYS = 3                     # a failed conversion's files are kept this long for "Try again"
_uploads: dict[str, dict] = {}           # upload id -> {site_id, version, path, size, received, name, up, user_id}
_build_slot = asyncio.Semaphore(1)       # one conversion at a time on this 2-CPU server
_tasks: set[asyncio.Task] = set()


def root() -> Path:
    return Path(get_settings().model_dir)


def job_dir(site_id: int, version: int) -> Path:
    return root() / str(site_id) / str(version)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def public(model: dict | None) -> dict | None:
    """What pages get (site_dict "model")."""
    if not model or not (model.get("ready") or model.get("job")):
        return None
    ready = model.get("ready")
    if ready:
        ready = {**ready, "url": f"/media/sites/{model['site_id']}/model/{ready['version']}/{ready['tileset']}"}
    return {"ready": ready, "job": model.get("job")}


async def _publish(site: Site) -> None:
    bus.publish(site.tenant_id, "site.updated", {"id": site.id, "model": public(site.model)})


def _set(site: Site, **parts) -> dict:
    model = {**(site.model or {}), "site_id": site.id, **parts}
    site.model = model                       # a new dict, so SQLAlchemy sees the JSON change
    return model


async def _site(db: AsyncSession, scope: Scope, site_id: int, write: bool = False) -> Site:
    site = await db.scalar(select(Site).where(Site.id == site_id, scope.where(Site.tenant_id), Site.archived_at.is_(None)))
    if site is None:
        raise HTTPException(404, "Site not found")
    if write:
        scope.require_write(site.tenant_id)
    return site


def _rmtree_later(path: Path) -> None:
    t = asyncio.create_task(asyncio.to_thread(shutil.rmtree, path, True))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


# ------------------------------------------------------------------------------------------- conversion

def _limits() -> None:                   # runs in the child before exec
    mb = get_settings().model_build_mem_mb
    resource.setrlimit(resource.RLIMIT_AS, (mb * 1024 * 1024, mb * 1024 * 1024))
    os.nice(19)


async def _convert(site_id: int, version: int) -> None:
    """Run the converter for one job, then publish the model (or the error)."""
    job = job_dir(site_id, version)
    settings = get_settings()
    async with _build_slot:
        t0 = time.monotonic()
        cmd = [sys.executable, "-m", "app.services.model_build", str(job)]
        if shutil.which("ionice"):
            cmd = ["ionice", "-c", "3", *cmd]                # idle disk priority
        env = {**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
        result = None
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, preexec_fn=_limits, env=env, cwd=os.getcwd(),
                                                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            try:
                _, err = await asyncio.wait_for(proc.communicate(), settings.model_build_max_s)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                result = {"ok": False, "error": f"Conversion took longer than {settings.model_build_max_s // 60} minutes. "
                                                "Export it tiled (DJI Terra: OBJ tiles or B3DM) or with lower detail"}
            else:
                try:
                    result = json.loads((job / "result.json").read_text())
                except (OSError, ValueError):
                    tail = (err or b"").decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
                    result = {"ok": False, "error": "The converter stopped (likely out of memory: export the model tiled "
                                                    f"or with lower detail). {tail[0][:200]}"}
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "error": f"Couldn't start the converter: {e}"}
        log.info("site %s model v%s: %s in %.1f s", site_id, version, "converted" if result.get("ok") else result.get("error"),
                 time.monotonic() - t0)
    await _finish(site_id, version, result)


async def _finish(site_id: int, version: int, result: dict) -> None:
    async with sessionmaker()() as db:
        site = await db.get(Site, site_id)
        if site is None or (site.model or {}).get("job", {}).get("version") != version:
            shutil.rmtree(job_dir(site_id, version), ignore_errors=True)     # cancelled or replaced meanwhile
            return
        job = site.model["job"]
        if not result.get("ok"):
            # The files stay (FAILED_KEEP_DAYS) so it can be tried again, e.g. after a converter fix, without re-uploading.
            _set(site, job={**job, "status": "failed", "error": result.get("error", "Conversion failed"),
                            "retry": job_dir(site_id, version).is_dir(), "failed_at": _now()})
            audit(db, site.tenant_id, "site.model_failed", user_id=job.get("by"), site_id=site.id, name=job.get("name"),
                  error=result.get("error"))
        else:
            old = (site.model or {}).get("ready")
            geo = result.get("georef") or {}
            if result.get("kind") == "mesh" and "lat" in geo:
                origin = {"lat": geo["lat"], "lng": geo["lng"], "height": 0.0, "heading": geo["heading"], "scale": geo["scale"]}
            elif result.get("anchor"):
                a = result["anchor"]
                origin = {"lat": a["lat"], "lng": a["lng"], "height": 0.0, "heading": 0.0, "scale": 1.0}
            else:
                origin = {"lat": site.lat or 0.0, "lng": site.lng or 0.0, "height": 0.0, "heading": 0.0, "scale": 1.0}
            keep = old and job.get("rebuild_of") == old.get("version")          # a rebuild keeps where it was put
            ready = {"version": version, "tileset": result["tileset"], "name": job.get("name"), "bytes": result.get("bytes"),
                     "kind": result["kind"], "pieces": result.get("pieces"), "faces": result.get("faces"), "up": result.get("up"),
                     "size_m": result.get("size_m"), "georef": geo.get("label"), "anchor": result.get("anchor"),
                     "origin": origin, "placement": dict(old["placement"]) if keep else dict(origin),
                     "uploaded_at": job.get("started_at"), "uploaded_by": job.get("by_name")}
            _set(site, ready=ready, job=None)
            if old:
                _rmtree_later(job_dir(site_id, old["version"]))
            audit(db, site.tenant_id, "site.model_ready", user_id=job.get("by"), site_id=site.id, name=job.get("name"),
                  bytes=result.get("bytes"), georef=geo.get("label"))
        await db.commit()
        await _publish(site)


def _spawn(site_id: int, version: int) -> None:
    t = asyncio.create_task(_convert(site_id, version), name=f"model-{site_id}-{version}")
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def resume() -> None:
    """At start-up: conversions cut off by a restart run again; cut-off uploads are failed."""
    async with sessionmaker()() as db:
        for site in (await db.scalars(select(Site).where(Site.model.is_not(None)))).all():
            job = (site.model or {}).get("job") or {}
            if job.get("status") == "converting" and job_dir(site.id, job["version"]).is_dir():
                _spawn(site.id, job["version"])
            elif job.get("status") in ("uploading", "converting"):
                _set(site, job={**job, "status": "failed", "error": "The upload was cut off by a portal restart. Upload it again."})
            elif job.get("status") == "failed" and job.get("retry"):
                failed = datetime.fromisoformat(job.get("failed_at") or job.get("started_at") or _now())
                if datetime.now(timezone.utc) - failed > timedelta(days=FAILED_KEEP_DAYS):
                    _rmtree_later(job_dir(site.id, job["version"]))
                    _set(site, job={**job, "retry": False})
        await db.commit()


async def shutdown() -> None:
    for t in list(_tasks):
        t.cancel()


# ------------------------------------------------------------------------------------------- API

class UploadIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    up: str = Field(default="auto", pattern="^(auto|y|z)$")


@router.post("/api/sites/{site_id}/model/uploads")
async def start_upload(site_id: int, body: UploadIn, request: Request, user: User = Depends(require_admin),
                       scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    site = await _site(db, scope, site_id, write=True)
    ext = Path(body.name).suffix.lower()
    if ext not in UPLOAD_EXTS:
        raise HTTPException(400, "Upload a .zip (3D Tiles, OBJ with its .mtl and textures, glTF), or a single .glb, .obj, .ply or .stl")
    if body.size > settings.model_max_mb * 1024 * 1024:
        raise HTTPException(400, f"That file is {body.size / 1024**2:,.0f} MB; the limit is {settings.model_max_mb:,} MB")
    job = (site.model or {}).get("job") or {}
    if job.get("status") in ("uploading", "converting"):
        raise HTTPException(409, "A model is already being uploaded or converted for this site")
    if job.get("status") == "failed":                     # a new upload replaces the failed one's kept files
        _rmtree_later(job_dir(site.id, job["version"]))
    root().mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(root()).free
    if free < body.size * 3 + 1024**3:
        raise HTTPException(507, f"Not enough disk space on the server ({free / 1024**3:.1f} GB free)")
    version = int((site.model or {}).get("next_version") or 1)
    path = job_dir(site.id, version)
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    (path / "job.json").write_text(json.dumps({"name": body.name, "up": body.up}))
    uid = secrets.token_urlsafe(16)
    _uploads[uid] = {"site_id": site.id, "version": version, "path": path / f"upload{ext}.part", "size": body.size,
                     "received": 0, "ext": ext, "user_id": user.id}
    _uploads[uid]["path"].touch()
    _set(site, next_version=version + 1, job={"status": "uploading", "version": version, "name": body.name, "bytes": body.size,
                                             "up": body.up, "started_at": _now(), "by": user.id, "by_name": user.label})
    audit(db, site.tenant_id, "site.model_upload", user_id=user.id, site_id=site.id, ip=client_ip(request),
          name=body.name, bytes=body.size)
    await db.commit()
    await _publish(site)
    return {"upload_id": uid, "chunk_bytes": CHUNK_BYTES}


def _upload(uid: str, site_id: int, user: User) -> dict:
    up = _uploads.get(uid)
    if up is None or up["site_id"] != site_id or up["user_id"] != user.id:
        raise HTTPException(404, "That upload has ended. Start it again.")
    return up


@router.put("/api/sites/{site_id}/model/uploads/{uid}")
async def upload_chunk(site_id: int, uid: str, offset: int, request: Request, user: User = Depends(require_admin),
                       scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    up = _upload(uid, site_id, user)
    site = await _site(db, scope, site_id, write=True)
    await db.commit()                                    # don't hold a DB connection while bytes arrive
    if offset != up["received"]:
        raise HTTPException(409, {"message": "Out of step", "received": up["received"]})
    n = 0
    with open(up["path"], "ab") as f:
        async for part in request.stream():
            n += len(part)
            if n > CHUNK_MAX or up["received"] + n > up["size"]:
                f.truncate(up["received"])
                raise HTTPException(413, "Chunk too big")
            f.write(part)
    up["received"] += n
    if up["received"] < up["size"]:
        return {"received": up["received"], "done": False}
    _uploads.pop(uid, None)
    up["path"].rename(up["path"].with_suffix(""))        # upload.zip.part -> upload.zip
    site = await _site(db, scope, site_id)
    job = (site.model or {}).get("job") or {}
    if job.get("version") != up["version"]:
        shutil.rmtree(job_dir(site_id, up["version"]), ignore_errors=True)
        raise HTTPException(409, "This upload was cancelled")
    _set(site, job={**job, "status": "converting"})
    await db.commit()
    await _publish(site)
    _spawn(site_id, up["version"])
    return {"received": up["received"], "done": True}


@router.delete("/api/sites/{site_id}/model/uploads/{uid}")
async def cancel_upload(site_id: int, uid: str, user: User = Depends(require_admin), scope: Scope = Depends(get_scope),
                        db: AsyncSession = Depends(get_db)):
    up = _upload(uid, site_id, user)
    site = await _site(db, scope, site_id, write=True)
    _uploads.pop(uid, None)
    if ((site.model or {}).get("job") or {}).get("version") == up["version"]:
        _set(site, job=None)
    await db.commit()
    _rmtree_later(job_dir(site_id, up["version"]))
    await _publish(site)
    return {"ok": True}


@router.get("/api/sites/{site_id}/model")
async def get_model(site_id: int, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    site = await _site(db, scope, site_id)
    out = public(site.model) or {"ready": None, "job": None}
    job = out.get("job")
    if job and job.get("status") == "converting":
        try:
            st = json.loads((job_dir(site.id, job["version"]) / "status.json").read_text())
            out = {**out, "job": {**job, "step": st.get("step"), "pct": st.get("pct")}}
        except (OSError, ValueError):
            out = {**out, "job": {**job, "step": "Waiting to start", "pct": 0}}
    return out


class PlacementIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    height: float = Field(ge=-1000, le=1000)
    heading: float
    scale: float = Field(gt=0.001, le=1000)


@router.put("/api/sites/{site_id}/model/placement")
async def place_model(site_id: int, body: PlacementIn, request: Request, user: User = Depends(require_admin),
                      scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    site = await _site(db, scope, site_id, write=True)
    ready = (site.model or {}).get("ready")
    if not ready:
        raise HTTPException(404, "This site has no 3D model")
    placement = {"lat": round(body.lat, 8), "lng": round(body.lng, 8), "height": round(body.height, 2),
                 "heading": round(body.heading % 360, 2), "scale": round(body.scale, 5)}
    _set(site, ready={**ready, "placement": placement})
    audit(db, site.tenant_id, "site.model_placed", user_id=user.id, site_id=site.id, ip=client_ip(request), **placement)
    await db.commit()
    await _publish(site)
    return public(site.model)


@router.post("/api/sites/{site_id}/model/retry")
async def retry_model(site_id: int, request: Request, user: User = Depends(require_admin),
                      scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Convert a failed upload again from its kept files."""
    site = await _site(db, scope, site_id, write=True)
    job = (site.model or {}).get("job") or {}
    if job.get("status") != "failed" or not job.get("retry") or not job_dir(site.id, job["version"]).is_dir():
        raise HTTPException(400, "There's no failed upload to try again. Upload the model again.")
    for f in ("result.json", "status.json"):
        (job_dir(site.id, job["version"]) / f).unlink(missing_ok=True)
    _set(site, job={k: v for k, v in job.items() if k not in ("error", "retry", "failed_at")} | {"status": "converting", "by": user.id,
                                                                                            "by_name": user.label})
    audit(db, site.tenant_id, "site.model_retry", user_id=user.id, site_id=site.id, ip=client_ip(request), name=job.get("name"))
    await db.commit()
    await _publish(site)
    _spawn(site.id, job["version"])
    return public(site.model)


class RebuildIn(BaseModel):
    up: str = Field(pattern="^(auto|y|z)$")


@router.post("/api/sites/{site_id}/model/rebuild")
async def rebuild_model(site_id: int, body: RebuildIn, request: Request, user: User = Depends(require_admin),
                        scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Convert the current model again (e.g. with the other up axis when it came out lying on its side)."""
    site = await _site(db, scope, site_id, write=True)
    model = site.model or {}
    ready = model.get("ready")
    if not ready or ready.get("kind") != "mesh":
        raise HTTPException(400, "Only converted models (OBJ, glTF, PLY, STL) can be rebuilt")
    if (model.get("job") or {}).get("status") in ("uploading", "converting"):
        raise HTTPException(409, "A model is already being uploaded or converted for this site")
    version = int(model.get("next_version") or ready["version"] + 1)
    src, dest = job_dir(site.id, ready["version"]) / "src", job_dir(site.id, version)
    shutil.rmtree(dest, ignore_errors=True)
    await asyncio.to_thread(shutil.copytree, src, dest / "src", copy_function=os.link)
    (dest / "job.json").write_text(json.dumps({"name": ready["name"], "up": body.up}))
    _set(site, next_version=version + 1, job={"status": "converting", "version": version, "name": ready["name"],
                                             "bytes": ready.get("bytes"), "up": body.up, "started_at": _now(), "by": user.id,
                                             "by_name": user.label, "rebuild_of": ready["version"]})
    audit(db, site.tenant_id, "site.model_rebuild", user_id=user.id, site_id=site.id, ip=client_ip(request), up=body.up)
    await db.commit()
    await _publish(site)
    _spawn(site.id, version)
    return public(site.model)


@router.delete("/api/sites/{site_id}/model")
async def delete_model(site_id: int, request: Request, job_only: bool = False, user: User = Depends(require_admin),
                       scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Remove the model on show and clear a failed job (an upload or conversion in progress is left alone).
    job_only: just clear a failed job's message."""
    site = await _site(db, scope, site_id, write=True)
    model = site.model or {}
    ready, job = model.get("ready"), model.get("job") or {}
    if job_only:
        if job.get("status") == "failed":
            _rmtree_later(job_dir(site.id, job["version"]))
            _set(site, job=None)
            await db.commit()
            await _publish(site)
        return {"ok": True}
    if not ready and job.get("status") != "failed":
        raise HTTPException(404, "This site has no 3D model")
    _set(site, ready=None, job=job if job.get("status") in ("uploading", "converting") else None)
    if ready:
        _rmtree_later(job_dir(site.id, ready["version"]))
        audit(db, site.tenant_id, "site.model_removed", user_id=user.id, site_id=site.id, ip=client_ip(request), name=ready.get("name"))
    await db.commit()
    await _publish(site)
    return {"ok": True}


@router.get("/media/sites/{site_id}/model/{version}/{path:path}")
async def model_file(site_id: int, version: int, path: str, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    site = await _site(db, scope, site_id)
    ready = (site.model or {}).get("ready") or {}
    await db.commit()
    if ready.get("version") != version or Path(path).suffix.lower() not in SERVE_TYPES:
        raise HTTPException(404)
    base = job_dir(site_id, version).resolve()
    file = (base / path).resolve()
    if not file.is_relative_to(base) or not file.is_file() or file.name in ("job.json", "result.json", "status.json"):
        raise HTTPException(404)
    return FileResponse(file, media_type=SERVE_TYPES[file.suffix.lower()],
                        headers={"Cache-Control": "private, max-age=31536000, immutable"})
