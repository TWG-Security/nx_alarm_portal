"""NX rules tagged #arm follow the site's arming (the user, 2026-11-06: arm/disarm should be able to "disable the rules
that have a specific # on them maybe #arm"; picked "the tag is the switch").

* Disarmed: every NX rule with #arm in its Title/Comment is switched off in NX; armed: switched back on. That stops
  what NX itself does (sirens, strobes, speakers, NX's own push notifications) while a site is disarmed. Untagged rules
  are never touched, and a rule also tagged #24h is never switched off (#24h wins). A rule can carry a level tag too
  ("Front door #arm #alarm"): while it is off NX doesn't log it, so it isn't listed under "While disarmed" either.
* The wanted state is arming.site_state(site, now), the same pure function ingest uses. One task per site applies it
  whenever it may differ: the site arms or disarms (button: kick() from the arm API; timer or schedule: kick() from
  arming.check_all's 1 s tick), or the rule list the poller reads every minute shows a tagged rule in the wrong state
  (a newly tagged rule, a change made in Nx Witness, a change missed while the portal was down).
* Each switch is rule_edit.edit(enabled=...): a partial update of `enabled` only, read back and checked; the rules
  are switched side by side so arming takes one NX round trip, not one per rule.
* It never sits in the alarm path. A failed switch is retried (2 s up to 60 s) for as long as it takes, and after
  FAIL_ALARM_AFTER failures in a row the site gets a system alarm (raised even while disarmed): a #arm rule left off
  after arming means its NX actions, and its portal alarms, don't happen. Not while the site is offline: its
  "Site connection lost" alarm already says nothing is being monitored, and the switch happens when NX is back.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from app.audit import audit
from app.db import sessionmaker
from app.models import Alarm, Site, Tenant
from app.services import arming
from app.services.alarm_filter import Policy
from app.services.bus import bus

log = logging.getLogger("portal.arm_rules")

RETRY_S = (2, 5, 10, 30, 60)
FAIL_ALARM_AFTER = 3
ALARM_ON, ALARM_OFF = "portalArmRulesOn", "portalArmRulesOff"


def tagged(rules: list[dict]) -> list[dict]:
    return [r for r in rules if arming.rule_follows_arming(r.get("comment"))]


def out_of_step(rules: list[dict], armed: bool) -> list[dict]:
    """Tagged rules whose on/off in NX isn't what the site's arming wants."""
    return [r for r in tagged(rules) if (r.get("enabled") is not False) != armed]


def _label(r: dict) -> str:
    from app.services.poller import _rule_label
    return _rule_label(r)


def _why(exc: BaseException) -> str:
    from app.services import rule_edit
    from app.services.sites import describe_http_error
    if isinstance(exc, rule_edit.RuleEditError):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return f"NX refused (HTTP {code}): the portal's NX account needs to be in Power Users or Administrators"
        return f"NX answered HTTP {code}"
    return str(describe_http_error(exc)) if isinstance(exc, httpx.HTTPError) else f"{type(exc).__name__}: {exc}"[:300]


async def _switch(rt, want: bool) -> tuple[list[str], list[str], list[dict]]:
    """Read the site's rules and switch the tagged ones that are out of step. Returns (tagged rule names, switched
    names, [{rule, error}]). Raises if the rules can't be read at all."""
    from app.services import rule_edit
    rules = await rt.client.get_rules() or []
    names = [_label(r) for r in tagged(rules)]
    todo = out_of_step(rules, want)
    client = rule_edit._Retrying(rt.client)

    async def one(r):
        await rule_edit.edit(client, (r.get("id") or "").strip("{}"), enabled=want)
    results = await asyncio.gather(*(one(r) for r in todo), return_exceptions=True)
    switched, failed = [], []
    for r, res in zip(todo, results):
        if isinstance(res, asyncio.CancelledError):
            raise res
        if isinstance(res, BaseException):
            failed.append({"rule": _label(r), "error": _why(res)})
        else:
            switched.append(_label(r))
            r["enabled"] = want
    if switched:                                 # the rules view shows it now; the poller re-reads the rest at once
        rt.rules, rt.rules_read_at, rt.rules_refreshed = rules, time.time(), 0.0
    return names, switched, failed


async def run(manager, rt) -> None:
    """The site's switching task (started and stopped with its poller)."""
    fails = 0
    while True:
        try:
            if fails:
                await asyncio.wait_for(rt.arm_kick.wait(), RETRY_S[min(fails, len(RETRY_S)) - 1])
            else:
                await rt.arm_kick.wait()
        except TimeoutError:
            pass
        rt.arm_kick.clear()
        try:
            fails = await _once(manager, rt, fails)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never let the task die; retried
            log.exception("site %s: #arm rule switching failed", rt.site_id)
            fails += 1


async def _once(manager, rt, fails: int) -> int:
    """One attempt. Returns the new count of failures in a row."""
    async with sessionmaker()() as db:
        site = await db.get(Site, rt.site_id)
        if site is None or not site.enabled or site.archived_at is not None:
            return 0
        want = arming.site_state(site, int(time.time() * 1000)).armed
    t0 = time.monotonic()
    try:
        names, switched, failed = await _switch(rt, want)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported on the site and retried
        names, switched, failed = (rt.arm_rules or {}).get("rules", []), [], [{"rule": "", "error": _why(exc)}]
    ms = round((time.monotonic() - t0) * 1000)
    state = "failed" if failed else "ok"
    detail = "; ".join(f"{f['rule']}: {f['error']}" if f["rule"] else f["error"] for f in failed)[:500]
    _set(rt, want, names, state, detail)
    if switched:
        log.info("site %s: %s %d NX rule(s) tagged #arm in %d ms: %s", rt.site_id, "switched on" if want else "switched off",
                 len(switched), ms, ", ".join(switched))
    if failed:
        log.warning("site %s: couldn't switch NX rules tagged #arm %s (try %d): %s", rt.site_id, "on" if want else "off",
                    fails + 1, detail)

    async with sessionmaker()() as db:
        site = await db.get(Site, rt.site_id)
        if site is None:
            return 0
        if switched or (failed and fails == 0):
            audit(db, site.tenant_id, "site.arm_rules", site_id=site.id, armed=want, switched=switched, failed=failed,
                  took_ms=ms)
        created = []
        if failed and fails + 1 == FAIL_ALARM_AFTER and site.status == "online":
            created = await _raise(manager, rt, db, site, want, detail)
        if not failed:
            await _resolved(db, site, want)
        await db.commit()
        manager._announce(created)
    return fails + 1 if failed else 0


def _set(rt, want: bool, names: list[str], state: str, detail: str) -> None:
    new = {"armed": want, "rules": names, "state": state, "detail": detail}
    old = rt.arm_rules or {}
    if {k: old.get(k) for k in new} != new:
        rt.arm_rules = {**new, "at": time.time()}
        bus.publish(rt.tenant_id, "site.arm_rules", {"site_id": rt.site_id, "arm_rules": rt.arm_rules})


def pending(rt, names: list[str], want: bool, behind: bool) -> None:
    """From the poller's rule read: the tagged rules, and whether some are in the wrong state (then switching)."""
    old = rt.arm_rules or {}
    state = "switching" if behind else ("ok" if old.get("state") in (None, "switching") else old["state"])
    _set(rt, want, names, state, old.get("detail", "") if state == "failed" else "")


async def _raise(manager, rt, db, site: Site, want: bool, detail: str) -> list[Alarm]:
    from app.services.poller import ingest
    tenant = await db.get(Tenant, site.tenant_id)
    policy = Policy.from_settings(tenant.settings if tenant else None)
    ts = int(time.time() * 1000)
    if want:
        caption = "NX rules tagged #arm didn't switch back on"
        text = (f"The site is armed but the portal couldn't switch its NX rules tagged #arm back on: {detail}. "
                "Until they're on, what those rules do in NX (sirens, speakers, notifications) and any portal alarms they "
                "raise won't happen. The portal keeps trying; switch them on in Nx Witness if it can't.")
    else:
        caption = "NX rules tagged #arm didn't switch off"
        text = (f"The site is disarmed but the portal couldn't switch its NX rules tagged #arm off: {detail}. "
                "What those rules do in NX (sirens, speakers, notifications) still happens. The portal keeps trying.")
    row = {"timestampMs": ts, "ruleId": "", "actionData": {},
           "eventData": {"type": ALARM_ON if want else ALARM_OFF, "timestamp": str(ts * 1000), "state": "instant",
                         "source": site.name, "caption": caption, "description": text}}
    async with rt.ingest_lock:
        return await ingest(db, site, [row], {}, policy=policy)


async def _resolved(db, site: Site, want: bool) -> None:
    """Switched after all: its open "didn't switch" alarms say so (they stay open for the operator)."""
    from app.services.serialize import alarm_dict
    rows = (await db.scalars(select(Alarm).where(Alarm.site_id == site.id, Alarm.state == "new",
                                                 Alarm.event_type.in_((ALARM_ON, ALARM_OFF))))).all()
    rows = [a for a in rows if "\nSwitched at " not in (a.description or "")]
    if not rows:
        return
    from app import sms
    now = datetime.now(timezone.utc)
    for a in rows:
        a.description = f"{a.description}\nSwitched at {sms._clock(a, now)}: the rules are {'on' if want else 'off'} now."
    await db.flush()
    for a in rows:
        bus.publish(site.tenant_id, "alarm.updated", alarm_dict(a))
