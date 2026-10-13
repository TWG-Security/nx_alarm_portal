"""Live guard / patrol-vehicle locations: polls Peplink InControl2 for each tracker's GPS position and pushes
moves to browsers (bus event "tracker.moved").

Alarm safety: this is one background task with timeouts on every call; it never holds a DB connection while
waiting on InControl2 (fetch first, then one short write), and a failing or slow InControl2 only marks the
trackers stale. One call per tracker per round (the per-device location is live; IC2's org-wide location is a
stale cache, so it's only used to list routers).
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
from app.models import Tracker, TrackerPoint, TrackerSource
from app.security import decrypt
from app.services.bus import bus

log = logging.getLogger("portal.trackers")

POLL_MIN_S = 15
CONCURRENCY = 4
TRAIL_KEEP_DAYS = 7
MIN_MOVE_M = 3.0             # a new trail point when it moved this far...
POINT_EVERY_S = 300          # ...or this long since the last one (shows it's parked, not lost)

_clients: dict[int, tuple[tuple[str, str], peplink.Client]] = {}     # source id -> (credentials, client)
_last_prune: float | None = None
transport = None                                # tests: an httpx transport for the IC2 client
_wake: asyncio.Event | None = None
NAME_SYNC_S = 600                # router names are re-read from InControl2 this often (and on a manual refresh)
REFRESH_MIN_S = 5.0              # a manual refresh asks IC2 at most this often per tracker
_names_at: dict[tuple, float] = {}             # (tenant, org, group) -> last name read (monotonic)
_refreshed_at: dict[int, float] = {}           # tracker id -> last manual refresh


def wake() -> None:
    """Poll now (settings saved, a tracker added) instead of at the end of the current wait."""
    if _wake is not None:
        _wake.set()


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


def client_for(src: TrackerSource) -> peplink.Client | None:
    """The IC2 client for a company's credentials (kept between rounds for its access token)."""
    if not (src.client_id and src.secret_enc):
        return None
    key = (src.client_id, src.secret_enc)
    old = _clients.get(src.id)
    if old is None or old[0] != key:
        if old is not None:
            asyncio.create_task(old[1].close())
        _clients[src.id] = (key, peplink.Client(src.client_id, decrypt(src.secret_enc), transport=transport))
    return _clients[src.id][1]


async def _fetch(client: peplink.Client, t: dict, slots: asyncio.Semaphore) -> dict:
    """One tracker's newest fix: {"id", "tz", "fix"} or {"id", "error"}."""
    async with slots:
        try:
            points, tz = await client.device_points(t["org"], t["group"], t["device"])
            tz = tz or t["tz"]
            if not tz:
                tz = (await client.device(t["org"], t["group"], t["device"])).get("group_timezone_label") or "UTC"
            return {"id": t["id"], "tz": tz, "fix": peplink.newest_fix(points, tz)}
        except peplink.PeplinkError as e:
            return {"id": t["id"], "error": str(e)}


async def _names(client: peplink.Client, org: str, group: int) -> dict[int, str]:
    """Router names in one group (the group location list carries them; never the device list)."""
    try:
        return {int(d["id"]): d["name"] for d in await client.group_locations(org, group) if d.get("id") is not None and d.get("name")}
    except peplink.PeplinkError as e:
        log.info("IC2 names for %s/%s unavailable: %s", org, group, e)
        return {}


async def _with_names(clients: dict, trackers: list[dict], results: list[dict], force: bool = False) -> None:
    """Attach each router's current InControl2 name to its result, re-reading each group every NAME_SYNC_S."""
    now = time.monotonic()
    due = {(t["tenant"], t["org"], t["group"]) for t in trackers
           if force or now - _names_at.get((t["tenant"], t["org"], t["group"]), -1e9) > NAME_SYNC_S}
    if not due:
        return
    keys = list(due)
    found = await asyncio.gather(*(_names(clients[k[0]], k[1], k[2]) for k in keys))
    names = {}
    for k, got in zip(keys, found):
        if got:
            _names_at[k] = now
        names[k] = got
    for t, r in zip(trackers, results):
        name = names.get((t["tenant"], t["org"], t["group"]), {}).get(t["device"])
        if name:
            r["device_name"] = name


async def poll_once() -> float:
    """One round over every enabled tracker of every company whose tracking is on (each with its own
    InControl2 credentials). Returns how long to wait before the next."""
    global _last_prune
    async with sessionmaker()() as db:
        sources = {s.tenant_id: s for s in (await db.scalars(select(TrackerSource).where(
            TrackerSource.enabled.is_(True), TrackerSource.kind == "peplink"))).all()}
        clients = {tid: client_for(src) for tid, src in sources.items()}
        interval = float(max(POLL_MIN_S, min((s.poll_s or 30 for s in sources.values()), default=30)))
        trackers = [{"id": t.id, "tenant": t.tenant_id, "org": t.ic2_org, "group": t.ic2_group, "device": t.ic2_device, "tz": t.tz}
                    for t in (await db.scalars(select(Tracker).where(Tracker.enabled.is_(True), Tracker.source == "peplink",
                                                                     Tracker.tenant_id.in_(list(sources) or [0])))).all()]
    trackers = [t for t in trackers if clients.get(t["tenant"]) is not None]
    if not trackers:
        return interval
    slots = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*(_fetch(clients[t["tenant"]], t, slots) for t in trackers))
    for t, r in zip(trackers, results):
        r["tenant"] = t["tenant"]
    await _with_names(clients, trackers, results)
    await _store(results)
    if _last_prune is None or time.monotonic() - _last_prune > 3600:
        _last_prune = time.monotonic()
        async with sessionmaker()() as db:
            await db.execute(delete(TrackerPoint).where(TrackerPoint.ts < utcnow() - timedelta(days=TRAIL_KEEP_DAYS)))
            await db.commit()
    return interval


async def _store(results: list[dict]) -> None:
    now, moved = utcnow(), []
    errors: dict[int, list[str]] = {}
    counts: dict[int, int] = {}
    async with sessionmaker()() as db:
        for r in results:
            counts[r["tenant"]] = counts.get(r["tenant"], 0) + 1
            t = await db.get(Tracker, r["id"])
            if t is None:
                continue
            before = (t.error, t.fix_at, t.name)
            t.checked_at = now
            new_name = r.get("device_name")
            if new_name and new_name != t.device_name:
                # Renamed in InControl2: a guard still called by the router's name follows it; a name given in
                # the portal stays (only the router name shown under it changes).
                if t.device_name and t.name == t.device_name:
                    t.name = new_name[:100]
                log.info("tracker %s: router renamed %r -> %r", t.id, t.device_name, new_name)
                t.device_name = new_name[:200]
            if "error" in r:
                t.error = r["error"][:300]
                errors.setdefault(r["tenant"], []).append(r["error"])
            else:
                t.error, t.tz = "", r["tz"]
                fix = r["fix"]
                if fix is None:
                    t.error = "The router hasn't reported a GPS position (no fix, or GPS is off)"
                elif t.fix_at is None or fix.at > aware(t.fix_at):
                    _move(db, t, fix)
            if (t.error, t.fix_at, t.name) != before:
                moved.append(t)
        for src in (await db.scalars(select(TrackerSource).where(TrackerSource.tenant_id.in_(list(counts))))).all():
            errs = errors.get(src.tenant_id, [])
            if errs and len(errs) == counts[src.tenant_id]:          # every one of its trackers failed: the account's problem
                src.last_error, src.last_error_at = errs[0][:500], now
            else:
                src.last_ok_at = now
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


async def refresh_one(tracker_id: int) -> dict | None:
    """Ask InControl2 for one tracker's position (and router name) now: the map's / Sites page's refresh button."""
    async with sessionmaker()() as db:
        t = await db.get(Tracker, tracker_id)
        if t is None:
            return None
        src = await db.scalar(select(TrackerSource).where(TrackerSource.tenant_id == t.tenant_id, TrackerSource.kind == "peplink"))
        client = client_for(src) if src and src.enabled else None
        row = {"id": t.id, "tenant": t.tenant_id, "org": t.ic2_org, "group": t.ic2_group, "device": t.ic2_device, "tz": t.tz}
        current = tracker_dict(t)
    if client is None:
        raise peplink.PeplinkError("Tracking is switched off for this company (Sites → Guards & vehicles)")
    if time.monotonic() - _refreshed_at.get(tracker_id, -1e9) < REFRESH_MIN_S:
        return current
    _refreshed_at[tracker_id] = time.monotonic()
    result = await _fetch(client, row, asyncio.Semaphore(1))
    result["tenant"] = row["tenant"]
    await _with_names({row["tenant"]: client}, [row], [result], force=True)
    await _store([result])
    async with sessionmaker()() as db:
        return tracker_dict(await db.get(Tracker, tracker_id))


async def run() -> None:
    """The background loop (started in main.lifespan)."""
    global _wake
    _wake = asyncio.Event()
    await asyncio.sleep(3)
    while True:
        _wake.clear()                   # before the round: a wake-up during it means "go again right away"
        try:
            wait = await poll_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep tracking alive whatever happens
            log.exception("tracker poll failed")
            wait = 30.0
        try:
            await asyncio.wait_for(_wake.wait(), wait)
        except asyncio.TimeoutError:
            pass
