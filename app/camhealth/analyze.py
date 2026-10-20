"""Camera health: the checks on one frame. Pure functions (OpenCV + numpy, no I/O); worker.py feeds them frames and
state.py decides what is shown.

Everything runs on a copy W px wide, compared with the camera's reference picture for the same mode (day = colour,
night = infrared black-and-white) after lining the reference up with the frame:

  moved          the reference no longer lines up: shifted, turned or zoomed past the limits (ORB + RANSAC)
  view_changed   nothing lines up any more (camera turned away, scene replaced)
  covered / blocked   cells that had detail and are now flat (whole view / an area)
  dark           cells that were lit and are now black (whole view = problem, an area = warning)
  glare          cells blown out to white that weren't
  blur           cells that lost their fine detail but kept their shapes (out of focus, smudge, water)
  scene_change   cells that differ from the camera's running background (lighting-proof correlation, best of
                 small shifts; on the GPU DINOv2 patch features must agree and people / vehicles never count),
                 joined into boxes. The background follows slow drift (sun, shadows): after each check the cells that
                 did NOT change are copied into it (`Result.absorb`), so a real change stays flagged until someone
                 accepts the view, while the afternoon sun never adds up.
  frozen         the very same picture as the last check (cameras always have some sensor noise)
  ir_day         infrared black-and-white while the sun is up at the site

Boxes are [x, y, w, h] in 0..1 of the frame, so the browser draws them over the stored picture at any size.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import cv2
import numpy as np

W = 960                    # analysis width (px)
CELL = 40                  # grid cell (px at W): 24 x 13-14 cells
MONO_CHROMA = 2.5          # mean |a|+|b| (Lab) below this = black-and-white picture (infrared)

# Per-cell limits (8-bit grey levels unless noted)
TEXTURED_STD = 10          # a reference cell with this much contrast "has detail"
FLAT_STD = 4.0             # ... and below this it's flat now (covered / blocked)
DARK_MEAN, LIT_MEAN = 22, 50
SAT_NOW, SAT_REF = 0.5, 0.15   # share of blown-out pixels in a glare cell now / in the reference
BLUR_RATIO = 0.35          # fine detail left vs the reference
BLUR_SHAPE_NCC = 0.6       # ... while the coarse shapes still match (else it's a change, not blur)
CHANGE_NCC = 0.35          # correlation below this = the cell's picture changed
SHIFTS = (-4, 0, 4)        # px: best correlation over small shifts (wind, vibration, JPEG)
DINO_CHANGE = 0.45         # GPU: cell's mean patch cosine similarity below this = changed. Measured 2026-10-20 on
                           # cells the correlation flagged: shadows / light on real lawns and drives 0.52-0.66 (10th
                           # percentile; single cells down to 0.39), a new object 0.33-0.63, people/vehicles excluded

MIN_REGION_CELLS = 2
MIN_REGION_AREA = 0.008    # of the frame
WHOLE = 0.6                # of the cells with detail: the whole view (covered, out of focus, glare)
VIEW_CHANGED = 0.55        # this much of the view changed = the whole view

# Moved: limits for the reference -> frame transform
MOVE_SHIFT = 0.03          # of the width
MOVE_ROT = 1.5             # degrees
MOVE_SCALE = 0.05
MIN_INLIERS = 15

PROBLEM_KINDS = {"no_picture", "frozen", "covered", "moved", "view_changed"}
TITLES = {
    "no_picture": "No picture", "frozen": "Picture frozen", "covered": "Lens covered", "blocked": "View partly blocked",
    "dark": "Too dark", "glare": "Glare / washed out", "blur": "Out of focus", "moved": "Camera moved",
    "view_changed": "View changed", "scene_change": "Scene changed", "ir_day": "Stuck in night mode",
}


@dataclass
class Frame:
    bgr: np.ndarray
    gray: np.ndarray
    mode: str                  # day | night
    chroma: float
    small: np.ndarray          # 160 px wide grey, for the frozen check

    @property
    def shape(self) -> tuple[int, int]:
        return self.gray.shape[:2]


def load(jpeg: bytes) -> Frame:
    arr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if arr is None or arr.size == 0:
        raise ValueError("not a picture")
    h, w = arr.shape[:2]
    bgr = cv2.resize(arr, (W, max(1, round(h * W / w))), interpolation=cv2.INTER_AREA) if w != W else arr
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB).astype(np.int16)
    chroma = float(np.mean(np.abs(lab[:, :, 1] - 128) + np.abs(lab[:, :, 2] - 128)))
    small = cv2.resize(gray, (160, max(1, round(gray.shape[0] * 160 / W))), interpolation=cv2.INTER_AREA)
    # Black-and-white = infrared night mode, unless the picture is black, flat or blown out (those have no colour
    # either): they're compared with the day reference, which then reports them for what they are.
    usable = gray.mean() >= DARK_MEAN and gray.std() >= 6 and (gray >= 250).mean() < 0.6
    return Frame(bgr, gray, "night" if chroma < MONO_CHROMA and usable else "day", round(chroma, 2), small)


def grid(shape: tuple[int, int]) -> tuple[int, int]:
    h, w = shape
    return max(1, round(h / CELL)), max(1, round(w / CELL))


def _cells(x: np.ndarray, g: tuple[int, int]) -> np.ndarray:
    """Mean of x over each grid cell (area resampling also handles cells that aren't whole pixels)."""
    return cv2.resize(x, (g[1], g[0]), interpolation=cv2.INTER_AREA)


@dataclass
class Stats:
    mean: np.ndarray
    std: np.ndarray
    sat: np.ndarray            # share of blown-out pixels
    sharp: np.ndarray          # fine detail, normalised by contrast (dimming isn't blur)


def stats(gray: np.ndarray, g: tuple[int, int], valid: np.ndarray | None = None) -> Stats:
    f = gray.astype(np.float32)
    m = _cells(f, g)
    sd = np.sqrt(np.maximum(_cells(f * f, g) - m * m, 0))
    sat = _cells((gray >= 250).astype(np.float32), g)
    lap = cv2.Laplacian(f, cv2.CV_32F, ksize=3)
    sharp = _cells(lap * lap, g) / (sd * sd + 25.0)
    return Stats(m, sd, sat, sharp)


def _ncc_cells(a: np.ndarray, b: np.ndarray, g: tuple[int, int]) -> np.ndarray:
    """Per-cell correlation of two float images (same size): 1 = same picture up to brightness and contrast."""
    ma, mb = _cells(a, g), _cells(b, g)
    va = np.maximum(_cells(a * a, g) - ma * ma, 1e-3)
    vb = np.maximum(_cells(b * b, g) - mb * mb, 1e-3)
    cov = _cells(a * b, g) - ma * mb
    return cov / np.sqrt(va * vb)


def best_ncc(cur: np.ndarray, ref: np.ndarray, g: tuple[int, int]) -> np.ndarray:
    """Per-cell correlation, the best over small shifts of the reference (blurred first: fine texture like gravel
    and leaves never lines up to the pixel)."""
    a = cv2.GaussianBlur(cur.astype(np.float32), (0, 0), 2)
    b = cv2.GaussianBlur(ref.astype(np.float32), (0, 0), 2)
    best = None
    for dy in SHIFTS:
        for dx in SHIFTS:
            m = np.float32([[1, 0, dx], [0, 1, dy]])
            n = _ncc_cells(a, cv2.warpAffine(b, m, (b.shape[1], b.shape[0]), borderMode=cv2.BORDER_REFLECT), g)
            best = n if best is None else np.maximum(best, n)
    return best


@dataclass
class Alignment:
    ok: bool                   # enough matching points to say anything
    matrix: np.ndarray | None  # reference -> frame (2x3)
    inliers: int = 0
    shift_px: float = 0.0
    dx: float = 0.0
    dy: float = 0.0
    rotation: float = 0.0
    scale: float = 1.0

    @property
    def moved(self) -> bool:
        return self.ok and (self.shift_px > MOVE_SHIFT * W or abs(self.rotation) > MOVE_ROT
                            or abs(self.scale - 1) > MOVE_SCALE)


_clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))


def align(ref: np.ndarray, cur: np.ndarray) -> Alignment:
    orb = cv2.ORB_create(nfeatures=1500, fastThreshold=10)
    k1, d1 = orb.detectAndCompute(_clahe.apply(ref), None)
    k2, d2 = orb.detectAndCompute(_clahe.apply(cur), None)
    if d1 is None or d2 is None or len(k1) < 20 or len(k2) < 20:
        return Alignment(False, None)
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(d1, d2, k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
    if len(good) < MIN_INLIERS:
        return Alignment(False, None, len(good))
    src = np.float32([k1[m.queryIdx].pt for m in good])
    dst = np.float32([k2[m.trainIdx].pt for m in good])
    m, mask = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=4.0)
    inliers = int(mask.sum()) if mask is not None else 0
    if m is None or inliers < MIN_INLIERS:
        return Alignment(False, None, inliers)
    scale = math.hypot(m[0, 0], m[1, 0])
    rot = math.degrees(math.atan2(m[1, 0], m[0, 0]))
    h, w = ref.shape[:2]
    cx, cy = w / 2, h / 2                                   # how far the picture's centre moved
    nx, ny = m[0, 0] * cx + m[0, 1] * cy + m[0, 2], m[1, 0] * cx + m[1, 1] * cy + m[1, 2]
    dx, dy = nx - cx, ny - cy
    return Alignment(True, m, inliers, math.hypot(dx, dy), dx, dy, rot, scale)


def regions(mask: np.ndarray, g: tuple[int, int], *, min_cells: int = MIN_REGION_CELLS) -> list[list[float]]:
    """Connected areas of a cell mask -> boxes [x, y, w, h] (0..1). Neighbouring cells join (8-connected) after
    closing one-cell gaps."""
    m = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    m &= cv2.dilate(mask.astype(np.uint8), np.ones((3, 3), np.uint8))     # closing mustn't grow the outline
    n, lab, st, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    rows, cols = g
    out = []
    for i in range(1, n):
        x, y, w, h, area = st[i]
        if area < min_cells or area / (rows * cols) < MIN_REGION_AREA:
            continue
        out.append([x / cols, y / rows, w / cols, h / rows])
    return out


def tighten(box: list[float], diff: np.ndarray) -> list[float]:
    """Shrink a cell-sized box to the changed pixels inside it (keeps the cell box if there are too few)."""
    h, w = diff.shape
    x0, y0 = int(box[0] * w), int(box[1] * h)
    x1, y1 = int((box[0] + box[2]) * w), int((box[1] + box[3]) * h)
    part = diff[y0:y1, x0:x1]
    ys, xs = np.nonzero(part)
    if len(xs) < 50:
        return box
    bx0, bx1 = np.percentile(xs, [1, 99])
    by0, by1 = np.percentile(ys, [1, 99])
    return [(x0 + bx0) / w, (y0 + by0) / h, max(bx1 - bx0, 4) / w, max(by1 - by0, 4) / h]


def _overlap(a: list[float], b: list[float]) -> float:
    """Share of box a that b covers."""
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    return ix * iy / max(a[2] * a[3], 1e-9)


def sun_elevation(lat: float, lng: float, when: datetime) -> float:
    """Degrees above the horizon (NOAA's simple formula: good to about a degree)."""
    when = when.astimezone(timezone.utc)
    day = when.timetuple().tm_yday
    hour = when.hour + when.minute / 60 + when.second / 3600
    g = 2 * math.pi / 365 * (day - 1 + (hour - 12) / 24)
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g) - 0.014615 * math.cos(2 * g)
                    - 0.040849 * math.sin(2 * g))
    tst = hour * 60 + eqt + 4 * lng
    ha = math.radians(tst / 4 - 180)
    la = math.radians(lat)
    cos_z = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    return 90 - math.degrees(math.acos(max(-1.0, min(1.0, cos_z))))


def issue(kind: str, *, severity: str | None = None, boxes: list | None = None, detail: str = "", **extra) -> dict:
    out = {"kind": kind, "severity": severity or ("problem" if kind in PROBLEM_KINDS else "warning"),
           "title": TITLES.get(kind, kind), "detail": detail, "boxes": [[round(float(v), 4) for v in b] for b in boxes or []]}
    out.update(extra)
    return out


def _area(boxes: list[list[float]]) -> float:
    return sum(b[2] * b[3] for b in boxes)


def _direction(a: Alignment) -> str:
    parts = []
    if abs(a.dx) > 0.01 * W:
        parts.append(f"{abs(a.dx) / W * 100:.0f}% {'right' if a.dx > 0 else 'left'}")
    if abs(a.dy) > 0.01 * W:
        parts.append(f"{abs(a.dy) / W * 100:.0f}% {'down' if a.dy > 0 else 'up'}")
    if abs(a.rotation) > 0.5:
        parts.append(f"turned {abs(a.rotation):.1f}°")
    if abs(a.scale - 1) > 0.02:
        parts.append(f"zoomed {'in' if a.scale > 1 else 'out'} {abs(a.scale - 1) * 100:.0f}%")
    return ("The reference view now sits " + ", ".join(parts)) if parts else "Small movement"


@dataclass
class Result:
    mode: str
    issues: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    usable_as_reference: bool = False      # nothing wrong on its own: may become the first reference
    absorb: np.ndarray | None = None       # cells to copy into the running background (None = leave it alone)


def check_alone(f: Frame) -> list[dict]:
    """Checks that need no reference (also all a camera gets until it has one)."""
    g = f.gray
    out = []
    if g.mean() < DARK_MEAN and np.percentile(g, 98) < 70:
        out.append(issue("dark", severity="problem", boxes=[[0, 0, 1, 1]], detail="The whole picture is black"))
    elif g.std() < 6:
        out.append(issue("covered", boxes=[[0, 0, 1, 1]], detail="The picture is one flat colour"))
    elif (g >= 250).mean() > 0.6:
        out.append(issue("glare", severity="problem", boxes=[[0, 0, 1, 1]], detail="Most of the picture is blown out to white"))
    return out


def _object_cells(objects: list[dict], g: tuple[int, int]) -> np.ndarray:
    rows, cols = g
    m = np.zeros(g, bool)
    for o in objects:
        x, y, w, h = o["box"]
        m[max(0, int(y * rows)):min(rows, math.ceil((y + h) * rows)), max(0, int(x * cols)):min(cols, math.ceil((x + w) * cols))] = True
    return m


def compose_background(bg: Frame, f: Frame, absorb: np.ndarray) -> np.ndarray:
    """The new running background (BGR): the frame's pixels in the absorbed cells, the old background elsewhere."""
    mask = cv2.resize(absorb.astype(np.uint8), (f.shape[1], f.shape[0]), interpolation=cv2.INTER_NEAREST).astype(bool)
    out = bg.bgr.copy()
    out[mask] = f.bgr[mask]
    return out


def frame_from_bgr(bgr: np.ndarray) -> Frame:
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return load(buf.tobytes())


def analyze(f: Frame, ref: Frame | None, *, bg: Frame | None = None, ai=None, view_checks: bool = True,
            lat: float | None = None, lng: float | None = None, now: datetime | None = None) -> Result:
    """All checks on one frame. `ref` is the reference for the same mode (None while learning), `bg` the running
    background (None = the reference). `ai` is app.camhealth.ai.AI or None. `view_checks` False skips moved / view
    changed / scene change (PTZ cameras)."""
    res = Result(f.mode, metrics={"chroma": f.chroma, "mean": round(float(f.gray.mean()), 1),
                                  "std": round(float(f.gray.std()), 1)})
    alone = check_alone(f)
    if lat is not None and lng is not None and f.mode == "night":
        elev = sun_elevation(lat, lng, now or datetime.now(timezone.utc))
        res.metrics["sun"] = round(elev, 1)
        if elev > 8 and f.gray.mean() > 40:
            alone.append(issue("ir_day", detail=f"Black-and-white (infrared) picture with the sun {elev:.0f}° up"))
    if ref is None or ref.shape != f.shape:
        res.issues += alone
        res.usable_as_reference = not alone
        return res

    gshape = grid(f.shape)
    ref_gray, ref_bgr = ref.gray, ref.bgr
    valid = np.ones(f.shape, np.uint8)
    a = align(ref.gray, f.gray)
    res.metrics.update(inliers=a.inliers, shift=round(a.shift_px / W, 3), rot=round(a.rotation, 2),
                       scale=round(a.scale, 3))
    if not view_checks and (not a.ok or a.moved):
        # A PTZ camera looking somewhere else: comparing it with the reference means nothing, the plain checks stay.
        res.issues += alone
        return res
    if a.ok:
        ref_gray = cv2.warpAffine(ref.gray, a.matrix, (f.shape[1], f.shape[0]), borderMode=cv2.BORDER_REFLECT)
        ref_bgr = cv2.warpAffine(ref.bgr, a.matrix, (f.shape[1], f.shape[0]), borderMode=cv2.BORDER_REFLECT)
        valid = cv2.warpAffine(np.ones(ref.shape, np.uint8), a.matrix, (f.shape[1], f.shape[0]))
        if a.moved:
            h, w = ref.shape
            corners = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
            poly = (corners @ a.matrix[:, :2].T + a.matrix[:, 2]) / np.float32([w, h])
            res.issues.append(issue("moved", detail=_direction(a),
                                    poly=[[round(float(x), 4), round(float(y), 4)] for x, y in poly]))
    rs = stats(ref_gray, gshape)
    cs = stats(f.gray, gshape)
    inside = _cells(valid.astype(np.float32), gshape) > 0.9          # cells the shifted reference still covers
    textured = (rs.std > TEXTURED_STD) & inside
    n_tex = max(int(textured.sum()), 1)

    dark = inside & (cs.mean < DARK_MEAN) & (rs.mean > LIT_MEAN)
    glare = inside & (cs.sat > SAT_NOW) & (rs.sat < SAT_REF) & ~dark
    flat = textured & (cs.std < FLAT_STD) & ~dark & ~glare
    coarse = best_ncc(cv2.resize(f.gray, None, fx=0.25, fy=0.25), cv2.resize(ref_gray, None, fx=0.25, fy=0.25), gshape)
    ncc = best_ncc(f.gray, ref_gray, gshape)
    ratio = cs.sharp / np.maximum(rs.sharp, 1e-3)
    blur = textured & ~flat & ~dark & ~glare & (ratio < BLUR_RATIO) & (coarse > BLUR_SHAPE_NCC) & (cs.std > FLAT_STD)
    res.metrics.update(cells=int(gshape[0] * gshape[1]), textured=int(textured.sum()),
                       sharp_ratio=round(float(np.median(ratio[textured])) if textured.any() else 1.0, 3),
                       flat=int(flat.sum()), dark=int(dark.sum()), glare=int(glare.sum()),
                       blur=int(blur.sum()), ncc_median=round(float(np.median(ncc[textured])) if textured.any() else 1, 3))
    full = [[0, 0, 1, 1]]

    if flat.sum() >= WHOLE * n_tex:
        res.issues.append(issue("covered", boxes=full, detail="The picture lost almost all its detail"))
    elif (b := regions(flat, gshape)):
        res.issues.append(issue("blocked", boxes=b, detail=f"{_area(b) * 100:.0f}% of the view has no detail any more"))
    if dark.sum() >= WHOLE * max(int((rs.mean > LIT_MEAN).sum()), 1) and dark.sum() > 0:
        res.issues.append(issue("dark", severity="problem", boxes=full, detail="The picture went black"))
    elif (b := regions(dark, gshape)):
        res.issues.append(issue("dark", boxes=b, detail="An area that was lit is black (a light out?)"))
    if glare.sum() >= WHOLE * gshape[0] * gshape[1]:
        res.issues.append(issue("glare", severity="problem", boxes=full, detail="The picture is blown out to white"))
    elif (b := regions(glare, gshape)):
        res.issues.append(issue("glare", boxes=b, detail="Glare is washing out part of the view"))
    if blur.sum() >= WHOLE * n_tex:
        res.issues.append(issue("blur", severity="problem", boxes=full, detail=f"Fine detail down to {res.metrics['sharp_ratio'] * 100:.0f}% "
                                                         "of the reference"))
    elif (b := regions(blur, gshape)):
        res.issues.append(issue("blur", boxes=b, detail="Part of the view is blurry (a smudge or water on the lens?)"))

    if view_checks and not any(i["kind"] in ("covered", "moved") for i in res.issues):
        _scene_change(res, f, bg if bg is not None and bg.shape == f.shape else Frame(ref_bgr, ref_gray, ref.mode, 0, ref.small),
                      ai, gshape, inside, cs, flat | dark | glare | blur)
    res.issues += [i for i in alone if i["kind"] == "ir_day"]
    if not any(i["kind"] in ("covered", "dark", "glare") for i in res.issues):
        res.issues += [i for i in alone if i["kind"] in ("covered", "dark", "glare")]
    return res


def _scene_change(res: Result, f: Frame, base: Frame, ai, g: tuple[int, int], inside: np.ndarray, cs: Stats,
                  others: np.ndarray) -> None:
    """Scene change against the running background; sets res.absorb (what the background takes from this frame)."""
    bs = stats(base.gray, g)
    textured = (bs.std > TEXTURED_STD) & inside
    n_tex = max(int(textured.sum()), 1)
    ncc = best_ncc(f.gray, base.gray, g)
    others = others | cv2.dilate(others.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)   # their edges too
    changed = textured & ~others & (ncc < CHANGE_NCC)
    # A cell that had no detail but has some now (something new in front of a plain wall) changed too.
    changed |= inside & ~textured & ~others & (cs.std > TEXTURED_STD * 1.5) & (ncc < CHANGE_NCC)
    held = np.zeros(g, bool)                   # people / vehicles now: never taken into the background
    res.metrics["ncc_changed"] = int(changed.sum())
    if ai is not None and changed.any():
        try:
            cur_feat, base_feat = ai.patch_features(f.bgr), ai.patch_features(base.bgr)
            sim = (cur_feat * base_feat).sum(-1).astype(np.float32)
            sim_cells = cv2.resize(sim, (g[1], g[0]), interpolation=cv2.INTER_AREA)
            changed &= sim_cells < DINO_CHANGE
            now_objs, base_objs = ai.objects(f.bgr), ai.objects(base.bgr)
            held = _object_cells(now_objs, g)
            changed &= ~held & ~_object_cells(base_objs, g)   # a car that left the background isn't a change either
            res.metrics.update(dino_p5=round(float(np.percentile(sim, 5)), 3), objects=len(now_objs),
                               objects_bg=len(base_objs))
        except Exception as exc:  # noqa: BLE001 - the CPU result stands
            res.metrics["ai_error"] = str(exc)[:200]
    res.metrics["changed"] = int(changed.sum())
    if changed.sum() >= VIEW_CHANGED * n_tex:
        res.issues.append(issue("view_changed", boxes=[[0, 0, 1, 1]], detail="Most of the view no longer matches"))
        return                                 # the background stays as it was
    diff = cv2.absdiff(cv2.GaussianBlur(f.gray, (0, 0), 2), cv2.GaussianBlur(base.gray, (0, 0), 2))
    diff = cv2.morphologyEx((diff > 30).astype(np.uint8), cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    boxes = [tighten(b, diff) for b in regions(changed, g)]
    if boxes:
        res.issues.append(issue("scene_change", boxes=boxes,
                                detail=f"{len(boxes)} area{'s' if len(boxes) > 1 else ''} changed and stayed "
                                       f"({_area(boxes) * 100:.0f}% of the view)"))
    res.absorb = inside & ~changed & ~held & ~others


def frozen(f: Frame, previous_small: np.ndarray | None) -> bool:
    return previous_small is not None and previous_small.shape == f.small.shape and np.array_equal(previous_small, f.small)
