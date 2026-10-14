"""The moving alarm picture (GIF, with its analytics boxes) for texts sent once someone verified the alarm.

The user, 2026-10-14: "can the sms send a gif of the alarm instead of an image? and it has the bounding box?" ->
"yes build the gif that way": the first alarm text keeps the still (it goes within seconds, before NX has recorded
what follows the alarm, and must never wait for a GIF); the Confirmed and Dispatch texts go after an operator
looked, so the seconds around the alarm exist, and they carry this GIF.

GIF_PRE_MS before to GIF_POST_MS after the event, from NX's secondary stream (clips._download: no transcoding on NX),
frames at GIF_FPS through ffmpeg (niced), each with the boxes the analytics drew at that frame's own time
(report._annotate, the incident report's drawing), shrunk until it fits GIF_MAX_BYTES (US carriers take MMS up to
~600 KB reliably). Cached per alarm; the text and Twilio's fetch share one build. Anything failing -> None, and the
caller sends the still picture instead (or words alone): a GIF never costs or holds back a text beyond its budget.
"""
import asyncio
import io
import logging
import shutil
import tempfile
import time
from pathlib import Path

from app.config import get_settings
from app.models import Alarm
from app.services import clips

log = logging.getLogger("portal.gif")

GIF_PRE_MS = 1000            # from 1 s before the alarm
GIF_POST_MS = 3000           # to 3 s after it
GIF_FPS = 5
GIF_MAX_BYTES = 600_000
GIF_BUDGET_S = 6.0           # the text waits at most this long for the GIF, then goes with the still
MIN_SPAN_MS = 2000           # less footage than this recorded yet: wait for it (within the budget)
# (width px, keep every Nth frame, palette colours): tried in order until the GIF fits GIF_MAX_BYTES
SIZES = ((480, 1, 128), (400, 1, 96), (360, 2, 96), (320, 2, 64), (280, 3, 48))

_gifs: dict[int, bytes] = {}
_MAX = 40
_inflight: dict[int, asyncio.Task] = {}
_slots: asyncio.Semaphore | None = None
_failed: dict[int, float] = {}           # alarm id -> when the last build failed (not retried for a minute)


class GifError(Exception):
    pass


async def _frames(alarm: Alarm, start_ms: int, end_ms: int, work: Path) -> list[tuple[int, bytes]]:
    """(wall-clock ms, JPEG) every 1/GIF_FPS s from start_ms to end_ms."""
    settings = get_settings()
    raw = work / "raw.mp4"
    await clips._download(alarm, start_ms, end_ms, "sd", raw)
    meta = await clips.probe(raw)
    first_ms = meta["start_ms"] or start_ms             # NX starts on the keyframe before positionMs
    skip = max(0.0, (start_ms - first_ms) / 1000)
    code, _, err = await clips._run(settings.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y",
                                    "-ss", f"{skip:.3f}", "-i", str(raw), "-t", f"{(end_ms - start_ms) / 1000:.3f}",
                                    "-an", "-vf", f"fps={GIF_FPS},scale='min(640,iw)':-2", "-q:v", "3",
                                    str(work / "f%03d.jpg"), timeout=30)
    if code != 0:
        raise GifError(f"ffmpeg: {err.decode(errors='replace')[-200:]}")
    files = sorted(work.glob("f*.jpg"))
    if len(files) < 3:
        raise GifError(f"only {len(files)} frames recorded")
    step = 1000 // GIF_FPS
    return [(max(start_ms, first_ms) + i * step, f.read_bytes()) for i, f in enumerate(files)]


def _encode(frames: list[bytes]) -> bytes:
    """Annotated JPEG frames -> the smallest GIF of SIZES that fits GIF_MAX_BYTES (else the smallest made)."""
    from PIL import Image
    imgs = [Image.open(io.BytesIO(f)).convert("RGB") for f in frames]
    out = b""
    for width, every, colours in SIZES:
        pick = imgs[::every]
        h = max(2, round(pick[0].height * width / pick[0].width))
        pal = [im.resize((width, h), Image.Resampling.BILINEAR).quantize(colours, method=Image.Quantize.MEDIANCUT,
                                                                          dither=Image.Dither.NONE) for im in pick]
        buf = io.BytesIO()
        pal[0].save(buf, "GIF", save_all=True, append_images=pal[1:], duration=1000 // GIF_FPS * every, loop=0,
                    optimize=True, disposal=1)
        out = buf.getvalue()
        if len(out) <= GIF_MAX_BYTES:
            break
    return out


def _recorded_by() -> int:
    return int(time.time() * 1000) - clips.PARTIAL_LAG_MS          # NX's archive lags live by ~1-3 s


async def _build(alarm: Alarm) -> bytes:
    from app.services import report
    t0 = time.monotonic()
    start = alarm.event_ts_ms - GIF_PRE_MS
    end = min(alarm.event_ts_ms + GIF_POST_MS, _recorded_by())
    work = Path(tempfile.mkdtemp(prefix="gif-", dir=clips._cache_dir()))
    try:
        frames, objs = await asyncio.gather(_frames(alarm, start, end, work),
                                            clips.objects(alarm, start - 1000, end + 1000), return_exceptions=True)
        if isinstance(frames, BaseException):
            raise frames
        if isinstance(objs, BaseException):          # boxes are a bonus; the moving picture alone still helps
            log.info("gif: alarm %s: no analytics boxes (%s)", alarm.id, objs)
            objs = []

        def draw() -> bytes:
            return _encode([report._annotate(jpeg, report._boxes_at(objs, ts)) for ts, jpeg in frames])
        gif = await asyncio.to_thread(draw)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    log.info("gif: alarm %s: %d frames, %d s of video, %d boxes' tracks, %d bytes in %d ms", alarm.id, len(frames),
             (end - start) // 1000, len(objs), len(gif), int((time.monotonic() - t0) * 1000))
    return gif


async def alarm_gif(alarm: Alarm, budget_s: float | None = None) -> bytes | None:
    """The GIF for this alarm (cached), or None if it can't be made within budget_s. The build carries on past the
    budget (Twilio's fetch or the next text gets it), but nobody waits longer than their budget."""
    global _slots
    budget_s = GIF_BUDGET_S if budget_s is None else budget_s
    if alarm.id in _gifs:
        return _gifs[alarm.id]
    if not alarm.device_id or time.time() - _failed.get(alarm.id, 0) < 60:
        return None
    task = _inflight.get(alarm.id)
    if task is None:
        if _slots is None:
            _slots = asyncio.Semaphore(2)

        async def run(a=alarm):
            wait_ms = a.event_ts_ms - GIF_PRE_MS + MIN_SPAN_MS - _recorded_by()
            if wait_ms > 0:                  # confirmed within ~4 s of the alarm: let NX record a little first
                await asyncio.sleep(wait_ms / 1000)
            async with _slots:
                return await _build(a)
        task = asyncio.create_task(run())
        _inflight[alarm.id] = task

        def done(t: asyncio.Task, aid=alarm.id):
            _inflight.pop(aid, None)
            if t.cancelled():
                return
            if t.exception():
                _failed[aid] = time.time()
                log.warning("gif: alarm %s: %s: %s", aid, type(t.exception()).__name__, t.exception())
                return
            gif = t.result()
            if gif[:6] in (b"GIF89a", b"GIF87a"):
                _gifs[aid] = gif
                while len(_gifs) > _MAX:
                    _gifs.pop(next(iter(_gifs)))
        task.add_done_callback(done)
    try:
        gif = await asyncio.wait_for(asyncio.shield(task), budget_s)
    except asyncio.TimeoutError:
        log.warning("gif: alarm %s: not ready within %.0f s; the text goes with the still picture", alarm.id, budget_s)
        return None
    except Exception:  # noqa: BLE001 - logged by done()
        return None
    return gif if gif[:6] in (b"GIF89a", b"GIF87a") else None
