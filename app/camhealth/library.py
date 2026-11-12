"""Camera health: each camera's library of clean frames per half hour of the day, so area checks compare 3 pm with
3 pm (sun, shadows, lights on a timer). On disk in the vision container's volume (CAMHEALTH_DIR): not in the backups,
it relearns by itself.

  <dir>/<site>/<device>/<slot 00-47>/<YYYYMMDD>_<HHMM>_<c|m>.jpg   (+ .json: people / vehicles the detector saw)

At most one frame per slot per day, the KEEP newest kept (different days: sunny and cloudy). A check compares with its
own slot and the two next to it (±30 min).

Each frame's GPU results (DINOv2 patches as float16 + ADE20K classes, ~0.65 MB) are saved beside it once computed
(`<name>.gpu.npz`), so a check runs the models on its own frame only: 2026-11-12, 35 cameras x 8 library frames
overflowed the 160-frame memory cache and every check reran both models on all 8 (6.4 s per check on the T1000).
The files are capped at GPU_CACHE_MB together, the least recently used dropped first (recomputed when needed again).
"""

from __future__ import annotations

import json
import os
import shutil
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from app.camhealth import analyze as A
from app.camhealth import ai as models

SLOTS = 48
KEEP = 4
CACHE = 160                    # decoded frames kept in memory (~1.5 MB each)
GPU_CACHE_MB = int(os.environ.get("CAMHEALTH_GPU_CACHE_MB", 4096))
GPU_TAG = f"{models.DINO}@{models.DINO_W}|{models.SEG}@{models.SEG_W}"     # other models / sizes: recomputed
GPU_SUFFIX = ".gpu.npz"


def slot(local: datetime) -> int:
    return local.hour * 2 + local.minute // 30


class Library:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.cache: OrderedDict[str, A.LibFrame] = OrderedDict()
        self.gpu_bytes: int | None = None          # on disk, counted at the first save
        self.gpu_saved = self.gpu_loaded = 0      # since start (processing panel)

    def _dir(self, site_id: int, dev: str) -> Path:
        return self.root / str(site_id) / dev

    def add(self, site_id: int, dev: str, local: datetime, f: A.Frame, objects: list) -> bool:
        d = self._dir(site_id, dev) / f"{slot(local):02d}"
        day = local.strftime("%Y%m%d")
        if d.exists() and any(p.name.startswith(day) for p in d.glob("*.jpg")):
            return False
        d.mkdir(parents=True, exist_ok=True)
        name = f"{day}_{local.strftime('%H%M')}_{'m' if f.mono else 'c'}"
        ok, buf = cv2.imencode(".jpg", f.bgr, [cv2.IMWRITE_JPEG_QUALITY, 88])
        (d / f"{name}.json").write_text(json.dumps(objects, default=float))     # first: a frame never lacks its notes
        (d / f"{name}.jpg").write_bytes(buf.tobytes())
        if f.feat is not None and f.seg is not None:                 # the check already ran the models on it
            self._save_gpu(d / f"{name}.jpg", f)
        for old in sorted(d.glob("*.jpg"), reverse=True)[KEEP:]:
            old.unlink(missing_ok=True)
            old.with_suffix(".json").unlink(missing_ok=True)
            self._drop_gpu(_gpu_path(old))
            self.cache.pop(str(old), None)
        return True

    def keep(self, frames: list[A.LibFrame]) -> int:
        """After a check: save the GPU results it computed for library frames that had none on disk yet."""
        n = 0
        for lf in frames:
            if lf.path and not lf.saved and lf.frame.feat is not None and lf.frame.seg is not None:
                lf.saved = self._save_gpu(Path(lf.path), lf.frame)
                n += lf.saved
        return n

    def _save_gpu(self, jpg: Path, f: A.Frame) -> bool:
        p = _gpu_path(jpg)
        tmp = p.with_name(p.name + ".tmp")
        try:
            with open(tmp, "wb") as fh:
                np.savez_compressed(fh, tag=np.array(GPU_TAG), feat=f.feat.astype(np.float16), seg=f.seg.astype(np.uint8))
            os.replace(tmp, p)                                       # never a half-written file
        except OSError:
            tmp.unlink(missing_ok=True)
            return False
        self.gpu_saved += 1
        self._count_gpu(p.stat().st_size)
        return True

    def _load_gpu(self, jpg: Path, f: A.Frame) -> bool:
        p = _gpu_path(jpg)
        try:
            with np.load(p) as z:
                if str(z["tag"]) != GPU_TAG:
                    return False
                f.feat, f.seg = z["feat"], z["seg"]
            os.utime(p)                                              # last used: the cap drops the oldest first
        except (OSError, ValueError, KeyError):
            return False
        self.gpu_loaded += 1
        return True

    def _drop_gpu(self, p: Path) -> None:
        try:
            size = p.stat().st_size
            p.unlink()
        except OSError:
            return
        if self.gpu_bytes is not None:
            self.gpu_bytes -= size

    def _count_gpu(self, added: int) -> None:
        if self.gpu_bytes is None:
            self.gpu_bytes = sum(p.stat().st_size for p in self.root.rglob(f"*{GPU_SUFFIX}"))
        else:
            self.gpu_bytes += added
        if self.gpu_bytes <= GPU_CACHE_MB * 1024 * 1024:
            return
        files = sorted(((p.stat().st_mtime, p) for p in self.root.rglob(f"*{GPU_SUFFIX}")), key=lambda x: x[0])
        goal = GPU_CACHE_MB * 1024 * 1024 * 0.9
        for _, p in files:
            if self.gpu_bytes <= goal:
                break
            self._drop_gpu(p)
            lf = self.cache.get(str(p.with_name(p.name[:-len(GPU_SUFFIX)] + ".jpg")))
            if lf is not None:
                lf.saved = False

    def stats(self) -> dict:
        return {"gpu_cache_mb": round((self.gpu_bytes or 0) / 1048576), "gpu_cache_cap_mb": GPU_CACHE_MB,
                "gpu_saved": self.gpu_saved, "gpu_loaded": self.gpu_loaded}

    def frames(self, site_id: int, dev: str, local: datetime, mono: bool) -> list[A.LibFrame]:
        """This slot's frames first, then the slots either side; newest first; same colour / infrared only."""
        s = slot(local)
        base = self._dir(site_id, dev)
        out = []
        for sl in (s, (s - 1) % SLOTS, (s + 1) % SLOTS):
            for p in sorted((base / f"{sl:02d}").glob(f"*_{'m' if mono else 'c'}.jpg"), reverse=True):
                lf = self._load(p)
                if lf is not None:
                    out.append(lf)
        return out[:A.LIBRARY_MAX]

    def _load(self, p: Path) -> A.LibFrame | None:
        key = str(p)
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        try:
            frame = A.load(p.read_bytes())
            objects = json.loads(p.with_suffix(".json").read_text()) if p.with_suffix(".json").exists() else []
        except (OSError, ValueError):
            return None
        lf = A.LibFrame(frame, objects, path=key, saved=self._load_gpu(p, frame))
        self.cache[key] = lf
        while len(self.cache) > CACHE:
            self.cache.popitem(last=False)
        return lf

    def slots_learned(self, site_id: int, dev: str) -> int:
        base = self._dir(site_id, dev)
        return sum(1 for d in base.glob("[0-9][0-9]") if any(d.glob("*.jpg"))) if base.exists() else 0

    def reset_if(self, site_id: int, dev: str, marker: str | None) -> bool:
        """Clear the library when the page asked for it (Accept view: settings.library_reset changed)."""
        if not marker:
            return False
        base = self._dir(site_id, dev)
        mark = base / ".reset"
        if mark.exists() and mark.read_text() == marker:
            return False
        if base.exists():
            shutil.rmtree(base)
            self.gpu_bytes = None                  # counted again at the next save
        for k in [k for k in self.cache if k.startswith(str(base) + "/")]:
            self.cache.pop(k)
        base.mkdir(parents=True, exist_ok=True)
        mark.write_text(marker)
        return True


def _gpu_path(jpg: Path) -> Path:
    return jpg.with_name(jpg.stem + GPU_SUFFIX)
