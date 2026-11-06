"""Webhooks into NX (the user, 2026-11-06: "the ability to have web hooks and ... send either a generic event or a soft
trigger from the alarm portal that way we can send things to NX").

A company admin adds a webhook to one of its sites (Sites page → Webhooks to NX). Calling its address
`https://<portal>/hook/<token>` (POST JSON / form, or GET with ?query, for devices that can only do a GET) does one
thing on that site's NX, through the site's own connection (cloud, direct or its VPN):

* generic: a Generic Event. Source / Caption / Description are templates: `{zone}` or `{event.zone}` is filled from the
  call's fields; a blank template takes the call's own `source` / `caption` / `description`. NX rules on Generic Event
  then do whatever they do there (and, if a rule logs it, the portal raises it like any other event).
* trigger: fires one of the site's NX soft triggers on its camera.

The call's `state` field (instant / started / stopped) overrides the saved one, so a door contact can start and stop a
prolonged trigger or a recording rule. Request formats are from the site's own NX 6.1.2 API spec (/static/openapi_v4.json):
POST /rest/v4/events/generic {source, caption, description, deviceIds, state}, POST /rest/v4/events/triggers {triggerId,
deviceId (required: one of the trigger's cameras), state}.

Security: the token is 32 random characters; only its SHA-256 is stored and it is shown once (Rotate makes a new one).
An unknown token counts as a failed sign-in for the caller's address (kind "webhook"), so guessing gets it banned.
Each webhook takes at most RATE_PER_MIN calls a minute. The last RECENT calls are kept on the row for the Sites page;
setting changes and button presses go to the audit log, calls from outside don't (a busy integration would drown it).
"""
from __future__ import annotations

import hashlib
import re
import secrets
import time
from collections import deque

import httpx

from app import platform_settings
from app.services.sites import describe_http_error

KINDS = {"generic": "Generic event", "trigger": "Soft trigger"}
STATES = ("instant", "started", "stopped")          # NX 6.1 openapi_v4, both endpoints
TEMPLATE_MAX = 1000
RATE_PER_MIN = 60
RECENT = 20
FIELD = re.compile(r"\{([A-Za-z0-9_.\-]{1,64})\}")
_calls: dict[int, deque] = {}


def new_token() -> tuple[str, str, str]:
    """(token, its SHA-256, a hint that recognises it but can't be used)."""
    token = "twgh_" + secrets.token_urlsafe(24)
    return token, token_hash(token), f"{token[:5]}…{token[-4:]}"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def url(token: str) -> str:
    return f"{platform_settings.current().portal_url}/hook/{token}"


def flatten(value, prefix: str = "", out: dict | None = None) -> dict[str, str]:
    """{"event": {"zone": "Lot"}} -> {"event.zone": "Lot"}; lists as a, b; everything as text."""
    out = {} if out is None else out
    if isinstance(value, dict):
        for k, v in value.items():
            key = f"{prefix}{k}"
            if isinstance(v, dict):
                flatten(v, key + ".", out)
            else:
                out[key] = ", ".join(map(str, v)) if isinstance(v, list) else ("" if v is None else str(v))
    return out


def render(template: str, fields: dict[str, str]) -> str:
    return FIELD.sub(lambda m: fields.get(m.group(1), ""), template or "")[:TEMPLATE_MAX]


def clean_config(kind: str, cfg: dict) -> dict:
    """The saved settings, checked. Raises ValueError with a readable message."""
    if kind == "generic":
        state = str(cfg.get("state") or "instant")
        if state not in STATES:
            raise ValueError("State must be instant, started or stopped")
        out = {k: str(cfg.get(k) or "").strip()[:TEMPLATE_MAX] for k in ("source", "caption", "description")}
        cams = [str(d).strip("{}") for d in (cfg.get("device_ids") or []) if str(d).strip()][:20]
        return {**out, "state": state, "device_ids": cams, **_on(cfg)}
    if kind == "trigger":
        tid = str(cfg.get("trigger_id") or "").strip("{} ")
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", tid):
            raise ValueError("Pick the soft trigger to fire")
        state = str(cfg.get("state") or "instant")
        if state not in STATES:
            raise ValueError("State must be instant, started or stopped")
        dev = str(cfg.get("device_id") or "").strip("{} ")
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", dev):
            raise ValueError("Pick the camera the soft trigger is on")
        return {"trigger_id": tid, "trigger_name": str(cfg.get("trigger_name") or "")[:200], "device_id": dev, "state": state,
                **_on(cfg)}
    raise ValueError("Kind must be generic or trigger")


def _on(cfg: dict) -> dict:
    """Also run by itself when the site arms / disarms (app/services/arm_event.py)."""
    return {"on_arm": bool(cfg.get("on_arm")), "on_disarm": bool(cfg.get("on_disarm"))}


def allowed(hook_id: int) -> bool:
    """At most RATE_PER_MIN calls a minute per webhook."""
    q = _calls.setdefault(hook_id, deque())
    now = time.monotonic()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_PER_MIN:
        return False
    q.append(now)
    return True


def request_for(hook, fields: dict[str, str]) -> dict:
    """What the call sends to NX (also what the Sites page shows as "sent")."""
    cfg = hook.config or {}
    if hook.kind == "generic":
        body = {}
        for k in ("source", "caption", "description"):
            body[k] = render(cfg.get(k, ""), fields) if cfg.get(k) else fields.get(k, "")[:TEMPLATE_MAX]
        body["source"] = body["source"] or hook.name
        state = fields.get("state", "")
        body["state"] = state if state in STATES else cfg.get("state", "instant")
        if cfg.get("device_ids"):
            body["device_ids"] = list(cfg["device_ids"])
        return body
    state = fields.get("state", "")
    return {"trigger_id": cfg["trigger_id"], "trigger_name": cfg.get("trigger_name", ""), "device_id": cfg.get("device_id", ""),
            "state": state if state in STATES else cfg.get("state", "instant")}


async def send(client, hook, fields: dict[str, str]) -> dict:
    """Do it on NX. Returns {ok, ms, sent, detail}; never raises for an NX problem."""
    sent = request_for(hook, fields)
    t0 = time.monotonic()
    try:
        if hook.kind == "generic":
            await client.create_generic_event(sent["source"], sent["caption"] or None, sent["description"] or None,
                                              device_ids=sent.get("device_ids") or None, state=sent["state"])
        else:
            await client.fire_trigger(sent["trigger_id"], sent["device_id"], state=sent["state"])
        ok, detail = True, ""
    except httpx.HTTPStatusError as e:
        ok = False
        code = e.response.status_code
        text = (e.response.text or "")[:200]
        detail = (f"NX refused it (HTTP {code}): the portal's NX account needs rights to "
                  f"{'create generic events' if hook.kind == 'generic' else 'use this soft trigger'}" if code in (401, 403)
                  else f"NX answered HTTP {code}{': ' + text if text else ''}")
    except httpx.HTTPError as e:
        ok, detail = False, str(describe_http_error(e))
    return {"ok": ok, "ms": round((time.monotonic() - t0) * 1000), "sent": sent, "detail": detail}


def note(hook, result: dict, origin: str) -> None:
    """Keep the call on the row (the caller commits)."""
    hook.uses = (hook.uses or 0) + 1
    entry = {"at": time.time(), "ok": result["ok"], "ms": result.get("ms"), "from": origin[:120],
             "detail": result.get("detail", "")[:300], "sent": result.get("sent")}
    hook.recent = ([entry] + list(hook.recent or []))[:RECENT]
