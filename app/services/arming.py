"""Per-site arming: armed / disarmed, by hand or on a weekly schedule.

Arming is decided by the portal; NX rules are never changed. While a site is disarmed its
security events (intrusion, line crossing, input signals, soft triggers...) are still stored,
with state "disarmed", but not raised: no feed card, no sound, no pop-up. System events
(site connection lost, storage, server failure...) always raise, and so do rules tagged
#24h in their NX Title/Comment (panic buttons that must work around the clock).

The armed state is a pure function of the site's settings and a moment in time, so it is
computed right when an alarm is ingested and never depends on a background job having run:

  * weekly schedule entries ("arm at 18:00 Mon-Fri", "disarm at 07:00 Mon-Fri"), in the
    site's time zone
  * the last manual arm/disarm (arm_override), optionally with an auto re-arm time
  * the latest of those at or before the moment wins; nothing at all -> armed

Editing a schedule never flips the state on the spot: occurrences before the edit
(schedule["since_ms"]) are ignored, so the change takes effect at the next scheduled time.
The scheduler loop below only announces changes (audit log + live update to browsers).
"""

import asyncio
import logging
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from app.config import get_settings

log = logging.getLogger("portal.arming")

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")      # index = date.weekday()
ACTIONS = ("arm", "disarm")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
WINDOW_DAYS = 8                     # covers a weekly schedule in both directions
MAX_ENTRIES = 50
MAX_DISARM_MINUTES = 7 * 24 * 60

RULE_24H = re.compile(r"(?<![\w#])#24h\b", re.I)


def rule_always_armed(comment: str | None) -> bool:
    """NX rule Title/Comment contains #24h: raise its alarms even while the site is disarmed."""
    return bool(RULE_24H.search(comment or ""))


def zone(name: str | None) -> ZoneInfo:
    for candidate in (name, get_settings().default_timezone, "UTC"):
        if candidate:
            try:
                return ZoneInfo(candidate)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return ZoneInfo("UTC")


def valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def clean_entries(entries: list[dict]) -> list[dict]:
    """Validate and normalise schedule entries. Raises ValueError with a readable message."""
    if len(entries) > MAX_ENTRIES:
        raise ValueError(f"At most {MAX_ENTRIES} schedule entries")
    out, seen = [], {}
    for i, e in enumerate(entries, 1):
        action = str(e.get("action", "")).lower()
        at = str(e.get("time", ""))
        days = sorted({str(d).lower() for d in e.get("days") or []})
        if action not in ACTIONS:
            raise ValueError(f"Entry {i}: action must be arm or disarm")
        if not TIME_RE.match(at):
            raise ValueError(f"Entry {i}: time must be HH:MM (24-hour)")
        if not days or any(d not in DAYS for d in days):
            raise ValueError(f"Entry {i}: pick at least one day")
        for d in days:
            other = seen.get((d, at))
            if other and other != action:
                raise ValueError(f"Entry {i}: {d.title()} {at} is set to both arm and disarm")
            seen[(d, at)] = action
        out.append({"action": action, "time": at, "days": [d for d in DAYS if d in days]})
    return sorted(out, key=lambda e: (e["time"], DAYS.index(e["days"][0])))


@dataclass(frozen=True)
class ArmState:
    armed: bool
    source: str = "default"          # default | manual | schedule | timer (a timed disarm ran out)
    since_ms: int | None = None
    by: str = ""
    note: str = ""
    until_ms: int | None = None      # a manual disarm re-arms by itself at this time
    next_armed: bool | None = None   # the next change that flips the state...
    next_ms: int | None = None       # ...and when
    next_source: str = ""

    def as_dict(self) -> dict:
        return {"armed": self.armed, "source": self.source, "since_ms": self.since_ms, "by": self.by,
                "note": self.note, "until_ms": self.until_ms, "next_armed": self.next_armed,
                "next_ms": self.next_ms, "next_source": self.next_source}


def _occurrences(schedule: dict | None, tz: ZoneInfo, lo_ms: int, hi_ms: int) -> list[tuple[int, bool]]:
    """Scheduled (time_ms, armed) events in [lo_ms, hi_ms], ignoring anything before the schedule was saved."""
    if not schedule or not schedule.get("entries"):
        return []
    since = int(schedule.get("since_ms") or 0)
    lo_ms = max(lo_ms, since)
    first = datetime.fromtimestamp(lo_ms / 1000, tz).date() - timedelta(days=1)
    last = datetime.fromtimestamp(hi_ms / 1000, tz).date() + timedelta(days=1)
    out = []
    day: date = first
    while day <= last:
        name = DAYS[day.weekday()]
        for e in schedule["entries"]:
            if name not in e["days"]:
                continue
            h, m = map(int, e["time"].split(":"))
            # A time skipped by a DST jump (e.g. 02:30) lands an hour later; a repeated one fires once.
            ts = int(datetime.combine(day, dtime(h, m), tzinfo=tz).timestamp() * 1000)
            if lo_ms <= ts <= hi_ms:
                out.append((ts, e["action"] == "arm"))
        day += timedelta(days=1)
    return sorted(set(out))


def state_at(schedule: dict | None, override: dict | None, tz_name: str | None, t_ms: int) -> ArmState:
    tz = zone(tz_name)
    span = WINDOW_DAYS * 86_400_000
    sched = _occurrences(schedule, tz, t_ms - span, t_ms + span)
    # (time, rank, armed, source, extra); rank breaks exact ties: a manual action beats the schedule.
    events: list[tuple[int, int, bool, str, dict]] = [(ts, 0, armed, "schedule", {}) for ts, armed in sched]
    if override and override.get("at_ms") is not None:
        at, until = int(override["at_ms"]), override.get("until_ms")
        events.append((at, 1, bool(override.get("armed")), "manual", override))
        # A timed disarm re-arms at until_ms, unless the schedule has already taken over by then.
        if until and not override.get("armed") and not any(at < ts < int(until) for ts, _ in sched):
            events.append((int(until), 1, True, "timer", {}))
    events.sort(key=lambda e: (e[0], e[1]))

    past = [e for e in events if e[0] <= t_ms]
    if past:
        ts, _, armed, source, extra = past[-1]
        cur = ArmState(armed, source, ts, extra.get("by", ""), extra.get("note", ""),
                       int(extra["until_ms"]) if source == "manual" and extra.get("until_ms") else None)
    else:
        cur = ArmState(True)
    nxt = next((e for e in events if e[0] > t_ms and e[2] != cur.armed), None)
    if nxt:
        cur = replace(cur, next_armed=nxt[2], next_ms=nxt[0], next_source=nxt[3])
    return cur


def site_state(site, t_ms: int) -> ArmState:
    return state_at(site.arm_schedule, site.arm_override, site.timezone, t_ms)


def armed_for_event(site, event_ts_ms: int, now_ms: int) -> bool:
    """Armed when the event happened OR when it reached us: when in doubt, raise it."""
    return site_state(site, event_ts_ms).armed or site_state(site, now_ms).armed


# ------------------------------------------------------------------ announcing changes

async def check_all(now_ms: int) -> int:
    """Store and announce every site whose computed state differs from what was last announced."""
    from app.audit import audit
    from app.db import sessionmaker
    from app.models import Site
    from app.services.bus import bus
    from app.services.serialize import site_dict

    changed = 0
    async with sessionmaker()() as db:
        sites = (await db.scalars(select(Site).where(Site.archived_at.is_(None)))).all()
        flips = []
        for s in sites:
            st = site_state(s, now_ms)
            if st.armed != s.armed:
                s.armed = st.armed
                audit(db, s.tenant_id, "site.armed" if st.armed else "site.disarmed", site_id=s.id,
                      source=st.source, at_ms=st.since_ms)
                flips.append(s)
        if flips:
            await db.commit()
            for s in flips:
                log.info("site %s %s by %s", s.id, "armed" if s.armed else "disarmed", site_state(s, now_ms).source)
                bus.publish(s.tenant_id, "site.updated", site_dict(s))
            changed = len(flips)
    return changed


async def run_scheduler() -> None:
    import time
    tick = get_settings().arm_tick_s
    while True:
        try:
            await check_all(int(time.time() * 1000))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — never let the loop die; alarm ingest doesn't depend on it
            log.exception("arming check failed")
        await asyncio.sleep(tick)
