"""Per-site event-log pollers.

Each enabled site gets one asyncio task that reads new NX event-log rows every few
seconds, turns qualifying rows into Alarm records, and pushes them onto the bus.
A failing site backs off on its own; it never blocks the others.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.config import get_settings
from app.db import sessionmaker
from app.models import Alarm, Site
from app.nx.client import NXClient
from app.security import decrypt
from app.services.alarm_filter import classify
from app.services.bus import bus
from app.services.nx_events import event_key, summarize
from app.services.serialize import alarm_dict
from app.services.sites import describe_http_error, make_client

log = logging.getLogger("portal.poller")


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class SiteRuntime:
    site_id: int
    tenant_id: int
    client: NXClient
    device_names: dict[str, str] = field(default_factory=dict)
    devices_refreshed: float = 0.0
    task: asyncio.Task | None = None


async def ingest(db: AsyncSession, site: Site, rows: list[dict], device_names: dict[str, str]) -> list[Alarm]:
    """Store qualifying rows as alarms. Returns only the newly created alarms."""
    candidates: dict[str, tuple[dict, object]] = {}
    for row in rows:
        c = classify(row, site.alarm_types)
        if not c.is_alarm:
            continue
        key = event_key(row)
        prev = candidates.get(key)
        # Several action rows per event: keep the one NX can acknowledge, if any.
        if prev is None or (row.get("actionData") or {}).get("acknowledge") and not (prev[0].get("actionData") or {}).get("acknowledge"):
            candidates[key] = (row, c)
    if not candidates:
        return []

    existing = {
        a.event_key: a
        for a in (await db.scalars(
            select(Alarm).where(Alarm.site_id == site.id, Alarm.event_key.in_(list(candidates)))
        )).all()
    }

    created: list[Alarm] = []
    for key, (row, c) in candidates.items():
        info = summarize(row, device_names)
        prior = existing.get(key)
        if prior is not None:
            if info["nx_ack_required"] and not prior.nx_ack_required and prior.state == "new":
                prior.nx_ack_required = True
                prior.nx_action_id = info["nx_action_id"]
                prior.nx_action_server_id = info["nx_action_server_id"]
                prior.priority = min(prior.priority, c.priority)
            continue
        alarm = Alarm(tenant_id=site.tenant_id, site_id=site.id, event_key=key,
                      category=c.category, priority=c.priority, raw=row, **info)
        alarm.site = site
        db.add(alarm)
        created.append(alarm)
    if created:
        await db.flush()
        for a in created:
            audit(db, site.tenant_id, "alarm.received", site_id=site.id, alarm_id=a.id,
                  event_type=a.event_type, caption=a.caption, priority=a.priority)
    return created


class PollerManager:
    def __init__(self) -> None:
        self._rt: dict[int, SiteRuntime] = {}

    # --- client access (also used by ack + media routes) -------------------
    def client_for(self, site: Site) -> NXClient:
        rt = self._rt.get(site.id)
        if rt is None:
            rt = self._runtime(site)
        return rt.client

    def _runtime(self, site: Site) -> SiteRuntime:
        client = make_client(site.host, site.nx_user, decrypt(site.nx_pass_enc))
        rt = SiteRuntime(site.id, site.tenant_id, client)
        self._rt[site.id] = rt
        return rt

    # --- lifecycle ----------------------------------------------------------
    async def start_all(self) -> None:
        async with sessionmaker()() as db:
            sites = (await db.scalars(
                select(Site).where(Site.enabled.is_(True), Site.archived_at.is_(None))
            )).all()
        for s in sites:
            self.start(s)
        log.info("started %d site pollers", len(sites))

    def start(self, site: Site) -> None:
        if not get_settings().start_pollers:
            return
        rt = self._rt.get(site.id) or self._runtime(site)
        if rt.task is None or rt.task.done():
            rt.task = asyncio.create_task(self._run(rt), name=f"poller-site-{site.id}")

    async def stop(self, site_id: int) -> None:
        rt = self._rt.pop(site_id, None)
        if rt is None:
            return
        if rt.task:
            rt.task.cancel()
            try:
                await rt.task
            except (asyncio.CancelledError, Exception):
                pass
        await rt.client.close()

    async def restart(self, site: Site) -> None:
        await self.stop(site.id)
        if site.enabled and site.archived_at is None:
            self.start(site)

    async def shutdown(self) -> None:
        for site_id in list(self._rt):
            await self.stop(site_id)

    # --- loop ---------------------------------------------------------------
    async def _run(self, rt: SiteRuntime) -> None:
        settings = get_settings()
        delay = settings.poll_interval_s
        while True:
            try:
                await self.poll_once(rt)
                delay = settings.poll_interval_s
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                err = describe_http_error(exc)
                log.warning("site %s poll failed: %s", rt.site_id, err)
                await self._set_status(rt, err.status, str(err))
                delay = min(max(delay * 2, settings.poll_interval_s), settings.poll_max_backoff_s)
            await asyncio.sleep(delay)

    async def poll_once(self, rt: SiteRuntime) -> list[Alarm]:
        settings = get_settings()
        async with sessionmaker()() as db:
            site = await db.get(Site, rt.site_id)
            if site is None or not site.enabled or site.archived_at is not None:
                return []

            if time.monotonic() - rt.devices_refreshed > settings.device_refresh_s:
                devices = await rt.client.list_devices() or []
                rt.device_names = {(d.get("id") or "").strip("{}"): d.get("name", "") for d in devices}
                rt.devices_refreshed = time.monotonic()
                site.camera_count = sum(1 for d in devices if (d.get("deviceType") or "Camera").lower() != "server")

            cursor = site.event_cursor_ms or (now_ms() - settings.initial_lookback_ms)
            rows = await rt.client.get_events(limit=500, from_ms=max(1, cursor - settings.poll_overlap_ms),
                                              descending=False) or []
            created = await ingest(db, site, rows, rt.device_names)
            site.event_cursor_ms = max([cursor] + [int(r.get("timestampMs") or 0) for r in rows])

            status_changed = site.status != "online"
            site.status, site.status_detail = "online", ""
            site.last_seen_at = datetime.now(timezone.utc)
            await db.commit()

            if status_changed:
                audit(db, site.tenant_id, "site.online", site_id=site.id)
                await db.commit()
                bus.publish(site.tenant_id, "site.status", {"site_id": site.id, "status": "online"})
            for a in created:
                bus.publish(site.tenant_id, "alarm.new", alarm_dict(a))
            return created

    async def _set_status(self, rt: SiteRuntime, status: str, detail: str) -> None:
        async with sessionmaker()() as db:
            site = await db.get(Site, rt.site_id)
            if site is None:
                return
            changed = site.status != status
            site.status, site.status_detail = status, detail[:1000]
            if changed:
                audit(db, site.tenant_id, f"site.{status}", site_id=site.id, detail_text=detail[:500])
            await db.commit()
            if changed:
                bus.publish(site.tenant_id, "site.status", {"site_id": site.id, "status": status, "detail": detail})


manager = PollerManager()
