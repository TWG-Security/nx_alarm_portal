"""Alarm video clips.

The portal pulls a short clip of the archive around the alarm from NX, makes it
browser-playable, caches it on disk and serves it with HTTP range support so the
<video> element can seek and loop.

  1. NX:   GET /rest/v4/devices/{id}/media.mp4?positionMs&durationMs
           "sd" = the camera's secondary stream as recorded (fast: no transcoding on NX)
           "hd" = primary stream transcoded by NX to 720p H.264 (slower, sharper)
  2. NX starts the clip on the keyframe before positionMs and records the true start
     time in the MP4 comment tag ({"startTimeMs": "..."}), which ffprobe reads. That
     keeps bounding boxes in sync to the frame.
  3. ffmpeg: H.264 is only rewrapped (+faststart, no audio); anything else
     (H.265, MPEG-4 Part 2, ...) is transcoded to H.264 so every browser can play it.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from app.config import get_settings
from app.models import Alarm
from app.services.poller import manager

log = logging.getLogger("portal.clips")

QUALITIES = ("sd", "hd")
READY_MARGIN_MS = 4000          # archive for the last few seconds may not be written yet

_inflight: dict[str, asyncio.Task] = {}
_errors: dict[str, tuple[float, str]] = {}          # key -> (when, message); retried after a minute
_nx_slots = asyncio.Semaphore(3)
_ffmpeg_slots = asyncio.Semaphore(1)                # 2-vCPU box: one transcode at a time


class ClipError(Exception):
    pass


@dataclass
class ClipInfo:
    status: str                 # ready | processing | pending | error
    start_ms: int = 0           # wall-clock time of the clip's first frame
    duration_ms: int = 0
    event_ts_ms: int = 0
    url: str = ""
    codec: str = ""
    transcoded: bool = False
    ready_in_s: float = 0       # for "pending": when the footage will exist
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _cache_dir() -> Path:
    p = Path(get_settings().media_cache_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def clip_key(alarm_id: int, pre_s: int, post_s: int, quality: str) -> str:
    return f"a{alarm_id}_{pre_s}_{post_s}_{quality}"


def clip_path(key: str) -> Path:
    return _cache_dir() / f"{key}.mp4"


def _meta_path(key: str) -> Path:
    return _cache_dir() / f"{key}.json"


def window(alarm: Alarm, pre_s: int, post_s: int) -> tuple[int, int]:
    return alarm.event_ts_ms - pre_s * 1000, alarm.event_ts_ms + post_s * 1000


def normalize(pre_s: int | None, post_s: int | None) -> tuple[int, int]:
    s = get_settings()
    pre = s.clip_pre_s if pre_s is None else max(0, int(pre_s))
    post = s.clip_post_s if post_s is None else max(1, int(post_s))
    if pre + post > s.clip_max_window_s:
        raise ClipError(f"Clip window can be at most {s.clip_max_window_s} seconds")
    return pre, post


async def _run(*args: str, timeout: float = 180) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ClipError("Video processing timed out")
    return proc.returncode, out, err


async def probe(path: Path) -> dict:
    code, out, err = await _run(get_settings().ffprobe_path, "-v", "error", "-print_format", "json",
                                "-show_entries", "format=duration:format_tags=comment:stream=codec_type,codec_name",
                                str(path), timeout=30)
    if code != 0:
        raise ClipError("NX returned no playable video for that time (no recording?)")
    data = json.loads(out or b"{}")
    video = next((st for st in data.get("streams", []) if st.get("codec_type") == "video"), None)
    if not video:
        raise ClipError("No video in the recording for that time")
    start_ms = None
    try:
        start_ms = int(json.loads((data.get("format", {}).get("tags") or {}).get("comment") or "{}").get("startTimeMs"))
    except (ValueError, TypeError):
        pass
    return {"codec": video.get("codec_name", ""), "duration_s": float(data.get("format", {}).get("duration") or 0),
            "start_ms": start_ms}


async def _download(alarm: Alarm, start_ms: int, end_ms: int, quality: str, dest: Path) -> None:
    client = manager.client_for(alarm.site)
    params: dict = {"positionMs": start_ms, "durationMs": end_ms - start_ms}
    if quality == "hd":
        params.update(stream="primary", videoCodec="h264", resolution="720p")
    else:
        params.update(stream="secondary")
    if not client._token:
        await client._login()
    url = f"{client.base_url}/rest/v4/devices/{alarm.device_id}/media.mp4"
    async with _nx_slots:
        for attempt in (1, 2):
            try:
                async with client._client.stream("GET", url, params=params, headers=client._headers(),
                                                 timeout=httpx.Timeout(300, read=120)) as r:
                    if r.status_code == 401 and attempt == 1:
                        await client._login()
                        continue
                    if r.status_code != 200:
                        body = (await r.aread())[:300].decode(errors="replace")
                        raise ClipError(f"NX refused the clip (HTTP {r.status_code}) {body}")
                    with dest.open("wb") as f:
                        async for chunk in r.aiter_bytes():
                            f.write(chunk)
                    return
            except httpx.HTTPError as exc:
                raise ClipError(f"Could not download the clip from NX ({exc.__class__.__name__})") from exc


async def _build(alarm: Alarm, key: str, pre_s: int, post_s: int, quality: str) -> ClipInfo:
    settings = get_settings()
    start_ms, end_ms = window(alarm, pre_s, post_s)
    raw = _cache_dir() / f"{key}.raw.mp4"
    out = clip_path(key)
    tmp = _cache_dir() / f"{key}.tmp.mp4"
    try:
        await _download(alarm, start_ms, end_ms, quality, raw)
        meta = await probe(raw)
        transcode = meta["codec"] != "h264"
        common = ["-hide_banner", "-loglevel", "error", "-y", "-i", str(raw), "-an", "-sn", "-dn",
                  "-map_metadata", "-1", "-movflags", "+faststart"]
        if transcode:
            args = common + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "25", "-pix_fmt", "yuv420p",
                             "-vf", "scale=-2:'min(720,ih)'", str(tmp)]
            async with _ffmpeg_slots:
                code, _, err = await _run(settings.ffmpeg_path, *args, timeout=600)
        else:
            code, _, err = await _run(settings.ffmpeg_path, *(common + ["-c:v", "copy", str(tmp)]), timeout=120)
        if code != 0:
            raise ClipError(f"Video conversion failed: {err.decode(errors='replace')[-300:]}")
        os.replace(tmp, out)
        info = ClipInfo(status="ready", start_ms=meta["start_ms"] or start_ms,
                        duration_ms=int(meta["duration_s"] * 1000), event_ts_ms=alarm.event_ts_ms,
                        url=f"/media/clips/{key}.mp4", codec=meta["codec"], transcoded=transcode)
        _meta_path(key).write_text(json.dumps(info.to_dict()))
        log.info("clip %s ready (%s%s, %.1fs)", key, meta["codec"], " -> h264" if transcode else "", meta["duration_s"])
        _evict()
        return info
    finally:
        for p in (raw, tmp):
            p.unlink(missing_ok=True)


def cached(key: str) -> ClipInfo | None:
    m, c = _meta_path(key), clip_path(key)
    if m.exists() and c.exists():
        try:
            info = ClipInfo(**json.loads(m.read_text()))
            os.utime(c)                      # LRU touch
            return info
        except (ValueError, TypeError):
            return None
    return None


async def get_clip(alarm: Alarm, pre_s: int | None = None, post_s: int | None = None, quality: str = "sd",
                   wait_s: float = 1.5) -> ClipInfo:
    """Return the clip if cached; otherwise start building it and report progress."""
    if quality not in QUALITIES:
        raise ClipError("Unknown quality")
    if not alarm.device_id:
        return ClipInfo(status="error", message="This alarm has no camera")
    pre, post = normalize(pre_s, post_s)
    key = clip_key(alarm.id, pre, post, quality)
    info = cached(key)
    if info:
        return info
    _, end_ms = window(alarm, pre, post)
    wait_ms = end_ms + READY_MARGIN_MS - int(time.time() * 1000)
    if wait_ms > 0:
        return ClipInfo(status="pending", event_ts_ms=alarm.event_ts_ms, ready_in_s=round(wait_ms / 1000, 1),
                        message="Recording is still in progress")
    err = _errors.get(key)
    if err and time.time() - err[0] < 60:
        return ClipInfo(status="error", event_ts_ms=alarm.event_ts_ms, message=err[1])
    task = _inflight.get(key)
    if task is None:
        task = asyncio.create_task(_build(alarm, key, pre, post, quality))
        _inflight[key] = task

        def _done(t: asyncio.Task, key=key):
            _inflight.pop(key, None)
            if not t.cancelled() and t.exception():
                _errors[key] = (time.time(), str(t.exception()))
                log.warning("clip %s failed: %s", key, t.exception())
        task.add_done_callback(_done)
    try:
        return await asyncio.wait_for(asyncio.shield(task), wait_s)
    except asyncio.TimeoutError:
        return ClipInfo(status="processing", event_ts_ms=alarm.event_ts_ms, message="Preparing clip")
    except ClipError as exc:
        return ClipInfo(status="error", event_ts_ms=alarm.event_ts_ms, message=str(exc))


def _evict() -> None:
    limit = get_settings().media_cache_max_mb * 1024 * 1024
    files = sorted(_cache_dir().glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    for p in files:
        if total <= limit:
            break
        total -= p.stat().st_size
        p.unlink(missing_ok=True)
        p.with_suffix(".json").unlink(missing_ok=True)


# ---------------------------------------------------------------- prefetch
_prefetch: set[asyncio.Task] = set()


def schedule_prefetch(alarm: Alarm) -> None:
    """Build the default clip for loud alarms as soon as the footage exists."""
    if not get_settings().prefetch_clips or not alarm.device_id or alarm.priority > 2:
        return
    settings = get_settings()

    async def run(alarm_id: int = alarm.id, site_id: int = alarm.site_id):
        _, end_ms = alarm.event_ts_ms - settings.clip_pre_s * 1000, alarm.event_ts_ms + settings.clip_post_s * 1000
        await asyncio.sleep(max(0, (end_ms + READY_MARGIN_MS) / 1000 - time.time()))
        from app.db import sessionmaker
        async with sessionmaker()() as db:
            a = await db.get(Alarm, alarm_id)
            if a is not None:
                await get_clip(a, wait_s=600)

    t = asyncio.create_task(run())
    _prefetch.add(t)
    t.add_done_callback(_prefetch.discard)


# ---------------------------------------------------------------- analytics objects (bounding boxes)
def _parse_box(s: str) -> list[float] | None:
    """NX box "{x},{y},{w}x{h}" (0..1) -> [x, y, w, h], or None if empty/invalid."""
    try:
        left, h = s.rsplit("x", 1)
        x, y, w = (float(v) for v in left.split(","))
        h = float(h)
    except ValueError:
        return None
    if w <= 0 or h <= 0:
        return None
    return [round(x, 5), round(y, 5), round(w, 5), round(h, 5)]


def _label(type_id: str) -> str:
    return (type_id or "object").rsplit(".", 1)[-1].replace("_", " ").title()


async def objects(alarm: Alarm, start_ms: int, end_ms: int) -> list[dict]:
    """Analytics object tracks with per-frame boxes inside [start_ms, end_ms].

    The event's own track is flagged primary; other tracks on the camera in the
    window are included so operators see everything the analytics saw.
    """
    if not alarm.device_id:
        return []
    client = manager.client_for(alarm.site)
    ev = (alarm.raw or {}).get("eventData") or {}
    own = (ev.get("objectTrackId") or "").strip("{}")
    own = "" if own.startswith("00000000-0000") else own
    tracks = await client.search_object_tracks(device_ids=[alarm.device_id], start_time_ms=start_ms,
                                               end_time_ms=end_ms, limit=25) or []
    ids = {t.get("id", "").strip("{}"): t for t in tracks}
    if own and own not in ids:
        try:
            ids[own] = await client.get_object_track(own)
        except httpx.HTTPError:
            pass

    async def one(tid: str, t: dict) -> dict | None:
        try:
            meta = await client._get(f"/rest/v4/analytics/objectTracks/{tid}/objectMetadata",
                                     params={"deviceId": alarm.device_id}) or []
        except httpx.HTTPError:
            return None
        boxes = []
        for m in meta:
            b = _parse_box(m.get("boundingBox") or "")
            ts = int(m.get("timestampMs") or 0)
            if b and start_ms - 2000 <= ts <= end_ms + 2000:
                boxes.append([ts, int(m.get("durationMs") or 0), *b])
        if not boxes:
            return None
        attrs = {a.get("name"): a.get("value") for a in t.get("attributes") or [] if a.get("name")}
        return {"id": tid, "primary": tid == own, "type": t.get("objectTypeId", ""), "label": _label(t.get("objectTypeId", "")),
                "attributes": attrs, "start_ms": t.get("startTimeMs"), "end_ms": t.get("endTimeMs"),
                "boxes": sorted(boxes)}

    results = await asyncio.gather(*(one(tid, t) for tid, t in list(ids.items())[:25]))
    out = [r for r in results if r]

    # Some analytics events carry a single box but no track.
    bb = ev.get("boundingBox") or {}
    if not out and isinstance(bb, dict) and bb.get("width") and bb.get("height"):
        out.append({"id": "event", "primary": True, "type": ev.get("eventTypeId", ""), "label": alarm.caption,
                    "attributes": {}, "start_ms": alarm.event_ts_ms - 1000, "end_ms": alarm.event_ts_ms + 2000,
                    "boxes": [[alarm.event_ts_ms - 1000, 3000, bb["x"], bb["y"], bb["width"], bb["height"]]]})
    return sorted(out, key=lambda r: (not r["primary"], r["start_ms"] or 0))
