"""Webhooks into a site's NX (app/webhooks.py), on the Sites page. Company admins set them up; TWG in support mode with
platform.support. The token is shown once, when made or rotated.

- GET    /api/webhooks                    the company's webhooks (no tokens), recent calls
- POST   /api/webhooks                    {site_id, name, kind: generic|trigger, config, button?, enabled?} -> + token, url
- PUT    /api/webhooks/{id}               {name?, config?, button?, enabled?}
- POST   /api/webhooks/{id}/rotate        a new token (the old address stops working) -> token, url
- DELETE /api/webhooks/{id}
- POST   /api/webhooks/{id}/send          {fields?}: admins' "Send test"; operators press it when it's a site panel button
- GET    /api/sites/{id}/nx-triggers      the site's NX soft triggers and their cameras (for the form)
- GET    /api/sites/{id}/buttons          the site panel's buttons (anyone who sees the site)
- GET|POST|PUT /hook/{token}              the webhook itself: no sign-in, the token is the key
"""
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import security_guard, webhooks
from app.audit import audit
from app.db import get_db
from app.deps import client_ip, csrf_protect, require_admin
from app.models import Site, User, Webhook
from app.routers.api import _get_site
from app.scope import Scope, get_scope
from app.services.poller import manager

router = APIRouter(dependencies=[Depends(csrf_protect)])
public = APIRouter()

BODY_MAX = 64 * 1024
PRIVATE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"}


class HookIn(BaseModel):
    site_id: int
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(pattern="^(generic|trigger)$")
    config: dict = Field(default_factory=dict)
    button: bool = False
    enabled: bool = True


class HookPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    config: dict | None = None
    button: bool | None = None
    enabled: bool | None = None


class SendIn(BaseModel):
    fields: dict = Field(default_factory=dict)


def _config(kind: str, cfg: dict) -> dict:
    try:
        return webhooks.clean_config(kind, cfg)
    except ValueError as exc:
        raise HTTPException(400, {"message": str(exc)}) from exc


def _out(h: Webhook, site_name: str = "") -> dict:
    from app import platform_settings
    return {"id": h.id, "tenant_id": h.tenant_id, "site_id": h.site_id, "site_name": site_name, "name": h.name,
            "kind": h.kind, "kind_label": webhooks.KINDS[h.kind], "enabled": h.enabled, "button": h.button,
            "url_hint": f"{platform_settings.current().portal_url}/hook/{h.hint}", "config": h.config or {},
            "uses": h.uses or 0, "recent": h.recent or [],
            "updated_at": h.updated_at.isoformat() if h.updated_at else None}


async def _get(db: AsyncSession, scope: Scope, hook_id: int, write: bool = False) -> Webhook:
    h = await db.scalar(select(Webhook).where(Webhook.id == hook_id, scope.where(Webhook.tenant_id)))
    if h is None:
        raise HTTPException(404, "Webhook not found")
    if write:
        scope.require_write(h.tenant_id)
    return h


@router.get("/api/webhooks")
async def list_hooks(user: User = Depends(require_admin), scope: Scope = Depends(get_scope),
                     db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Webhook, Site.name).join(Site, Site.id == Webhook.site_id)
                             .where(scope.where(Webhook.tenant_id)).order_by(Site.name, Webhook.name))).all()
    return [_out(h, name) for h, name in rows]


@router.post("/api/webhooks")
async def create_hook(body: HookIn, request: Request, user: User = Depends(require_admin), scope: Scope = Depends(get_scope),
                      db: AsyncSession = Depends(get_db)):
    site = await _get_site(db, scope, body.site_id, write=True)
    token, digest, hint = webhooks.new_token()
    h = Webhook(tenant_id=site.tenant_id, site_id=site.id, name=body.name.strip(), kind=body.kind,
                config=_config(body.kind, body.config), button=body.button, enabled=body.enabled, token_hash=digest, hint=hint)
    db.add(h)
    await db.flush()
    audit(db, site.tenant_id, "webhook.created", user_id=user.id, site_id=site.id, ip=client_ip(request), webhook_id=h.id,
          name=h.name, kind=h.kind, config=h.config, button=h.button)
    await db.commit()
    return {**_out(h, site.name), "token": token, "url": webhooks.url(token)}


@router.put("/api/webhooks/{hook_id}")
async def update_hook(hook_id: int, body: HookPatch, request: Request, user: User = Depends(require_admin),
                      scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    h = await _get(db, scope, hook_id, write=True)
    changed = {}
    if body.name is not None and body.name.strip() != h.name:
        changed["name"] = h.name = body.name.strip()
    if body.config is not None:
        cfg = _config(h.kind, body.config)
        if cfg != (h.config or {}):
            changed["config"] = h.config = cfg
    for k in ("button", "enabled"):
        v = getattr(body, k)
        if v is not None and v != getattr(h, k):
            setattr(h, k, v)
            changed[k] = v
    if changed:
        audit(db, h.tenant_id, "webhook.updated", user_id=user.id, site_id=h.site_id, ip=client_ip(request), webhook_id=h.id,
              name=h.name, **changed)
    await db.commit()
    site = await db.get(Site, h.site_id)
    return _out(h, site.name if site else "")


@router.post("/api/webhooks/{hook_id}/rotate")
async def rotate_hook(hook_id: int, request: Request, user: User = Depends(require_admin), scope: Scope = Depends(get_scope),
                      db: AsyncSession = Depends(get_db)):
    h = await _get(db, scope, hook_id, write=True)
    token, h.token_hash, h.hint = webhooks.new_token()
    audit(db, h.tenant_id, "webhook.rotated", user_id=user.id, site_id=h.site_id, ip=client_ip(request), webhook_id=h.id, name=h.name)
    await db.commit()
    site = await db.get(Site, h.site_id)
    return {**_out(h, site.name if site else ""), "token": token, "url": webhooks.url(token)}


@router.delete("/api/webhooks/{hook_id}")
async def delete_hook(hook_id: int, request: Request, user: User = Depends(require_admin), scope: Scope = Depends(get_scope),
                      db: AsyncSession = Depends(get_db)):
    h = await _get(db, scope, hook_id, write=True)
    audit(db, h.tenant_id, "webhook.deleted", user_id=user.id, site_id=h.site_id, ip=client_ip(request), webhook_id=h.id,
          name=h.name, kind=h.kind, config=h.config)
    await db.delete(h)
    await db.commit()
    return {"ok": True}


@router.post("/api/webhooks/{hook_id}/send")
async def send_hook(hook_id: int, body: SendIn, request: Request, scope: Scope = Depends(get_scope),
                    db: AsyncSession = Depends(get_db)):
    """An admin's "Send test", or an operator pressing it on the site panel (button webhooks only)."""
    user = scope.user
    h = await _get(db, scope, hook_id, write=True)
    if not user.is_admin and not (h.button and h.enabled):
        raise HTTPException(403, "Only administrators can send this")
    site = await db.get(Site, h.site_id)
    if site is None or site.archived_at is not None or not site.enabled:
        raise HTTPException(409, {"message": "The site is disabled or archived"})
    await db.commit()                                   # don't hold the connection while NX answers
    result = await webhooks.send(manager.client_for(site), h, webhooks.flatten(body.fields))
    h = await db.get(Webhook, hook_id)
    webhooks.note(h, result, f"{user.label} (portal)")
    audit(db, h.tenant_id, "webhook.sent", user_id=user.id, site_id=h.site_id, ip=client_ip(request), webhook_id=h.id,
          name=h.name, ok=result["ok"], ms=result["ms"], detail=result["detail"], sent=result["sent"])
    await db.commit()
    if not result["ok"]:
        raise HTTPException(502, {"message": result["detail"]})
    return {**result, "webhook": _out(h, site.name)}


@router.get("/api/sites/{site_id}/nx-triggers")
async def site_triggers(site_id: int, user: User = Depends(require_admin), scope: Scope = Depends(get_scope),
                        db: AsyncSession = Depends(get_db)):
    from app.services.sites import describe_http_error
    site = await _get_site(db, scope, site_id)
    await db.commit()
    try:
        rows = await manager.client_for(site).get_triggers() or []
    except Exception as exc:  # noqa: BLE001 - shown on the form
        raise HTTPException(502, {"message": f"Couldn't read the site's soft triggers: {describe_http_error(exc)}"}) from None
    names = manager.device_names(site.id)
    out = []
    for t in rows if isinstance(rows, list) else []:
        devs = t.get("devices") or {}
        ids = [str(d).strip("{}") for d in devs.get("ids") or []]
        cams = [{"id": d, "name": names.get(d) or names.get("{" + d + "}") or d[:8]} for d in (list(names) if devs.get("all") else ids)]
        out.append({"id": str(t.get("triggerId") or "").strip("{}"), "name": t.get("name") or "(no name)",
                    "prolonged": bool(t.get("prolonged")), "all_cameras": bool(devs.get("all")),
                    "cameras": [{**c, "id": c["id"].strip("{}")} for c in cams]})
    return out


@router.get("/api/sites/{site_id}/buttons")
async def site_buttons(site_id: int, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    site = await _get_site(db, scope, site_id)
    rows = (await db.scalars(select(Webhook).where(Webhook.site_id == site.id, Webhook.button.is_(True),
                                                   Webhook.enabled.is_(True)).order_by(Webhook.name))).all()
    return [{"id": h.id, "name": h.name, "kind": h.kind, "kind_label": webhooks.KINDS[h.kind]} for h in rows]


async def _fields(request: Request) -> dict[str, str]:
    """The call's fields: the query string, then the body (JSON object, form, or plain text as `text`)."""
    fields = dict(request.query_params)
    raw = await request.body()
    if len(raw) > BODY_MAX:
        raise HTTPException(413, "Too large")
    ctype = request.headers.get("content-type", "")
    if raw:
        if "json" in ctype or raw.lstrip()[:1] in (b"{", b"["):
            try:
                data = json.loads(raw)
            except ValueError:
                raise HTTPException(400, "Body isn't valid JSON") from None
            fields.update(webhooks.flatten(data if isinstance(data, dict) else {"items": data}))
        elif "form" in ctype:
            fields.update({k: str(v) for k, v in (await request.form()).items()})
        else:
            fields["text"] = raw.decode("utf-8", "replace")[:webhooks.TEMPLATE_MAX]
    return fields


@public.api_route("/hook/{token}", methods=["GET", "POST", "PUT"])
async def call_hook(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    ip = client_ip(request)
    h = await db.scalar(select(Webhook).where(Webhook.token_hash == webhooks.token_hash(token)))
    if h is None:
        await security_guard.record(db, ip, "webhook", "failure", reason="unknown webhook")
        await db.commit()
        return JSONResponse({"ok": False, "detail": "Not found"}, status_code=404, headers=PRIVATE)
    if not h.enabled:
        return JSONResponse({"ok": False, "detail": "This webhook is switched off"}, status_code=403, headers=PRIVATE)
    site = await db.get(Site, h.site_id)
    if site is None or site.archived_at is not None or not site.enabled:
        return JSONResponse({"ok": False, "detail": "The site is disabled or archived"}, status_code=409, headers=PRIVATE)
    if not webhooks.allowed(h.id):
        return JSONResponse({"ok": False, "detail": f"Too many calls (at most {webhooks.RATE_PER_MIN} a minute)"},
                            status_code=429, headers={**PRIVATE, "Retry-After": "60"})
    fields = await _fields(request)
    hook_id = h.id
    await db.commit()
    t0 = time.monotonic()
    result = await webhooks.send(manager.client_for(site), h, fields)
    h = await db.get(Webhook, hook_id)
    webhooks.note(h, result, ip)
    await db.commit()
    return JSONResponse({"ok": result["ok"], "ms": round((time.monotonic() - t0) * 1000), "sent": result["sent"],
                         "detail": result["detail"]}, status_code=200 if result["ok"] else 502, headers=PRIVATE)
