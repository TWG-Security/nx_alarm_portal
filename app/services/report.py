"""Incident report (PDF) and evidence package (ZIP) for one alarm.

The report: summary, timestamps (site time zone and UTC), the event-time frame from NX, a
sequence of stills from the clip with the analytics boxes drawn in, a timeline built from
the audit log, the operator's notes, and the clip's SHA-256. The package adds the MP4, the
original and annotated stills, and a manifest with every file's checksum.

CPU-heavy steps (PDF layout, drawing, zipping, hashing) run in a worker thread so an
export never holds up alarm delivery on the event loop.
"""

import asyncio
import hashlib
import io
import json
import logging
import math
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import Image as RLImage
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from xml.sax.saxutils import escape

from app.config import get_settings
from app.models import Alarm, AlarmNote, AuditLog, User
from app.services import arming, clips
from app.services.alarm_filter import LEVEL_NAMES

log = logging.getLogger("portal.report")

# TWG branding: charcoal text, red accents, white page, Helvetica (Arial metrics).
INK = colors.HexColor("#1A1A1A")
RED = colors.HexColor("#C0392B")
MUTED = colors.HexColor("#5F5F5F")
RULE = colors.HexColor("#D6D6D6")
SOFT = colors.HexColor("#F5F5F5")
LOGO = Path("app/static/img/twg-logo.png")
FRAME_OFFSETS_S = (-5, -2, 0, 2, 5, 10)     # stills around the alarm, kept inside the clip
BOX_MATCH_MS = 400                           # a box within this of a still's time is drawn on it
LEVEL_SOURCE = {"rule_tag": "#tag on the NX rule", "force_ack": "NX rule forces acknowledgement",
                "site": "site override", "tenant": "portal settings", "default": "default for this event type"}


class ExportError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Frame:
    offset_s: float
    ts_ms: int
    jpeg: bytes                      # original, untouched
    annotated: bytes                 # with analytics boxes drawn (same as jpeg when there are none)
    labels: list[str] = field(default_factory=list)
    source: str = "clip"             # clip | nx


@dataclass
class Evidence:
    clip: clips.ClipInfo | None = None
    clip_bytes: bytes = b""
    clip_sha256: str = ""
    event_frame: Frame | None = None
    frames: list[Frame] = field(default_factory=list)
    objects: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ gathering

async def full_clip(alarm: Alarm, pre: int, post: int, quality: str, timeout_s: float = 90) -> clips.ClipInfo:
    """The finished clip (not a growing one). Raises ExportError if it can't be had in time."""
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        info = await clips.get_clip(alarm, pre, post, quality, wait_s=max(0.5, min(10.0, remaining)))
        if info.status == "ready" and not info.partial:
            return info
        if info.status == "error":
            raise ExportError(f"The clip is unavailable: {info.message}", 502)
        wait = info.final_in_s or info.ready_in_s          # we need the finished clip, not the first part
        if wait and wait > remaining:
            raise ExportError(f"The clip is still being recorded. Try again in {math.ceil(wait)} s.", 409)
        if remaining <= 0:
            raise ExportError("Preparing the clip took too long. Try again.", 504)
        await asyncio.sleep(1)


async def _still(path: Path, at_s: float) -> bytes:
    s = get_settings()
    code, out, err = await clips._run(s.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-ss", f"{at_s:.3f}",
                                      "-i", str(path), "-frames:v", "1", "-q:v", "2", "-f", "image2", "-c:v", "mjpeg",
                                      "pipe:1", timeout=60)
    if code != 0 or not out:
        raise ExportError(f"Could not extract a frame: {err.decode(errors='replace')[-200:]}", 500)
    return out


def _boxes_at(objects: list[dict], ts_ms: int) -> list[tuple[float, float, float, float, str, bool]]:
    out = []
    for o in objects:
        best = min(o.get("boxes") or [], key=lambda b: abs(b[0] - ts_ms), default=None)
        if best and abs(best[0] - ts_ms) <= max(BOX_MATCH_MS, best[1]):
            out.append((best[2], best[3], best[4], best[5], o.get("label") or "Object", bool(o.get("primary"))))
    return out


def _annotate(jpeg: bytes, boxes: list) -> bytes:
    if not boxes:
        return jpeg
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    d = ImageDraw.Draw(img)
    w, h = img.size
    lw = max(2, w // 320)
    for x, y, bw, bh, label, primary in boxes:
        color = (224, 138, 48) if primary else (255, 255, 255)
        rect = [x * w, y * h, (x + bw) * w, (y + bh) * h]
        d.rectangle(rect, outline=color, width=lw)
        tw = d.textlength(label) + 8
        d.rectangle([rect[0], max(0, rect[1] - 14), rect[0] + tw, rect[1]], fill=color)
        d.text((rect[0] + 4, max(0, rect[1] - 13)), label, fill=(26, 26, 26))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


async def gather(alarm: Alarm, pre: int, post: int, quality: str, need_clip: bool) -> Evidence:
    """Clip, stills and analytics for the alarm. need_clip: fail instead of reporting without video."""
    from app.services.poller import manager   # late import: poller imports lots
    ev = Evidence()
    if not alarm.device_id:
        ev.problems.append("This alarm has no camera, so there is no video.")
        return ev
    try:
        ev.clip = await full_clip(alarm, pre, post, quality)
    except ExportError as exc:
        if need_clip or exc.status == 409:
            raise
        ev.problems.append(str(exc))
    start, end = clips.window(alarm, pre, post)
    try:
        ev.objects = await clips.objects(alarm, start, end)
    except Exception as exc:  # noqa: BLE001 — analytics are optional
        ev.problems.append(f"Analytics tracks unavailable ({exc.__class__.__name__}).")

    if ev.clip:
        path = clips.clip_path(ev.clip.url.rsplit("/", 1)[-1].removesuffix(".mp4"))
        ev.clip_bytes = await asyncio.to_thread(path.read_bytes)
        ev.clip_sha256 = await asyncio.to_thread(lambda: hashlib.sha256(ev.clip_bytes).hexdigest())
        dur = ev.clip.duration_ms / 1000
        wanted = [(o, alarm.event_ts_ms + o * 1000) for o in FRAME_OFFSETS_S]
        wanted = [(o, t, (t - ev.clip.start_ms) / 1000) for o, t in wanted]
        wanted = [(o, t, at) for o, t, at in wanted if 0 <= at <= dur - 0.1]
        stills = await asyncio.gather(*(_still(path, at) for _, _, at in wanted), return_exceptions=True)
        for (o, t, _), jpeg in zip(wanted, stills):
            if isinstance(jpeg, bytes):
                boxes = _boxes_at(ev.objects, t)
                ann = await asyncio.to_thread(_annotate, jpeg, boxes)
                ev.frames.append(Frame(o, t, jpeg, ann, sorted({b[4] for b in boxes})))
    try:   # NX's own frame at the exact event time, at a higher resolution than the SD clip
        jpeg = await manager.client_for(alarm.site).get_thumbnail(alarm.device_id, alarm.event_ts_ms, width=1280)
        boxes = _boxes_at(ev.objects, alarm.event_ts_ms)
        ev.event_frame = Frame(0, alarm.event_ts_ms, jpeg, await asyncio.to_thread(_annotate, jpeg, boxes),
                               sorted({b[4] for b in boxes}), "nx")
    except Exception as exc:  # noqa: BLE001
        at0 = next((f for f in ev.frames if f.offset_s == 0), None)
        if at0:
            ev.event_frame = at0
        else:
            ev.problems.append(f"Event-time frame unavailable ({exc.__class__.__name__}).")
    return ev


async def timeline(db: AsyncSession, alarm: Alarm) -> list[dict]:
    """What happened, in order: the event, its arrival, then everything the audit log recorded."""
    received_ms = int(alarm.received_at.replace(tzinfo=alarm.received_at.tzinfo or timezone.utc).timestamp() * 1000)
    rows = [{"ts": alarm.event_ts_ms, "what": "Event occurred in NX", "who": alarm.source_name or "NX",
             "detail": alarm.caption},
            {"ts": received_ms, "what": "Received by the portal", "who": "system",
             "detail": f"{max(0, received_ms - alarm.event_ts_ms):,} ms after the event"
                       + (". Site was disarmed: recorded, not raised" if alarm.state == "disarmed" else "")}]
    # The last arm/disarm of the site before the event says how it was armed at the time.
    arm = await db.scalar(select(AuditLog).where(
        AuditLog.site_id == alarm.site_id, AuditLog.action.in_(("site.armed", "site.disarmed")),
        AuditLog.ts <= datetime.fromtimestamp(alarm.event_ts_ms / 1000, timezone.utc)).order_by(AuditLog.id.desc()))
    if arm:
        rows.append(_audit_row(arm))
    for a in (await db.scalars(select(AuditLog).where(AuditLog.alarm_id == alarm.id).order_by(AuditLog.id))).all():
        if a.action != "alarm.received":
            rows.append(_audit_row(a))
    return sorted(rows, key=lambda r: r["ts"])


def _audit_row(a: AuditLog) -> dict:
    d = a.detail or {}
    ts = int(a.ts.replace(tzinfo=a.ts.tzinfo or timezone.utc).timestamp() * 1000)
    who = a.user.label if a.user else "system"
    if a.action == "alarm.acknowledged":
        nx = d.get("nx") or {}
        detail = f"NX write-back: {nx.get('method') or '-'}{' (failed: ' + str(nx.get('error')) + ')' if nx.get('ok') is False else ''}"
        verdict = {"real": "Real event. ", "false": "False alarm. "}.get(d.get("verdict", ""), "")
        return {"ts": ts, "what": "Acknowledged" + (" (bulk)" if d.get("bulk") else ""), "who": who, "detail": verdict + detail}
    if a.action == "alarm.note":
        text = d.get("text", "")
        return {"ts": ts, "what": "Note added", "who": who, "detail": text if len(text) <= 90 else text[:88] + "…"}
    if a.action == "alarm.verdict":
        name = {"real": "real event", "false": "false alarm", "": "not marked"}
        return {"ts": ts, "what": "Verdict changed" + (" (bulk)" if d.get("bulk") else ""), "who": who,
                "detail": f"{name.get(d.get('old', ''), d.get('old'))} → {name.get(d.get('new', ''), d.get('new'))}"
                          + (f" · “{d['note']}”" if d.get("note") else "")}
    if a.action == "alarm.exported":
        return {"ts": ts, "what": f"Exported ({d.get('format', '').upper()})", "who": who, "detail": d.get("note", "")}
    if a.action == "alarm.raised":
        return {"ts": ts, "what": "Raised by a #24h rule", "who": who, "detail": d.get("reason", "")}
    if a.action in ("site.armed", "site.disarmed"):
        how = {"manual": f"by {who}", "schedule": "by schedule", "timer": "disarm timer ran out"}.get(d.get("source"), "")
        note = f" · “{d['note']}”" if d.get("note") else ""
        return {"ts": ts, "what": f"Site {'armed' if a.action == 'site.armed' else 'disarmed'} (last change before the event)",
                "who": who, "detail": f"{how}{note}"}
    return {"ts": ts, "what": a.action, "who": who, "detail": json.dumps(d)[:200]}


# ------------------------------------------------------------------ PDF

def _fmt(ts_ms: int, tz) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz)
    return f"{dt:%Y-%m-%d %H:%M:%S}.{ts_ms % 1000:03d} {dt:%Z}"


def _styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle("base", fontName="Helvetica", fontSize=9.5, leading=13, textColor=INK)
    return {
        "base": base,
        "title": ParagraphStyle("title", base, fontName="Helvetica-Bold", fontSize=18, leading=22, spaceAfter=2),
        "sub": ParagraphStyle("sub", base, fontSize=10.5, textColor=MUTED, spaceAfter=10),
        "h2": ParagraphStyle("h2", base, fontName="Helvetica-Bold", fontSize=11.5, leading=15, spaceBefore=12,
                             spaceAfter=6, textColor=INK, borderPadding=(0, 0, 3, 0)),
        "cap": ParagraphStyle("cap", base, fontSize=8, leading=10, textColor=MUTED),
        "key": ParagraphStyle("key", base, fontName="Helvetica-Bold", fontSize=8.5, textColor=MUTED),
        "mono": ParagraphStyle("mono", base, fontName="Courier", fontSize=8, leading=10),
        "note": ParagraphStyle("note", base, fontSize=10, leading=14, backColor=SOFT, borderPadding=8,
                               spaceBefore=12, spaceAfter=14, leftIndent=8, rightIndent=8),
    }


def _p(text: str, style) -> Paragraph:
    return Paragraph(escape(str(text)).replace("\n", "<br/>"), style)


def _h2(text: str, st) -> Table:
    """Section heading with the TWG red rule underneath."""
    t = Table([[Paragraph(escape(text), st["h2"])]], colWidths=[7.3 * inch])
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 1.5, RED), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 8)]))
    return t


def _image(jpeg: bytes, width: float) -> RLImage:
    with Image.open(io.BytesIO(jpeg)) as im:
        w, h = im.size
    return RLImage(io.BytesIO(jpeg), width=width, height=width * h / w)


def _kv(rows: list[tuple[str, str]], st) -> Table:
    t = Table([[_p(k, st["key"]), _p(v, st["base"])] for k, v in rows], colWidths=[1.55 * inch, 5.75 * inch])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
                           ("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 4),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    return t


class _NumberedCanvas(rl_canvas.Canvas):
    """Adds the header, footer and "Page n of m" once the page count is known."""

    header: dict = {}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._pages = []

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self._decorate(total)
            super().showPage()
        super().save()

    def _decorate(self, total: int) -> None:
        h = self.header
        w, ht = letter
        if LOGO.exists():
            self.drawImage(str(LOGO), 0.6 * inch, ht - 0.85 * inch, width=1.5 * inch, height=0.46 * inch,
                           mask="auto", preserveAspectRatio=True)
        self.setFillColor(INK)
        self.setFont("Helvetica-Bold", 11)
        self.drawRightString(w - 0.6 * inch, ht - 0.58 * inch, "INCIDENT REPORT")
        self.setFont("Helvetica", 8.5)
        self.setFillColor(MUTED)
        self.drawRightString(w - 0.6 * inch, ht - 0.75 * inch, h.get("ref", ""))
        self.setStrokeColor(RED)
        self.setLineWidth(2)
        self.line(0.6 * inch, ht - 0.95 * inch, w - 0.6 * inch, ht - 0.95 * inch)
        self.setStrokeColor(RULE)
        self.setLineWidth(0.5)
        self.line(0.6 * inch, 0.62 * inch, w - 0.6 * inch, 0.62 * inch)
        self.setFont("Helvetica", 7.5)
        self.setFillColor(MUTED)
        self.drawString(0.6 * inch, 0.47 * inch, "TWG Security · Confidential: contains security video evidence")
        self.drawString(0.6 * inch, 0.33 * inch, h.get("generated", ""))
        self.drawRightString(w - 0.6 * inch, 0.47 * inch, f"Page {self._pageNumber} of {total}")


def build_pdf(alarm: Alarm, ev: Evidence, events: list[dict], user: User, note: str, pre: int, post: int,
              quality: str, now_ms: int, notes: list[dict] | None = None) -> bytes:
    st = _styles()
    site = alarm.site
    tz = arming.zone(site.timezone if site else "")
    level = LEVEL_NAMES.get(alarm.priority, "warning").title()
    ref = f"Alarm #{alarm.id} · {site.name if site else ''}"
    story: list = [Spacer(1, 4), _p(alarm.caption or alarm.event_type, st["title"]),
                   _p(" · ".join(x for x in (site.name if site else "", alarm.source_name, f"Level: {level}") if x), st["sub"])]

    if alarm.state == "acknowledged":
        status = f"Acknowledged by {alarm.acked_by.label if alarm.acked_by else '-'} at {_fmt(_ms(alarm.acked_at), tz)}"
    elif alarm.state == "disarmed":
        status = "Received while the site was disarmed: recorded, not raised to operators"
    else:
        status = "Open (not yet acknowledged)"
    nx = alarm.nx_ack_result or {}
    rows = [("Site", site.name if site else "-"), ("Address", site.address if site and site.address else "-"),
            ("Camera", alarm.source_name or "-"),
            ("Event", f"{alarm.event_type}{' · ' + alarm.event_subtype if alarm.event_subtype else ''}"),
            ("Level", f"{level} ({LEVEL_SOURCE.get(alarm.level_source, 'event type')})"),
            ("Event time", f"{_fmt(alarm.event_ts_ms, tz)}   ({_fmt(alarm.event_ts_ms, timezone.utc)})"),
            ("Received", f"{_fmt(_ms(alarm.received_at), tz)}  · {max(0, _ms(alarm.received_at) - alarm.event_ts_ms):,} ms after the event"),
            ("Status", status),
            ("Verdict", {"real": "REAL EVENT", "false": "FALSE ALARM"}.get(alarm.verdict, "Not marked")
             + (f" (marked by {alarm.verdict_by.label} at {_fmt(_ms(alarm.verdict_at), tz)})" if alarm.verdict and alarm.verdict_by else ""))]
    if nx:
        rows.append(("NX write-back", f"{nx.get('method', '-')}{'' if nx.get('ok', True) else ' · FAILED: ' + str(nx.get('error'))}"))
    if alarm.description:
        rows.append(("Description", alarm.description))
    story.append(_kv(rows, st))

    if ev.event_frame:
        f = ev.event_frame
        story += [_h2("Event-time frame", st), Spacer(1, 4), _image(f.annotated, 6.0 * inch),
                  _p(f"{_fmt(f.ts_ms, tz)} · {'NX archive frame' if f.source == 'nx' else 'from the clip'}"
                     + (f" · detected: {', '.join(f.labels)}" if f.labels else ""), st["cap"])]

    if ev.frames:
        cells, row = [], []
        for f in ev.frames:
            sign = "+" if f.offset_s > 0 else ""
            row.append([_image(f.annotated, 2.35 * inch),
                        _p(f"{sign}{f.offset_s:g} s · {_fmt(f.ts_ms, tz)}" + (f" · {', '.join(f.labels)}" if f.labels else ""), st["cap"])])
            if len(row) == 3:
                cells.append(row)
                row = []
        if row:
            cells.append(row + [""] * (3 - len(row)))
        grid = Table(cells, colWidths=[2.43 * inch] * 3)
        grid.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                  ("RIGHTPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
        story += [KeepTogether([_h2("Sequence from the clip", st), grid])]

    if ev.objects:
        obj_rows = [[_p("Object", st["key"]), _p("Seen", st["key"]), _p("Attributes", st["key"])]]
        for o in ev.objects[:12]:
            seen = f"{_fmt(o['start_ms'], tz)[11:23]} – {_fmt(o['end_ms'], tz)[11:23]}" if o.get("start_ms") and o.get("end_ms") else "-"
            attrs = ", ".join(f"{k}: {v}" for k, v in (o.get("attributes") or {}).items()) or "-"
            obj_rows.append([_p(o["label"] + (" (event's own)" if o.get("primary") else ""), st["base"]), _p(seen, st["base"]),
                             _p(attrs, st["base"])])
        t = Table(obj_rows, colWidths=[1.8 * inch, 2.1 * inch, 3.4 * inch], repeatRows=1)
        t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        story += [_h2("Detected by analytics", st), t]

    story.append(_h2("Timeline", st))
    trows = [[_p("Time", st["key"]), _p("What", st["key"]), _p("Who", st["key"]), _p("Detail", st["key"])]]
    day = datetime.fromtimestamp(alarm.event_ts_ms / 1000, tz).date()
    for e in events:
        when = _fmt(e["ts"], tz)
        if datetime.fromtimestamp(e["ts"] / 1000, tz).date() == day:
            when = when[11:]                      # same day as the event: time only (the date is in the summary)
        trows.append([_p(when, st["base"]), _p(e["what"], st["base"]), _p(e["who"], st["base"]),
                      _p(e["detail"], st["base"])])
    t = Table(trows, colWidths=[1.45 * inch, 2.0 * inch, 1.3 * inch, 2.55 * inch], repeatRows=1)
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 0), ("FONTSIZE", (0, 0), (-1, -1), 8.5)]))
    story.append(t)

    story.append(_h2("Operator notes", st))
    if alarm.ack_note:
        story += [_p(f"Disposition at acknowledgement, by {alarm.acked_by.label if alarm.acked_by else '-'} at "
                     f"{_fmt(_ms(alarm.acked_at), tz) if alarm.acked_at else '-'}", st["cap"]), _p(alarm.ack_note, st["note"])]
    elif alarm.state == "acknowledged":
        story.append(_p("Acknowledged without a disposition note.", st["base"]))
    elif not notes:
        story.append(_p("No disposition yet: the alarm has not been acknowledged.", st["base"]))
    for n in notes or []:
        story += [_p(f"Follow-up, by {n['by']} at {_fmt(n['ts'], tz)}", st["cap"]), _p(n["text"], st["note"])]
    if note.strip():
        story += [_p(f"Export note, by {user.label}", st["cap"]), _p(note.strip(), st["note"])]

    evidence: list = [_h2("Evidence", st)]
    erows = []
    if ev.clip:
        c = ev.clip
        erows += [("Clip window", f"{_fmt(c.start_ms, tz)} – {_fmt(c.start_ms + c.duration_ms, tz)[11:]} "
                                  f"({c.duration_ms / 1000:.1f} s; requested −{pre} s / +{post} s around the event)"),
                  ("Clip format", f"MP4, H.264{' (converted from ' + c.codec.upper() + ')' if c.transcoded else ''}, "
                                  f"{'HD' if quality == 'hd' else 'SD'}, {len(ev.clip_bytes) / 1_048_576:.1f} MB"),
                  ("Clip SHA-256", ev.clip_sha256)]
    erows += [("Stills", f"{len(ev.frames)} from the clip" + (", plus the NX event-time frame" if ev.event_frame and ev.event_frame.source == "nx" else "")
               + ". Orange boxes: the event's own object; white: other objects the analytics saw."),
              ("Source", f"Nx Witness site “{site.nx_site_name or site.name}”, recorded video retrieved by the TWG Alarm Portal" if site else "-"),
              ("Exported", f"{_fmt(now_ms, tz)} by {user.label}")]
    evidence.append(_kv(erows, st))
    story.append(KeepTogether(evidence))
    if ev.problems:
        story += [_h2("Notes on this export", st)] + [_p("• " + p, st["base"]) for p in ev.problems]

    buf = io.BytesIO()
    _NumberedCanvas.header = {"ref": ref, "generated": f"Generated {_fmt(now_ms, tz)} by {user.label}"}
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.6 * inch, rightMargin=0.6 * inch,
                            topMargin=1.15 * inch, bottomMargin=0.85 * inch,
                            title=f"Incident report: alarm {alarm.id}", author="TWG Security", subject=alarm.caption)
    doc.build(story, canvasmaker=_NumberedCanvas)
    return buf.getvalue()


def _ms(dt: datetime | None) -> int:
    if dt is None:
        return 0
    return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp() * 1000)


# ------------------------------------------------------------------ package

def build_zip(alarm: Alarm, ev: Evidence, pdf: bytes, user: User, note: str, now_ms: int,
              notes: list[dict] | None = None) -> tuple[bytes, dict]:
    tz = arming.zone(alarm.site.timezone if alarm.site else "")
    root = f"alarm-{alarm.id}_{datetime.fromtimestamp(alarm.event_ts_ms / 1000, tz):%Y-%m-%d_%H%M%S}"
    files: dict[str, bytes] = {"incident-report.pdf": pdf}
    if ev.clip_bytes:
        files["clip.mp4"] = ev.clip_bytes
    stamp = lambda f: datetime.fromtimestamp(f.ts_ms / 1000, tz).strftime("%H-%M-%S") + f".{f.ts_ms % 1000:03d}"  # noqa: E731
    if ev.event_frame and ev.event_frame.source == "nx":
        files[f"frames/00_event_{stamp(ev.event_frame)}.jpg"] = ev.event_frame.jpeg
        if ev.event_frame.annotated is not ev.event_frame.jpeg:
            files[f"frames/annotated/00_event_{stamp(ev.event_frame)}.jpg"] = ev.event_frame.annotated
    for i, f in enumerate(ev.frames, 1):
        name = f"{i:02d}_{'+' if f.offset_s > 0 else ''}{f.offset_s:g}s_{stamp(f)}.jpg"
        files[f"frames/{name}"] = f.jpeg
        if f.annotated is not f.jpeg:
            files[f"frames/annotated/{name}"] = f.annotated
    sums = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    manifest = {
        "alarm_id": alarm.id, "site": alarm.site.name if alarm.site else "", "camera": alarm.source_name,
        "caption": alarm.caption, "event_type": alarm.event_type, "event_ts_ms": alarm.event_ts_ms,
        "event_time_utc": _fmt(alarm.event_ts_ms, timezone.utc), "event_time_site": _fmt(alarm.event_ts_ms, tz),
        "state": alarm.state, "verdict": alarm.verdict or None, "ack_note": alarm.ack_note, "acked_by": alarm.acked_by.label if alarm.acked_by else None,
        "clip": ev.clip.to_dict() if ev.clip else None, "exported_by": user.label, "exported_at_ms": now_ms,
        "notes": [{"by": n["by"], "at_ms": n["ts"], "text": n["text"]} for n in notes or []],
        "export_note": note, "files": sums,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            # Video and JPEGs don't compress; storing keeps the export fast.
            z.writestr(f"{root}/{name}", data, zipfile.ZIP_DEFLATED if name.endswith(".pdf") else zipfile.ZIP_STORED)
        z.writestr(f"{root}/manifest.json", json.dumps(manifest, indent=2))
        z.writestr(f"{root}/SHA256SUMS.txt", "".join(f"{h}  {n}\n" for n, h in sums.items()))
    return buf.getvalue(), {"root": root, "sha256": sums}


async def export(db: AsyncSession, alarm: Alarm, user: User, fmt: str, note: str, pre: int | None, post: int | None,
                 quality: str) -> tuple[bytes, str, str, dict]:
    """Returns (bytes, filename, media type, audit detail)."""
    pre, post = clips.normalize(pre, post)
    ev = await gather(alarm, pre, post, quality, need_clip=fmt == "zip")
    events = await timeline(db, alarm)
    notes = [{"by": n.user.label if n.user else "system", "ts": _ms(n.created_at), "text": n.text}
             for n in (await db.scalars(select(AlarmNote).where(AlarmNote.alarm_id == alarm.id).order_by(AlarmNote.id))).all()]
    now = int(time.time() * 1000)
    pdf = await asyncio.to_thread(build_pdf, alarm, ev, events, user, note, pre, post, quality, now, notes)
    detail = {"format": fmt, "note": note, "window": [pre, post], "quality": quality,
              "clip_sha256": ev.clip_sha256 or None, "pdf_sha256": hashlib.sha256(pdf).hexdigest()}
    if fmt == "pdf":
        return pdf, f"incident-report_alarm-{alarm.id}.pdf", "application/pdf", detail
    data, info = await asyncio.to_thread(build_zip, alarm, ev, pdf, user, note, now, notes)
    detail["zip_sha256"] = hashlib.sha256(data).hexdigest()
    return data, f"{info['root']}.zip", "application/zip", detail
