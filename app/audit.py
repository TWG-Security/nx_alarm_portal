"""Helper for writing audit rows.

Rows about an alarm are its timeline (app/services/report.py `timeline`: the alarm details' Timeline and the
incident report). Once such a row is committed, open screens get "alarm.timeline" {alarm_id} so a dispatcher
watches the guard open the link, go en route and arrive, live.
"""

import contextvars
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog

QUIET = {"alarm.received"}           # every alarm has one; screens already get the alarm itself
# What the guard did from their alarm link (report._audit_row kind "guard"): the event says {"guard": true}, so an
# active call's dispatcher hears the update chime (the user, 2026-10-14: "if the guard says enroute it makes a noise").
GUARD_ACTIONS = {"alarm.link_opened", "alarm.link_cameras", "tracker.share_from_alarm", "guard.status",
                 "alarm.joined_call"}          # a new alarm joined an active call: the same update chime and card


# Set while an MCP tool call runs (app/mcp_server.py): every audit row it causes says so, e.g. "MCP (Claude desk)".
VIA: contextvars.ContextVar[str] = contextvars.ContextVar("audit_via", default="")


# Which client sent the request being handled: [kind, version], set by main.ClientMark for every HTTP request and
# emptied when it ends (so a task it started, like a new site's poller, doesn't keep the label). Every audit row
# stores it (the Mac session, 2026-10-26: "show in the portal's logs whether each sign-in and action came from the
# iPhone app, the Android app or the website").
CLIENT: contextvars.ContextVar = contextvars.ContextVar("audit_client", default=("", ""))
CLIENT_NAMES = {"ios": "iPhone app", "android": "Android app", "app": "phone app", "api": "API", "web": "website"}
_NOT_VERSION = re.compile(r"[^\w .()+-]")


def client_of(scope: dict) -> tuple[str, str]:
    """(kind, version) of an ASGI request:
      X-TWG-Client "ios/1.6 (55)" / "android/0.1 (1)" (both apps send it on every request) -> ("ios", "1.6 (55)");
      the app's Bearer token or /api/mobile/ without it (builds before the header) -> ("app", "");
      an API token, or MCP -> ("api", ""); a browser (our session cookie, or a Mozilla User-Agent) -> ("web", "");
      anything else (a Twilio webhook) -> ("", "")."""
    if scope.get("twg_mcp") is not None:
        return "api", ""
    h: dict[bytes, bytes] = {}
    for k, v in scope.get("headers") or ():
        h.setdefault(k.lower(), v)
    kind, _, version = h.get(b"x-twg-client", b"").decode("latin-1").strip().partition("/")
    if kind.lower() in ("ios", "android"):
        return kind.lower(), _NOT_VERSION.sub("", version).strip()[:40]
    auth = h.get(b"authorization", b"").decode("latin-1")
    if auth[:7].lower() == "bearer ":
        return ("app" if auth[7:].strip().startswith("twgm_") else "api"), ""
    if scope.get("path", "").startswith("/api/mobile/"):
        return "app", ""
    if b"twg_portal=" in h.get(b"cookie", b"") or h.get(b"user-agent", b"").startswith(b"Mozilla/"):
        return "web", ""
    return "", ""


def audit(db: AsyncSession, tenant_id: int, action: str, *, user_id: int | None = None,
          site_id: int | None = None, alarm_id: int | None = None, ip: str = "", **detail) -> AuditLog:
    if VIA.get() and "via" not in detail:
        detail["via"] = VIA.get()
    client, version = CLIENT.get()
    row = AuditLog(tenant_id=tenant_id, action=action, user_id=user_id, site_id=site_id,
                   alarm_id=alarm_id, ip=ip, detail=detail, client=client, client_version=version)
    db.add(row)
    if alarm_id is not None and action not in QUIET:
        timeline_changed(db, tenant_id, alarm_id, guard=action in GUARD_ACTIONS or (action == "alarm.note" and bool(detail.get("guard"))))
    return row


def timeline_changed(db: AsyncSession, tenant_id: int, alarm_id: int, guard: bool = False) -> None:
    """Tell open screens the alarm's timeline grew, once this session commits (never before: they re-read it)."""
    from sqlalchemy import event
    pending = db.info.setdefault("timeline", {})
    if not pending:
        def _go(_session):
            from app.services.bus import bus
            for (tid, aid), by_guard in db.info.pop("timeline", {}).items():
                bus.publish(tid, "alarm.timeline", {"alarm_id": aid, "guard": by_guard})
        event.listen(db.sync_session, "after_commit", _go, once=True)
    pending[(tenant_id, alarm_id)] = pending.get((tenant_id, alarm_id), False) or guard
