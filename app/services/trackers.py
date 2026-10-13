"""Live guard / patrol-vehicle locations: polls Peplink InControl2 for each tracker's GPS position and pushes
moves to browsers (bus event "tracker.moved").

Alarm safety: this is one background task with timeouts on every call; it never holds a DB connection while
waiting on InControl2 (fetch first, then one short write), and a failing or slow InControl2 only marks the
trackers stale. Positions come about every 1-2 minutes from IC2, so polling faster than POLL_MIN_S is pointless.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app import peplink
from app.db import sessionmaker
from app.models import PlatformSettings, Tracker, TrackerPoint
from app.security import decrypt
from app.services.bus import bus

log = logging.getLogger("portal.trackers")

POLL_MIN_S = 15
CONCURRENCY = 4
TRAIL_KEEP_DAYS = 7
MIN_MOVE_M = 3.0             # a new trail point when it moved this far...
POINT_EVERY_S = 300          # ...or this long since the last one (shows it's parked, not lost)
ORG_FALLBACK_S = 3600        # after an org-wide call fails, ask per device for this long

_client: tuple[tuple[str, str], peplink.Client] | None = None
_per_device_orgs: dict[str, float] = {}         # org id -> until (monotonic): use per-device calls
_last_prune: float | None = None
transport = None                                # tests: an httpx transport for the IC2 client


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def metres(a_lat, a_lng, b_lat, b_lng) -> float:
    k = math.cos(math.radians((a_lat + b_lat) / 2))
    return math.hypot((b_lat - a_lat) * 111_320, (b_lng - a_lng) * 111_320 * k)


def bearing(a_lat, a_lng, b_lat, b_lng) -> float:
    return (math.degrees(math.atan2((b_lng - a_lng) * math.cos(math.radians(a_lat)), b_lat - a_lat)) + 360) % 360


def tracker_dict(t: Tracker) -> dict:
    return {"id": t.id, "tenant_id": t.tenant_id, "name": t.name, "kind": t.kind, "enabled": t.enabled,
            "lat": t.lat, "lng": t.lng, "speed": t.speed, "heading": t.heading,
            "fix_at": aware(t.fix_at).isoformat() if t.fix_at else None,
            "checked_at": aware(t.checked_at).isoformat() if t.checked_at else None,
            "error": t.error, "serial": t.serial, "device_name": t.device_name}


def client_for(row: PlatformSettings) -> peplink.Client | None:
    global _client
    if not (row.peplink_client_id and row.peplink_secret_enc):
        return None
    key = (row.peplink_client_id, row.peplink_secret_enc)
    if _client is None or _client[0] != key:
        if _client is not None:
            asyncio.create_task(_client[1].close())
        _client = (key, peplink.Client(row.peplink_client_id, decrypt(row.peplink_secret_enc), transport=transport))
    return _client[1]


async def _org_points(client: peplink.Client, org: str) -> dict[int, list] | None:
    """{device id: points} for a whole organization in one call, or None to fall back to per-device calls."""
    if _per_device_orgs.get(org, 0) > time.monotonic():
        return None
    try:
        return {int(d["id"]): d["points"] for d in await client.org_locations(org) if d.get("id") is not None}
    except peplink.PeplinkError as e:
        log.info("IC2 org %s: org-wide locations unavailable (%s); asking per device", org, e)
        _per_device_orgs[org] = time.monotonic() + ORG_FALLBACK_S
        return None


async def _fetch(client: peplink.Client, t: dict, org_points: dict | None, slots: asyncio.Semaphore) -> dict:
    """One tracker's newest fix: {"id", "tz", "fix"} or {"id", "error"}."""
    async with slots:
        try:
            tz = t["tz"]
            if not tz:
                tz = (await client.device(t["org"], t["group"], t["device"])).get("group_timezone_label") or "UTC"
            points = org_points.get(t["device"]) if org_points is not None else None
            if points is None:
                points = await client.device_points(t["org"], t["group"], t["device"])
            return {"id": t["id"], "tz": tz, "fix": peplink.newest_fix(points, tz)}
        except peplink.PeplinkError as e:
            return {"id": t["id"], "error": str(e)}


async def poll_once() -> float:
    """One round over every enabled tracker. Returns how long to wait before the next."""
    global _last_prune
    async with sessionmaker()() as db:
        row = await db.get(PlatformSettings, 1)
        if row is None or not row.peplink_enabled:
            return 30.0
        client = client_for(row)
        interval = float(max(POLL_MIN_S, row.peplink_poll_s or 30))
        trackers = [{"id": t.id, "org": t.ic2_org, "group": t.ic2_group, "device": t.ic2_device, "tz": t.tz}
                    for t in (await db.scalars(select(Tracker).where(Tracker.enabled.is_(True), Tracker.source == "peplink"))).all()]
    if client is None or not trackers:
        return interval
    slots = asyncio.Semaphore(CONCURRENCY)
    orgs = {t["org"] for t in trackers}
    org_points = dict(zip(orgs, await asyncio.gather(*(_org_points(client, o) for o in orgs))))
    results = await asyncio.gather(*(_fetch(client, t, org_points[t["org"]], slots) for t in trackers))
    await _store(results)
    if _last_prune is None or time.monotonic() - _last_prune > 3600:
        _last_prune = time.monotonic()
        async with sessionmaker()() as db:
            await db.execute(delete(TrackerPoint).where(TrackerPoint.ts < utcnow() - timedelta(days=TRAIL_KEEP_DAYS)))
            await db.commit()
    return interval


async def _store(results: list[dict]) -> None:
    now, moved, errors = utcnow(), [], []
    async with sessionmaker()() as db:
        row = await db.get(PlatformSettings, 1)
        for r in results:
            t = await db.get(Tracker, r["id"])
            if t is None:
                continue
            before = (t.error, t.fix_at)
            t.checked_at = now
            if "error" in r:
                t.error = r["error"][:300]
                errors.append(r["error"])
            else:
                t.error, t.tz = "", r["tz"]
                fix = r["fix"]
                if fix is None:
                    t.error = "The router hasn't reported a GPS position (no fix, or GPS is off)"
                elif t.fix_at is None or fix.at > aware(t.fix_at):
                    _move(db, t, fix)
            if (t.error, t.fix_at) != before:
                moved.append(t)
        if row is not None:
            if errors and len(errors) == len(results):
                row.peplink_last_error, row.peplink_last_error_at = errors[0][:500], now
            else:
                row.peplink_last_ok_at = now
        await db.commit()
        out = [(t.tenant_id, tracker_dict(t)) for t in moved]
    for tenant_id, data in out:
        bus.publish(tenant_id, "tracker.moved", data)


def _move(db, t: Tracker, fix: peplink.Fix) -> None:
    gap = metres(t.lat, t.lng, fix.lat, fix.lng) if t.lat is not None else None
    if gap is not None and gap >= MIN_MOVE_M:
        t.heading = round(bearing(t.lat, t.lng, fix.lat, fix.lng), 1)
    last_point = aware(t.fix_at)
    if gap is None or gap >= MIN_MOVE_M or last_point is None or (fix.at - last_point).total_seconds() >= POINT_EVERY_S:
        db.add(TrackerPoint(tracker_id=t.id, ts=fix.at, lat=fix.lat, lng=fix.lng, speed=fix.speed))
    t.lat, t.lng, t.speed, t.fix_at = fix.lat, fix.lng, fix.speed, fix.at


async def run() -> None:
    """The background loop (started in main.lifespan)."""
    await asyncio.sleep(3)
    while True:
        try:
            wait = await poll_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep tracking alive whatever happens
            log.exception("tracker poll failed")
            wait = 30.0
        await asyncio.sleep(wait)
