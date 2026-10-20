"""Camera health: what is shown, from what the checks found over time (pure; the worker stores the state as JSON).

One bad frame never raises anything: an issue shows after RAISE_AFTER checks in a row and clears after CLEAR_AFTER
clean ones. Scene changes and AI lens findings must also stay in the same place (boxes overlapping from check to
check), so a person or car passing through never adds up.
"""

from __future__ import annotations

RAISE_AFTER = {"no_picture": 2, "frozen": 2, "scene_change": 3, "lens": 3, "glare": 3, "ir_day": 3}
DEFAULT_RAISE = 2
CLEAR_AFTER = 2
TRACKED = {"scene_change", "lens"}       # kinds whose boxes must stay put
TRACK_IOU = 0.2
# Issues measured against the reference picture: a new reference (Accept view) starts them over.
REFERENCE_KINDS = {"moved", "view_changed", "scene_change", "blocked", "blur", "dark", "glare", "covered"}


def iou(a: list[float], b: list[float]) -> float:
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def _tracked(kind: str, issue: dict | None, tracks: list[dict], need: int) -> tuple[dict | None, list[dict]]:
    """Match this check's boxes to the earlier ones; only boxes seen `need` checks in a row count."""
    new_tracks = []
    for box in (issue or {}).get("boxes", []):
        best = max(tracks, key=lambda t: iou(t["box"], box), default=None)
        n = best["n"] + 1 if best is not None and iou(best["box"], box) >= TRACK_IOU else 1
        new_tracks.append({"box": box, "n": n})
    steady = [t["box"] for t in new_tracks if t["n"] >= need]
    if not steady:
        return None, new_tracks
    out = dict(issue)
    out["boxes"] = steady
    return out, new_tracks


def update(state: dict | None, found: list[dict], now_ms: int) -> tuple[list[dict], dict, list[dict]]:
    """(issues to show, new state, events [{kind, change: raised|cleared, title, severity}])."""
    st = {"streak": {}, "miss": {}, "shown": {}, "tracks": {}}
    st.update(state or {})
    by_kind: dict[str, dict] = {}
    for i in found:
        key = i["kind"] if i["kind"] != "lens" else f"lens:{i.get('label', '')}"
        if key in by_kind:                                    # e.g. two lens crops of the same kind: one issue
            by_kind[key] = {**by_kind[key], "boxes": by_kind[key]["boxes"] + i["boxes"]}
        else:
            by_kind[key] = i
    events = []
    keys = set(by_kind) | set(st["shown"]) | set(st["streak"])
    streak, miss, shown, tracks = dict(st["streak"]), dict(st["miss"]), dict(st["shown"]), dict(st["tracks"])
    for key in sorted(keys):
        kind = key.split(":")[0]
        need = RAISE_AFTER.get(kind, DEFAULT_RAISE)
        cur = by_kind.get(key)
        if kind in TRACKED:
            cur, tracks[key] = _tracked(kind, cur, tracks.get(key, []), need)
            if not tracks[key]:
                tracks.pop(key)
        if cur is not None:
            streak[key] = streak.get(key, 0) + 1
            miss.pop(key, None)
            if key in shown:
                shown[key] = {**cur, "since_ms": shown[key]["since_ms"]}
            elif kind in TRACKED or streak[key] >= need:
                shown[key] = {**cur, "since_ms": now_ms}
                events.append({"kind": kind, "change": "raised", "title": cur["title"], "severity": cur["severity"],
                               "detail": cur.get("detail", "")})
        else:
            streak.pop(key, None)
            if key in shown:
                miss[key] = miss.get(key, 0) + 1
                if miss[key] >= CLEAR_AFTER:
                    events.append({"kind": kind, "change": "cleared", "title": shown[key]["title"],
                                   "severity": shown[key]["severity"]})
                    shown.pop(key)
                    miss.pop(key)
    new = {"streak": streak, "miss": miss, "shown": shown, "tracks": tracks}
    order = {"problem": 0, "warning": 1}
    issues = sorted(shown.values(), key=lambda i: (order.get(i["severity"], 2), i["since_ms"]))
    return issues, new, events


def forget(state: dict | None, kinds: set[str]) -> dict:
    """Drop these kinds (a new reference makes moved / scene change / ... start over)."""
    st = {k: dict(v) for k, v in (state or {}).items() if isinstance(v, dict)}
    for part in st.values():
        for key in [k for k in part if k.split(":")[0] in kinds]:
            part.pop(key)
    return st


def status(issues: list[dict]) -> str:
    if any(i["severity"] == "problem" for i in issues):
        return "problem"
    return "warning" if issues else "ok"
