"""A company's VPN connections to its sites (app/tunnels.py), on the Sites page. Company admins only; TWG in support
mode with platform.support. Keys and client files are write-only: the page only sees what tunnels.summary allows.

- GET    /api/tunnels            the company's connections, live state, and which sites use each
- POST   /api/tunnels            {kind: wireguard, name, config} | {kind: tailscale, name, auth_key}
- PUT    /api/tunnels/{id}       {name?, config? / auth_key?, enabled?}
- DELETE /api/tunnels/{id}       only when no site uses it (a Tailscale one also leaves the tailnet)
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import tunnels
from app.audit import audit
from app.db import get_db
from app.deps import client_ip, csrf_protect, require_admin
from app.models import Site, Tenant, Tunnel, User
from app.scope import Scope, get_scope
from app.security import encrypt

router = APIRouter(prefix="/api/tunnels", dependencies=[Depends(csrf_protect)])


class TunnelIn(BaseModel):
    kind: str = Field(pattern="^(wireguard|tailscale)$")
    name: str = Field(min_length=1, max_length=200)
    config: str = Field(default="", max_length=20_000)       # WireGuard client file
    auth_key: str = Field(default="", max_length=300)        # Tailscale


class TunnelPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    config: str = Field(default="", max_length=20_000)       # "" = keep
    auth_key: str = Field(default="", max_length=300)        # "" = keep
    enabled: bool | None = None


def _secret(kind: str, config: str, auth_key: str) -> tuple[str, dict]:
    """(the secret to store, the public info). Raises 400 with the parser's sentence."""
    try:
        if kind == "wireguard":
            return config, tunnels.parse_wireguard(config).summary()
        return tunnels.check_auth_key(auth_key), {}
    except ValueError as exc:
        raise HTTPException(400, {"message": str(exc)}) from exc


async def _out(db: AsyncSession, rows: list[Tunnel]) -> list[dict]:
    used: dict[int, list[dict]] = {}
    if rows:
        for sid, name, tid in (await db.execute(select(Site.id, Site.name, Site.tunnel_id).where(
                Site.tunnel_id.in_([t.id for t in rows]), Site.archived_at.is_(None)).order_by(Site.name))).all():
            used.setdefault(tid, []).append({"id": sid, "name": name})
    return [{"id": t.id, "tenant_id": t.tenant_id, "kind": t.kind, "kind_label": tunnels.KINDS[t.kind], "name": t.name,
             "enabled": t.enabled, "info": t.info or {}, "sites": used.get(t.id, []), "status": tunnels.supervisor.status(t.id),
             "updated_at": t.updated_at.isoformat() if t.updated_at else None} for t in rows]


async def _get(db: AsyncSession, scope: Scope, tunnel_id: int, write: bool = False) -> Tunnel:
    t = await db.scalar(select(Tunnel).where(Tunnel.id == tunnel_id, scope.where(Tunnel.tenant_id)))
    if t is None:
        raise HTTPException(404, "VPN connection not found")
    if write:
        scope.require_write(t.tenant_id)
    return t


@router.get("")
async def list_tunnels(user: User = Depends(require_admin), scope: Scope = Depends(get_scope),
                       db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(Tunnel).where(scope.where(Tunnel.tenant_id)).order_by(Tunnel.name))).all()
    return await _out(db, list(rows))


@router.post("")
async def create_tunnel(body: TunnelIn, request: Request, user: User = Depends(require_admin),
                        scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    tenant_id = scope.write_tenant()
    secret, info = _secret(body.kind, body.config, body.auth_key)
    t = Tunnel(tenant_id=tenant_id, kind=body.kind, name=body.name.strip(), secret_enc=encrypt(secret), info=info)
    db.add(t)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, {"message": f"A VPN connection named '{body.name}' already exists"}) from exc
    if body.kind == "tailscale":
        tenant = await db.get(Tenant, tenant_id)
        t.info = {"hostname": tunnels.tailscale_hostname(tenant.name, t.id)}
    audit(db, tenant_id, "tunnel.created", user_id=user.id, ip=client_ip(request), tunnel_id=t.id, name=t.name, kind=t.kind,
          **{k: v for k, v in (t.info or {}).items() if k in ("endpoint", "address", "hostname")})
    await db.commit()
    await tunnels.supervisor.apply(t)
    return (await _out(db, [t]))[0]


@router.put("/{tunnel_id}")
async def update_tunnel(tunnel_id: int, body: TunnelPatch, request: Request, user: User = Depends(require_admin),
                        scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    t = await _get(db, scope, tunnel_id, write=True)
    changed = []
    if body.name is not None and body.name.strip() != t.name:
        t.name = body.name.strip()
        changed.append("name")
    if body.config or body.auth_key:
        secret, info = _secret(t.kind, body.config, body.auth_key)
        t.secret_enc = encrypt(secret)
        t.info = {**info, **({"hostname": (t.info or {}).get("hostname", "")} if t.kind == "tailscale" else {})}
        changed.append("config" if t.kind == "wireguard" else "auth_key")
    if body.enabled is not None and body.enabled != t.enabled:
        t.enabled = body.enabled
        changed.append("enabled")
    audit(db, t.tenant_id, "tunnel.updated", user_id=user.id, ip=client_ip(request), tunnel_id=t.id, name=t.name,
          fields=changed, enabled=t.enabled)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, {"message": f"A VPN connection named '{body.name}' already exists"}) from exc
    await tunnels.supervisor.apply(t)
    return (await _out(db, [t]))[0]


@router.delete("/{tunnel_id}")
async def delete_tunnel(tunnel_id: int, request: Request, user: User = Depends(require_admin),
                        scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    t = await _get(db, scope, tunnel_id, write=True)
    users = (await db.scalars(select(Site.name).where(Site.tunnel_id == t.id))).all()
    if users:
        raise HTTPException(409, {"message": "Sites still use this VPN connection: " + ", ".join(users) +
                                  ". Pick another connection for them first (Edit site)."})
    await tunnels.supervisor.remove(t)
    audit(db, t.tenant_id, "tunnel.deleted", user_id=user.id, ip=client_ip(request), tunnel_id=t.id, name=t.name, kind=t.kind)
    await db.delete(t)
    await db.commit()
    return {"ok": True}
