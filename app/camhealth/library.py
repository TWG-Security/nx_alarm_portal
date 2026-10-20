"""Camera health: each camera's library of clean frames per half hour of the day, so area checks compare 3 pm with
3 pm (sun, shadows, lights on a timer). On disk in the vision container's volume (CAMHEALTH_DIR): not in the backups,
it relearns by itself.

  <dir>/<site>/<device>/<slot 00-47>/<YYYYMMDD>_<HHMM>_<c|m>.jpg   (+ .json: people / vehicles the detector saw)

At most one frame per slot per day, the KEEP newest kept (different days: sunny and cloudy). A check compares with its
own slot and the two next to it (±30 min).
"""

from __future__ import annotations

import json
import shutil
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import cv2

from app.camhealth import analyze as A

SLOTS = 48
KEEP = 4
CACHE = 160                    # decoded frames kept in memory (~1.5 MB each)


def slot(local: datetime) -> int:
    return local.hour * 2 + local.minute // 30


class Library:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.cache: OrderedDict[str, A.LibFrame] = OrderedDict()

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
        (d / f"{name}.jpg").write_bytes(buf.tobytes())
        (d / f"{name}.json").write_text(json.dumps(objects))
        for old in sorted(d.glob("*.jpg"), reverse=True)[KEEP:]:
            old.unlink(missing_ok=True)
            old.with_suffix(".json").unlink(missing_ok=True)
            self.cache.pop(str(old), None)
        return True

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
        lf = A.LibFrame(frame, objects)
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
        for k in [k for k in self.cache if k.startswith(str(base) + "/")]:
            self.cache.pop(k)
        base.mkdir(parents=True, exist_ok=True)
        mark.write_text(marker)
        return True
