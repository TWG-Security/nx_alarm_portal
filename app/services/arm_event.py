"""Tell NX when a site arms or disarms: a Generic Event (the user, 2026-11-06: "if we arm the system we not only want
#arm to disable or enable events in NX we also want it to be able to trigger a generic event into NX"; then "i want
both and the option arm rules"). Per site, each one optional (Edit site → Arming):
  * rules: NX rules tagged #arm off while disarmed (app/services/arm_rules.py; on by default; off = they stay on)
  * enabled: the Generic Event below (off by default)
  * webhooks (Sites → Webhooks to NX) ticked "Run when the site arms" / "disarms": a soft trigger or their own
    generic event, with {arming} (armed / disarmed), {by} and {note} for their templates

Per site (Edit site → Arming → "Tell NX when this site arms or disarms"; off by default): every change of state, by
the Arm/Disarm buttons (website, apps, MCP), the weekly schedule or a disarm timer running out, sends
    Source "TWG Alarm Portal", Caption "Site armed" / "Site disarmed", Description who / why / until when
to the site's NX, so its rules can react (chirp a siren, flash a strobe, switch an Axis virtual input...). Source and
both captions can be changed per site. "Site armed" is not part of "Site disarmed", so NX's "Caption contains" filter
tells them apart.

Order with the #arm rules (app/services/arm_rules.py), so a reacting rule that is itself tagged #arm still gets it:
arming switches the #arm rules on first and then sends "armed"; disarming sends "disarmed" first and then switches
them off. A send is tried for up to 5 times (2 / 5 / 10 / 30 s apart) and dropped if the site has changed state again
meanwhile; it never holds up arming itself or any alarm. Audit "site.arm_event" (ok, ms, why not).

NX logs the event only if one of its rules fires on it; the portal then doesn't raise its own message as an alarm
(is_echo, checked in poller.ingest): the site's own Generic Event always, and each soft trigger / webhook event sent
for an arm or disarm once (the first event NX logs for it within ECHO_S; a real press after that raises as usual).
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

from app.audit import audit
from app.db import sessionmaker
from app.models import Site
from app.services import arming

log = logging.getLogger("portal.arm_event")

DEFAULT = {"rules": True, "enabled": False, "source": "TWG Alarm Portal", "armed": "Site armed", "disarmed": "Site disarmed"}
RETRY_S = (2, 5, 10, 30)
RULES_WAIT_S = 10.0
ECHO_S = 30.0
_tasks: set[asyncio.Task] = set()
_expect: dict[tuple[int, str], dict] = {}     # (site id, "trigger:<id>" | "generic:<source>|<caption>") -> {until, key}


def config(site) -> dict:
    return {**DEFAULT, **(site.arm_event or {})}


def clean(cfg: dict) -> dict:
    """Edit site's settings, checked. Raises ValueError."""
    out = {"rules": bool(cfg.get("rules", True)), "enabled": bool(cfg.get("enabled"))}
    for k in ("source", "armed", "disarmed"):
        v = str(cfg.get(k) if cfg.get(k) is not None else DEFAULT[k]).strip()[:200]
        out[k] = v or DEFAULT[k]
    a, d = out["armed"].lower(), out["disarmed"].lower()
    if a == d or a in d or d in a:
        raise ValueError("The armed and disarmed captions must differ, and neither may contain the other "
                         "(NX matches \"Caption contains\")")
    return out


def is_echo(site, row: dict) -> bool:
    """The site's own arm/disarm message coming back through NX's event log."""
    ev = row.get("eventData") or {}
    if ev.get("type") == "generic" and (site.arm_event or {}).get("enabled"):
        cfg = config(site)
        if (ev.get("source") or "") == cfg["source"] and (ev.get("caption") or "") in (cfg["armed"], cfg["disarmed"]):
            return True
    if ev.get("type") == "softTrigger":
        what = "trigger:" + str(ev.get("triggerId") or "").strip("{}").lower()
    elif ev.get("type") == "generic":
        what = f"generic:{ev.get('source') or ''}|{ev.get('caption') or ''}"
    else:
        return False
    exp = _expect.get((site.id, what))
    if exp is None or time.monotonic() > exp["until"]:
        return False
    from app.services.nx_events import event_key
    key = event_key(row)
    if exp["key"] is None:
        exp["key"] = key                      # NX logs one row per rule that fired: they all share this key
    return exp["key"] == key


def _expect_echo(site_id: int, hook, sent: dict) -> None:
    if hook.kind == "trigger":
        what = "trigger:" + sent["trigger_id"].lower()
    else:
        what = f"generic:{sent.get('source', '')}|{sent.get('caption', '')}"
    _expect[(site_id, what)] = {"until": time.monotonic() + ECHO_S, "key": None}


def changed(manager, site, armed: bool, how: str, by: str = "", note: str = "", until_ms: int | None = None) -> None:
    """The site just armed or disarmed. Switches its #arm rules and, if set up, sends the event, in that order."""
    t = asyncio.create_task(_run(manager, site.id, armed, how, by, note, until_ms), name=f"arm-event-site-{site.id}")
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def _text(cfg: dict, armed: bool, how: str, by: str, note: str, until_ms: int | None, tz: str | None) -> str:
    from datetime import datetime
    who = by if how == "manual" and by else {"schedule": "the schedule", "timer": "the disarm timer ending",
                                             "manual": "an operator"}.get(how, how)
    bits = [f"{'Armed' if armed else 'Disarmed'} by {who}"]
    if until_ms:
        bits.append("re-arms at " + datetime.fromtimestamp(until_ms / 1000, arming.zone(tz)).strftime("%a %H:%M"))
    if note:
        bits.append(note)
    return " · ".join(bits)[:1000]


async def _run(manager, site_id: int, armed: bool, how: str, by: str, note: str, until_ms: int | None) -> None:
    from app.webhooks import note as note_call
    from app.webhooks import send
    try:
        if armed:
            manager.kick_arm_rules(site_id)
            await _rules_settled(manager, site_id, True)
        site = await _load(site_id)
        jobs = await _jobs(site, armed, how, by, note, until_ms) if site is not None else []
        if not jobs:
            if not armed:
                manager.kick_arm_rules(site_id)
            return
        client = manager.client_for(site)
        results: dict[int, tuple[dict, int]] = {}
        pending = list(range(len(jobs)))
        for tries, wait in enumerate((0, *RETRY_S), 1):
            await asyncio.sleep(wait)
            if tries > 1:
                site = await _load(site_id) or site
                if arming.site_state(site, int(time.time() * 1000)).armed != armed:
                    for i in pending:
                        results[i] = ({"ok": False, "ms": 0, "detail": "not sent: the site changed state again",
                                       "sent": jobs[i]["fields"]}, tries - 1)
                    break
            for i in pending:
                if jobs[i].get("webhook_id"):              # its event, logged back by NX, isn't an alarm
                    from app.webhooks import request_for
                    _expect_echo(site_id, jobs[i]["hook"], request_for(jobs[i]["hook"], jobs[i]["fields"]))
            outs = await asyncio.gather(*(send(client, jobs[i]["hook"], jobs[i]["fields"]) for i in pending))
            for i, out in zip(pending, outs):
                results[i] = (out, tries)
            if not armed and tries == 1:
                manager.kick_arm_rules(site_id)            # disarm: tell NX first, then the #arm rules off
            pending = [i for i, out in zip(pending, outs) if not out["ok"]]
            if not pending:
                break
        async with sessionmaker()() as db:
            for i, job in enumerate(jobs):
                result, tries = results[i]
                log.info("site %s: %s → %s %s in %s ms (try %d)%s", site_id, "armed" if armed else "disarmed", job["label"],
                         "sent" if result["ok"] else "NOT sent", result["ms"], tries,
                         f": {result['detail']}" if result["detail"] else "")
                audit(db, site.tenant_id, "site.arm_event", site_id=site_id, armed=armed, ok=result["ok"], ms=result["ms"],
                      tries=tries, detail=result["detail"], caption=job["label"], webhook_id=job.get("webhook_id"))
                if job.get("webhook_id"):
                    from app.models import Webhook
                    h = await db.get(Webhook, job["webhook_id"])
                    if h is not None:
                        note_call(h, result, f"site {'armed' if armed else 'disarmed'}")
            await db.commit()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - never let it touch arming or alarms
        log.exception("site %s: arm/disarm event failed", site_id)
        manager.kick_arm_rules(site_id)


async def _jobs(site, armed: bool, how: str, by: str, note: str, until_ms: int | None) -> list[dict]:
    """What to send for this change: the site's Generic Event (if on) and its webhooks ticked for it."""
    from sqlalchemy import select

    from app.models import Webhook
    cfg = config(site)
    jobs = []
    if cfg["enabled"]:
        caption = cfg["armed" if armed else "disarmed"]
        jobs.append({"label": caption,
                     "hook": SimpleNamespace(kind="generic", name=cfg["source"], config={
                         "source": "", "caption": "", "description": "", "state": "instant", "device_ids": []}),
                     "fields": {"source": cfg["source"], "caption": caption,
                                "description": _text(cfg, armed, how, by, note, until_ms, site.timezone)}})
    async with sessionmaker()() as db:
        hooks = (await db.scalars(select(Webhook).where(Webhook.site_id == site.id, Webhook.enabled.is_(True))
                                  .order_by(Webhook.id))).all()
    who = _text(cfg, armed, how, by, "", None, None).split(" by ", 1)[1]
    for h in hooks:
        if (h.config or {}).get("on_arm" if armed else "on_disarm"):
            jobs.append({"label": h.name, "hook": h, "webhook_id": h.id,
                         "fields": {"arming": "armed" if armed else "disarmed", "by": who, "note": note}})
    return jobs


async def _load(site_id: int):
    async with sessionmaker()() as db:
        return await db.get(Site, site_id)


async def _rules_settled(manager, site_id: int, armed: bool) -> None:
    """Wait (at most RULES_WAIT_S) until the site's #arm rules are switched for this state, if it has any."""
    rt = manager._rt.get(site_id)
    end = time.monotonic() + RULES_WAIT_S
    await asyncio.sleep(0.05)
    while rt is not None and time.monotonic() < end:
        r = rt.arm_rules or {}
        if not r.get("rules") or (r.get("armed") == armed and r.get("state") in ("ok", "failed")):
            return
        await asyncio.sleep(0.05)
