"""The portal's MCP server: AI assistants (Claude through the TWG MCP Gateway) read and act on alarms.

The user, 2026-10-15: "Make a MCP for this platform", scope "Read + actions + dispatch".

Transport: MCP Streamable HTTP at POST /mcp, stateless (no Mcp-Session-Id), every answer a single JSON body
(the spec lets a server answer a POST with application/json instead of an SSE stream). GET and DELETE: 405 (no
server-initiated stream, no session to end). Protocol versions 2024-11-05 to 2025-06-18; a batch (2025-03-26) works.

Auth: "Authorization: Bearer twgp_..." (an API token from the Account page, app/api_tokens.py). The token acts as
its person: each tool runs the portal's own API route inside this process (an ASGI call with "twg_mcp" in the scope,
which deps.current_user trusts; nothing from outside can set it), so the same company scope, permissions, checks
and audit rows apply as on the website, and every audit row says "via": "MCP (<token name>)". Bad tokens count as
failed sign-ins from that IP (security_guard: bans), and each token gets CALLS_PER_MIN tool calls a minute.

Platform (TWG) users see every company by default; company_id narrows to one. Tools that need one company
(users, test texts) use the person's own unless company_id says otherwise.
"""
import asyncio
import base64
import json
import logging
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app import api_tokens, security_guard
from app.audit import VIA
from app.config import get_settings
from app.db import sessionmaker
from app.net import client_ip

log = logging.getLogger("portal.mcp")
router = APIRouter()

VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
CALLS_PER_MIN = 120
CALL_TIMEOUT_S = 30.0
SERVER_INFO = {"name": "twg-alarm-portal", "title": "TWG Alarm Portal", "version": "1.0.0"}
INSTRUCTIONS = (
    "TWG Security's alarm monitoring portal (NX Witness). Alarms come from NX rules at each site; levels are critical, "
    "alarm and warning. An open alarm waits for an operator: set_alarm_verdict closes it as real / false / dismissed "
    "(real texts the people set to get confirmed alarms). dispatch_guards texts guards a link to the alarm and makes "
    "it an active call until end_call. Dispatch and verdicts reach real people: confirm with the user before using "
    "them. Times are UTC ISO 8601. Each alarm has a 'link' that opens it in the portal.")

_calls: dict[int, list[float]] = {}          # token id -> monotonic times of recent tool calls


def _base() -> str:
    return get_settings().webauthn_origins.split(",")[0].strip().rstrip("/")


def _iso_ms(ms) -> str | None:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if ms else None


# ---------------------------------------------------------------- calling the portal's own API as the token's person
class ApiError(Exception):
    pass


async def _api(ctx: dict, method: str, path: str, params: dict | None = None, body=None, raw: bool = False):
    """Run one portal route in-process as the token's person. JSON (or bytes with raw=True); ApiError on 4xx/5xx."""
    params = {k: ("true" if v is True else "false" if v is False else v) for k, v in (params or {}).items() if v is not None}
    data = json.dumps(body).encode() if body is not None else b""
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1", "method": method,
             "scheme": "https", "path": path, "raw_path": path.encode(), "root_path": "",
             "query_string": urlencode(params, doseq=True).encode(),
             "headers": [(b"host", ctx["host"].encode()), (b"accept", b"application/json"),
                         (b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())],
             "client": (ctx["ip"], 0), "server": ("mcp.internal", 443),
             "twg_mcp": {"user_id": ctx["user_id"], "company": ctx.get("company"), "token_id": ctx["token_id"]}}
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": data, "more_body": False}
        await asyncio.Event().wait()                   # never disconnects; the call is bounded by CALL_TIMEOUT_S

    status, chunks, ctype = 500, [], ""

    async def send(msg):
        nonlocal status, ctype
        if msg["type"] == "http.response.start":
            status = msg["status"]
            ctype = dict((k.lower(), v) for k, v in msg.get("headers", [])).get(b"content-type", b"").decode()
        elif msg["type"] == "http.response.body":
            chunks.append(msg.get("body", b""))

    await asyncio.wait_for(ctx["app"](scope, receive, send), CALL_TIMEOUT_S)
    out = b"".join(chunks)
    if status >= 400:
        try:
            detail = json.loads(out).get("detail")
        except (ValueError, AttributeError):
            detail = None
        if isinstance(detail, list):                    # FastAPI validation errors
            detail = "; ".join(f"{'.'.join(str(x) for x in d.get('loc', [])[1:])}: {d.get('msg')}" for d in detail)
        raise ApiError(f"{detail or 'request failed'} (HTTP {status})")
    if raw:
        return out, ctype
    return json.loads(out) if out else None


# ---------------------------------------------------------------- shaping answers for a model to read
def _alarm(a: dict) -> dict:
    return {"id": a["id"], "company": a.get("tenant_name"), "site_id": a.get("site_id"), "site": a.get("site_name"),
            "address": a.get("site_address") or None, "level": a.get("level"), "caption": a.get("caption"),
            "description": (a.get("description") or "")[:500] or None, "camera": a.get("source_name") or None,
            "camera_id": a.get("device_id") or None, "event_at": _iso_ms(a.get("event_ts_ms")),
            "received_at": a.get("received_at"), "state": "open" if a.get("state") == "new" else a.get("state"),
            "verdict": a.get("verdict") or None, "acknowledged_by": a.get("acked_by"), "acknowledged_at": a.get("acked_at"),
            "note": a.get("ack_note") or None, "call": a.get("call_state") or None,
            "call_ended_at": a.get("call_ended_at"), "link": f"{_base()}/?alarm={a['id']}"}


def _site(s: dict) -> dict:
    keys = ("id", "name", "address", "lat", "lng", "enabled", "status", "status_detail", "last_seen_at",
            "nx_site_name", "nx_version", "camera_count", "timezone_effective", "notes")
    out = {"company": s.get("tenant_name"), **{k: s.get(k) for k in keys if k in s}}
    out["open_alarms"] = {"critical": s.get("open_critical", 0), "alarm": s.get("open_alarm", 0), "warning": s.get("open_warning", 0)}
    arming = s.get("arming") or {}
    if arming:
        out["arming"] = arming
    return out


def _tracker(t: dict) -> dict:
    return {"id": t["id"], "name": t["name"], "kind": t.get("kind"), "source": t.get("source"), "online": t.get("online"),
            "lat": t.get("lat"), "lng": t.get("lng"), "speed": t.get("speed"), "heading": t.get("heading"),
            "position_at": t.get("fix_at"), "device": t.get("device_name") or None, "error": t.get("error") or None}


def _int(args: dict, key: str, required: bool = True) -> int | None:
    v = args.get(key)
    if v is None or v == "":
        if required:
            raise ApiError(f"{key} is required")
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ApiError(f"{key} must be a number") from None


# ---------------------------------------------------------------- tools
async def t_summary(ctx, args):
    s = await _api(ctx, "GET", "/api/summary")
    calls = await _api(ctx, "GET", "/api/alarms", {"state": "active", "limit": 500})
    sites = await _api(ctx, "GET", "/api/sites")
    return {"open_alarms": {"critical": s["open_critical"], "alarm": s["open_alarm"], "warning": s["open_warning"]},
            "active_calls": len(calls), "sites": len(sites),
            "sites_offline": [x["name"] for x in sites if x.get("status") in ("offline", "auth_error") or not x.get("enabled")]}


async def t_list_alarms(ctx, args):
    params = {"state": args.get("state") or "open", "site_id": _int(args, "site_id", False),
              "priority": {"critical": 1, "alarm": 2, "warning": 3}.get(args.get("level") or ""),
              "verdict": args.get("verdict"), "q": args.get("search"), "before_id": _int(args, "before_id", False),
              "limit": min(_int(args, "limit", False) or 25, 100)}
    rows = await _api(ctx, "GET", "/api/alarms", params)
    return {"alarms": [_alarm(a) for a in rows], "count": len(rows),
            "more": f"call again with before_id={rows[-1]['id']}" if len(rows) == params["limit"] else None}


async def t_get_alarm(ctx, args):
    aid = _int(args, "alarm_id")
    a = await _api(ctx, "GET", f"/api/alarms/{aid}")
    notes = await _api(ctx, "GET", f"/api/alarms/{aid}/notes")
    timeline = await _api(ctx, "GET", f"/api/alarms/{aid}/timeline")
    return {**_alarm(a), "notes": [{"at": n["at"], "by": n["by"], "text": n["text"]} for n in notes],
            "timeline": [{"at": _iso_ms(r.get("ts")), "what": r.get("what"), "who": r.get("who"),
                          "detail": r.get("detail") or None, "kind": r.get("kind")} for r in timeline]}


async def t_snapshot(ctx, args):
    aid = _int(args, "alarm_id")
    a = await _api(ctx, "GET", f"/api/alarms/{aid}")
    if not a.get("device_id"):
        raise ApiError("This alarm has no camera, so there's no picture")
    img, ctype = await _api(ctx, "GET", f"/media/alarms/{aid}/snapshot.jpg", raw=True)
    if not img or not ctype.startswith("image/"):
        raise ApiError("NX didn't return a picture for this alarm")
    return [{"type": "text", "text": f"Alarm {aid}: {a.get('caption')} at {a.get('site_name')}, camera "
                                     f"{a.get('source_name') or a.get('device_id')}, {_iso_ms(a.get('event_ts_ms'))}"},
            {"type": "image", "data": base64.b64encode(img).decode(), "mimeType": ctype.split(";")[0]}]


async def t_list_sites(ctx, args):
    return {"sites": [_site(s) for s in await _api(ctx, "GET", "/api/sites")]}


async def t_list_cameras(ctx, args):
    sid = _int(args, "site_id")
    rows = await _api(ctx, "GET", f"/api/sites/{sid}/cameras")
    return {"site_id": sid, "cameras": [{"camera_id": c["device_id"], "name": c["name"], "in_nx": c.get("in_nx"),
                                         "placed_on_map": bool(c.get("place"))} for c in rows]}


async def t_list_rules(ctx, args):
    sid = _int(args, "site_id")
    return await _api(ctx, "GET", f"/api/sites/{sid}/rules")


async def t_list_guards(ctx, args):
    rows = await _api(ctx, "GET", "/api/trackers", {"trail_min": 0})
    kind = args.get("kind")
    return {"trackers": [_tracker(t) for t in rows if not kind or t.get("kind") == kind]}


async def t_dispatch_candidates(ctx, args):
    aid = _int(args, "alarm_id")
    r = await _api(ctx, "GET", f"/api/alarms/{aid}/guards")
    return {"alarm_id": aid, "texts_ready": r.get("texts_ready"),
            "guards": [{"guard_id": g["tracker_id"], "name": g["name"], "person": g.get("person") or None,
                        "distance": g.get("distance"), "distance_m": g.get("distance_m"), "online": g.get("online"),
                        "position_at": g.get("fix_at"), "closest": g.get("closest"), "can_text": g.get("can_text"),
                        "why_not": g.get("reason") or None} for g in r.get("guards", [])],
            "already_dispatched": r.get("dispatched")}


async def t_audit(ctx, args):
    params = {"action": args.get("action"), "site_id": _int(args, "site_id", False), "alarm_id": _int(args, "alarm_id", False),
              "before_id": _int(args, "before_id", False), "limit": min(_int(args, "limit", False) or 50, 200)}
    rows = await _api(ctx, "GET", "/api/audit", params)
    keep = ("id", "ts", "action", "user", "support", "site_name", "alarm_id", "ip", "detail")
    return {"entries": [{k: r.get(k) for k in keep if k in r} for r in rows]}


async def t_list_users(ctx, args):
    ctx = {**ctx, "company": ctx.get("company") or "own"}
    rows = await _api(ctx, "GET", "/api/users")
    return {"users": [{"id": u["id"], "name": u.get("display_name") or u["email"], "email": u["email"], "role": u["role"],
                       "active": u.get("is_active"), "phone": u.get("phone") or None, "texts": u.get("sms_level"),
                       "text_consent": u.get("sms_consent"), "monitoring_station": u.get("monitoring_station"),
                       "last_sign_in": u.get("last_login_at")} for u in rows]}


async def t_add_note(ctx, args):
    aid = _int(args, "alarm_id")
    text = str(args.get("text") or "").strip()
    if not text:
        raise ApiError("text is required")
    n = await _api(ctx, "POST", f"/api/alarms/{aid}/notes", body={"text": text})
    return {"added": True, "alarm_id": aid, "note": {"at": n["at"], "by": n["by"], "text": n["text"]}}


async def t_verdict(ctx, args):
    aid = _int(args, "alarm_id")
    verdict = args.get("verdict")
    if verdict not in ("real", "false", "dismissed"):
        raise ApiError("verdict must be real, false or dismissed")
    note = str(args.get("note") or "")
    a = await _api(ctx, "GET", f"/api/alarms/{aid}")
    if a.get("state") == "new":
        try:
            a = await _api(ctx, "POST", f"/api/alarms/{aid}/ack", body={"note": note, "verdict": verdict})
            return {"done": "acknowledged", "alarm": _alarm(a)}
        except ApiError as e:
            if "409" not in str(e):
                raise
    # Already closed: change its verdict (the portal's admin override, which needs that permission).
    r = await _api(ctx, "POST", "/api/alarms/bulk", body={"ids": [aid], "verdict": verdict, "note": note})
    a = await _api(ctx, "GET", f"/api/alarms/{aid}")
    return {"done": "verdict changed" if aid in r.get("changed", []) or aid in r.get("acknowledged", []) else "unchanged",
            "alarm": _alarm(a)}


async def t_end_call(ctx, args):
    aid = _int(args, "alarm_id")
    a = await _api(ctx, "POST", f"/api/alarms/{aid}/end-call", body={"note": str(args.get("note") or "")})
    return {"ended": True, "alarm": _alarm(a)}


async def t_dispatch(ctx, args):
    aid = _int(args, "alarm_id")
    ids = args.get("guard_ids")
    if not isinstance(ids, list) or not ids:
        raise ApiError("guard_ids: give at least one guard id (from get_dispatch_candidates)")
    try:
        ids = [int(x) for x in ids][:50]
    except (TypeError, ValueError):
        raise ApiError("guard_ids must be numbers") from None
    r = await _api(ctx, "POST", f"/api/alarms/{aid}/dispatch", body={"tracker_ids": ids})
    return {"alarm_id": aid, "sent": r.get("sent"), "failed": r.get("failed")}


async def t_test_text(ctx, args):
    uid = _int(args, "user_id")
    ctx = {**ctx, "company": ctx.get("company") or "own"}
    return await _api(ctx, "POST", f"/api/users/{uid}/test-text")


_COMPANY = {"company_id": {"type": "integer", "description": "TWG staff only: one company's id (default: every company you can see)"}}
_ALARM = {"alarm_id": {"type": "integer", "description": "The alarm's id"}}
RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


def _schema(props: dict, required: tuple = ()) -> dict:
    return {"type": "object", "properties": {**props, **_COMPANY}, "required": list(required), "additionalProperties": False}


TOOLS = [
    ("get_overview", "Get overview", "Open alarms by level, active calls (dispatched alarms), number of sites and which are offline.",
     _schema({}), RO, t_summary),
    ("list_alarms", "List alarms", "Alarms, newest first. state: open (waiting for an operator, the default), active (calls "
     "in progress), acknowledged, disarmed or all. Filter by site, level, verdict or text. Page with before_id.",
     _schema({"state": {"type": "string", "enum": ["open", "active", "acknowledged", "disarmed", "all"]},
              "site_id": {"type": "integer"}, "level": {"type": "string", "enum": ["critical", "alarm", "warning"]},
              "verdict": {"type": "string", "enum": ["real", "false", "dismissed", "none"]},
              "search": {"type": "string", "description": "Text in the caption, camera or description"},
              "before_id": {"type": "integer", "description": "Only alarms older than this id (paging)"},
              "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Default 25"}}), RO, t_list_alarms),
    ("get_alarm", "Get alarm", "One alarm in full: what happened, where, its state and verdict, notes, and the timeline "
     "(texts sent and delivered, who viewed it, guard updates, dispatch).", _schema(_ALARM, ("alarm_id",)), RO, t_get_alarm),
    ("get_alarm_snapshot", "Alarm picture", "The camera picture at the moment of the alarm (JPEG).",
     _schema(_ALARM, ("alarm_id",)), {**RO, "openWorldHint": True}, t_snapshot),
    ("list_sites", "List sites", "Sites (NX systems): status (online / offline), address, cameras, armed, open alarms.",
     _schema({}), RO, t_list_sites),
    ("list_cameras", "List cameras", "A site's cameras as NX lists them, and whether each is placed on the map.",
     _schema({"site_id": {"type": "integer"}}, ("site_id",)), {**RO, "openWorldHint": True}, t_list_cameras),
    ("list_nx_rules", "List NX rules", "A site's NX event rules as the portal last read them, and the alarm level each gives.",
     _schema({"site_id": {"type": "integer"}}, ("site_id",)), RO, t_list_rules),
    ("list_guards", "List guards and vehicles", "Guards (GPS routers, phones) and fleet vehicles with their last known "
     "positions and whether they're online.",
     _schema({"kind": {"type": "string", "description": "Only this kind, e.g. guard or vehicle"}}), RO, t_list_guards),
    ("get_dispatch_candidates", "Guards for an alarm", "Guards who could be sent to an alarm, nearest first, with distance, "
     "whether they can be texted (and why not), and who was already dispatched.", _schema(_ALARM, ("alarm_id",)), RO,
     t_dispatch_candidates),
    ("search_audit_log", "Search audit log", "Who did what, newest first. action is a prefix, e.g. alarm. or alarm.acknowledged.",
     _schema({"action": {"type": "string"}, "site_id": {"type": "integer"}, "alarm_id": {"type": "integer"},
              "before_id": {"type": "integer"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}), RO, t_audit),
    ("list_users", "List users", "The company's portal users, their alarm-text settings and last sign-in (admins only).",
     _schema({}), RO, t_list_users),
    ("add_alarm_note", "Add alarm note", "Add a note to an alarm's record (open or closed). Everyone watching sees it.",
     _schema({**_ALARM, "text": {"type": "string", "maxLength": 4000}}, ("alarm_id", "text")),
     {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}, t_add_note),
    ("set_alarm_verdict", "Set alarm verdict", "Close an open alarm as real (texts people set to get confirmed alarms), "
     "false (false alarm) or dismissed (no verdict), with a note; it stops sounding for every operator and is "
     "acknowledged in NX. On an alarm already closed it changes the verdict (needs the bulk-edit permission). "
     "Confirm with the user first.",
     _schema({**_ALARM, "verdict": {"type": "string", "enum": ["real", "false", "dismissed"]},
              "note": {"type": "string", "maxLength": 4000}}, ("alarm_id", "verdict")),
     {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}, t_verdict),
    ("end_call", "End call", "End an active call (a dispatched alarm). Dispatched guards who haven't cleared are told.",
     _schema({**_ALARM, "note": {"type": "string", "maxLength": 4000}}, ("alarm_id",)),
     {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}, t_end_call),
    ("dispatch_guards", "Dispatch guards", "Send guards to an alarm: each gets a text with the alarm link and directions, "
     "and the alarm becomes an active call. This texts real people: confirm with the user first. Guard ids come from "
     "get_dispatch_candidates.",
     _schema({**_ALARM, "guard_ids": {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 50}},
             ("alarm_id", "guard_ids")),
     {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}, t_dispatch),
    ("send_test_text", "Send test text", "Text a portal user a test message to check their number (admins only; they "
     "must have replied YES to alarm texts).", _schema({"user_id": {"type": "integer"}}, ("user_id",)),
     {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}, t_test_text),
]
_BY_NAME = {t[0]: t for t in TOOLS}


def tool_list() -> list[dict]:
    return [{"name": n, "title": title, "description": desc, "inputSchema": schema, "annotations": {"title": title, **ann}}
            for n, title, desc, schema, ann, _ in TOOLS]


# ---------------------------------------------------------------- JSON-RPC
def _err(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


async def _handle(msg, ctx: dict) -> dict | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _err(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid request")
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if method is None:                                   # a response from the client: nothing we asked for
        return None
    if "id" not in msg:                                  # notification (notifications/initialized, cancelled, ...)
        return None
    if method == "initialize":
        asked = str(params.get("protocolVersion") or "")
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": asked if asked in VERSIONS else VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tool_list()}}
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        tool = _BY_NAME.get(name)
        if tool is None:
            return _err(mid, -32602, f"Unknown tool: {name}")
        if not isinstance(args, dict):
            return _err(mid, -32602, "arguments must be an object")
        now = time.monotonic()
        recent = [t for t in _calls.get(ctx["token_id"], []) if now - t < 60]
        if len(recent) >= CALLS_PER_MIN:
            return {"jsonrpc": "2.0", "id": mid, "result": {"isError": True, "content": [
                {"type": "text", "text": f"Rate limit: {CALLS_PER_MIN} tool calls a minute per token. Wait a little."}]}}
        _calls[ctx["token_id"]] = recent + [now]
        company = args.pop("company_id", None)
        call_ctx = {**ctx, "company": str(int(company)) if str(company or "").isdigit() else ctx.get("company")}
        t0 = time.monotonic()
        token = VIA.set(f"MCP ({ctx['token_name']})")
        try:
            out = await tool[5](call_ctx, args)
            content = out if isinstance(out, list) else [{"type": "text", "text": json.dumps(out, default=str)}]
            result = {"content": content, "isError": False}
            ok = "ok"
        except ApiError as e:
            result, ok = {"content": [{"type": "text", "text": str(e)}], "isError": True}, f"error: {e}"
        except asyncio.TimeoutError:
            result, ok = {"content": [{"type": "text", "text": f"The portal didn't answer within {CALL_TIMEOUT_S:.0f} s"}],
                          "isError": True}, "timeout"
        except Exception as e:  # noqa: BLE001 - reported to the model, logged for us
            log.exception("mcp: %s failed", name)
            result, ok = {"content": [{"type": "text", "text": f"Internal error ({type(e).__name__})"}], "isError": True}, "crash"
        finally:
            VIA.reset(token)
        log.info("mcp: user %s token %s %s %s in %d ms", ctx["user_id"], ctx["token_id"], name, ok,
                 int((time.monotonic() - t0) * 1000))
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    return _err(mid, -32601, f"Method not found: {method}")


def _unauthorized(message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": message}}, 401,
                        headers={"WWW-Authenticate": 'Bearer realm="twg-alarm-portal"'})


@router.post("/mcp")
async def mcp_post(request: Request):
    ip = client_ip(request)
    if await security_guard.active_ban(ip) is not None:
        return JSONResponse({"error": "blocked"}, 403)
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    async with sessionmaker()() as db:
        found = await api_tokens.verify(db, token, ip) if token else None
        if found is None:
            if token:
                await security_guard.record(db, ip, "api_token", "failure", reason="unknown or revoked API token")
                await db.commit()
            return _unauthorized("A valid API token is needed (portal: Account → API tokens)")
        row, user, tenant = found
        from app.permissions import load_permissions
        user._tenant, user._perms = tenant, await load_permissions(db, user, tenant)
        ctx = {"app": request.app, "ip": ip, "host": request.headers.get("host", "localhost"), "user_id": user.id,
               "token_id": row.id, "token_name": row.name, "company": "all" if user.can("platform.view") else None}
    pv = request.headers.get("mcp-protocol-version")
    if pv and pv not in VERSIONS:
        return JSONResponse(_err(None, -32600, f"Unsupported MCP-Protocol-Version {pv}"), 400)
    try:
        body = json.loads(await request.body())
    except ValueError:
        return JSONResponse(_err(None, -32700, "Parse error"), 400)
    if isinstance(body, list):
        if not body:
            return JSONResponse(_err(None, -32600, "Empty batch"), 400)
        out = [r for r in [await _handle(m, ctx) for m in body[:20]] if r is not None]
        return JSONResponse(out) if out else Response(status_code=202)
    out = await _handle(body, ctx)
    return JSONResponse(out) if out is not None else Response(status_code=202)


@router.get("/mcp")
@router.delete("/mcp")
async def mcp_other():
    return Response(status_code=405, headers={"Allow": "POST"})
