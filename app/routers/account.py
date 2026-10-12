"""Your account (every signed-in user) and the admin's view of a company's users' sign-in security.

- GET  /account                                   the page
- GET  /api/account                               profile, 2FA, passkeys, sessions
- POST /api/account/password                      change it (current password needed); other sessions end
- POST /api/account/totp/setup|enable|disable     authenticator app
- POST /api/account/recovery-codes                new set (old ones stop working)
- POST /api/account/passkeys/options, POST /api/account/passkeys        add a passkey
- POST /api/account/passkeys/{id}/test/options|test, DELETE /api/account/passkeys/{id}
- DELETE /api/account/sessions/{sid}, POST /api/account/sign-out-everywhere
Admins (their company, or TWG support):
- GET  /api/users/{id}/security                   2FA state and signed-in browsers
- POST /api/users/{id}/reset-2fa                  clears the authenticator app and recovery codes (optionally passkeys)
- DELETE /api/users/{id}/sessions[/{sid}]         sign them out (everywhere, or one browser)
"""

import time

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import mfa, passwords, platform_settings, sessions
from app.audit import audit
from app.db import get_db
from app.deps import client_ip, csrf_protect, current_user, render, require_admin
from app.models import Tenant, User, UserSession, WebAuthnCredential
from app.scope import Scope, get_scope
from app.security import hash_password, verify_password

router = APIRouter(dependencies=[Depends(csrf_protect)])
REG_TTL_S = 5 * 60


def _iso(dt):
    dt = sessions.aware(dt)
    return dt.isoformat() if dt else None


def _passkey_dict(c: WebAuthnCredential) -> dict:
    return {"id": c.id, "name": c.name, "created_at": _iso(c.created_at), "last_used_at": _iso(c.last_used_at),
            "tested_at": _iso(c.tested_at)}


async def _passkeys(db: AsyncSession, user_id: int) -> list[WebAuthnCredential]:
    return list((await db.scalars(select(WebAuthnCredential).where(WebAuthnCredential.user_id == user_id)
                                  .order_by(WebAuthnCredential.created_at))).all())


@router.get("/account")
async def account_page(request: Request, user: User = Depends(current_user), scope: Scope = Depends(get_scope)):
    return render(request, "account.html", user, scope=scope, page="account")


@router.get("/api/account")
async def account(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    keys = await _passkeys(db, user.id)
    rows = await sessions.active(db, [user.id])
    return {
        "email": user.email, "display_name": user.display_name, "company": user._tenant.label,
        "role": user.role, "password_changed_at": _iso(user.password_changed_at),
        "mfa_required": mfa.required_for(user._tenant), "totp_enabled": user.totp_enabled,
        "totp_pending": bool(user.totp_secret_enc) and not user.totp_enabled,
        "recovery_left": await mfa.recovery_codes_left(db, user.id) if user.totp_enabled else 0,
        "passkeys_enabled": mfa.passkeys_enabled(), "rp_id": mfa.rp_id(),
        "passkeys": [_passkey_dict(c) for c in keys],
        "sessions": [sessions.session_dict(r, request.session.get("sid")) for r in rows],
        "password_hint": passwords.hint(),
    }


# ---------------------------------------------------------------- password
class PasswordIn(BaseModel):
    current: str = Field(min_length=1, max_length=200)
    new: str = Field(min_length=1, max_length=200)


@router.post("/api/account/password")
async def change_password(body: PasswordIn, request: Request, user: User = Depends(current_user),
                          db: AsyncSession = Depends(get_db)):
    if not verify_password(user.password_hash, body.current):
        raise HTTPException(400, "Your current password isn't right.")
    if body.new == body.current:
        raise HTTPException(400, "The new password must be different.")
    problem = passwords.check(body.new)
    if problem:
        raise HTTPException(400, problem)
    user.password_hash, user.password_changed_at = hash_password(body.new), sessions.utcnow()
    ended = await sessions.revoke_all(db, user, "password changed", keep_sid=request.session.get("sid"))
    audit(db, user.tenant_id, "password.changed", user_id=user.id, ip=client_ip(request), other_sessions_ended=ended)
    await db.commit()
    return {"changed": True, "other_sessions_ended": ended}


# ---------------------------------------------------------------- authenticator app
class CodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=12)


@router.post("/api/account/totp/setup")
async def totp_setup(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if user.totp_enabled:
        raise HTTPException(409, "Two-step sign-in is already on. Turn it off first to move it to a new phone.")
    setup = mfa.start_totp_enrolment(user)
    await db.commit()
    return setup


@router.post("/api/account/totp/enable")
async def totp_enable(body: CodeIn, request: Request, user: User = Depends(current_user),
                      db: AsyncSession = Depends(get_db)):
    codes = await mfa.enable_totp(db, user, body.code)
    if codes is None:
        raise HTTPException(400, "That code didn't work. Check the phone's clock and try the next one.")
    audit(db, user.tenant_id, "mfa.enabled", user_id=user.id, ip=client_ip(request))
    await db.commit()
    return {"recovery_codes": codes}


@router.post("/api/account/totp/disable")
async def totp_disable(body: CodeIn, request: Request, user: User = Depends(current_user),
                       db: AsyncSession = Depends(get_db)):
    if not user.totp_enabled:
        raise HTTPException(400, "Two-step sign-in isn't on.")
    if mfa.required_for(user._tenant) and not await mfa.passkey_count(db, user.id):
        raise HTTPException(400, f"{user._tenant.label} requires two-step sign-in, so it can't be turned off. "
                                 "To move it to a new phone, add a passkey first, or ask an admin to reset it.")
    ok, _ = mfa.check_totp(user, body.code)
    if not ok:
        raise HTTPException(400, "That code didn't work.")
    await mfa.disable_totp(db, user)
    audit(db, user.tenant_id, "mfa.disabled", user_id=user.id, ip=client_ip(request))
    await db.commit()
    return {"totp_enabled": False}


@router.post("/api/account/recovery-codes")
async def regenerate_codes(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if not user.totp_enabled:
        raise HTTPException(400, "Turn on two-step sign-in first.")
    codes = await mfa.new_recovery_codes(db, user)
    audit(db, user.tenant_id, "mfa.recovery_regenerated", user_id=user.id, ip=client_ip(request))
    await db.commit()
    return {"recovery_codes": codes}


# ---------------------------------------------------------------- passkeys
def _require_passkeys():
    if not mfa.passkeys_enabled():
        raise HTTPException(400, "Passkeys are turned off for this portal.")


@router.post("/api/account/passkeys/options")
async def passkey_register_options(request: Request, user: User = Depends(current_user),
                                   db: AsyncSession = Depends(get_db)):
    _require_passkeys()
    options, challenge = mfa.registration_options(user, await _passkeys(db, user.id))
    request.session["pk_reg"] = {"c": challenge, "exp": time.time() + REG_TTL_S}
    return options


@router.post("/api/account/passkeys")
async def passkey_register(request: Request, body: dict = Body(...), user: User = Depends(current_user),
                           db: AsyncSession = Depends(get_db)):
    _require_passkeys()
    state = request.session.pop("pk_reg", None)
    credential = body.get("credential")
    if not state or state.get("exp", 0) < time.time() or not isinstance(credential, dict):
        raise HTTPException(400, "That took too long. Try again.")
    try:
        fields = mfa.verify_registration(credential, state["c"])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"The passkey couldn't be registered ({type(e).__name__}).") from None
    if await db.scalar(select(WebAuthnCredential.id).where(WebAuthnCredential.credential_id == fields["credential_id"])):
        raise HTTPException(409, "That passkey is already registered.")
    name = str(body.get("name") or "").strip()[:100] or "Passkey"
    row = WebAuthnCredential(user_id=user.id, name=name, **fields)
    db.add(row)
    await db.flush()
    audit(db, user.tenant_id, "passkey.added", user_id=user.id, ip=client_ip(request), name=name)
    await db.commit()
    return _passkey_dict(row)


async def _own_key(db, user, key_id) -> WebAuthnCredential:
    row = await db.get(WebAuthnCredential, key_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, "No such passkey")
    return row


@router.post("/api/account/passkeys/{key_id}/test/options")
async def passkey_test_options(key_id: int, request: Request, user: User = Depends(current_user),
                               db: AsyncSession = Depends(get_db)):
    _require_passkeys()
    row = await _own_key(db, user, key_id)
    options, challenge = mfa.authentication_options([row])
    request.session["pk_test"] = {"c": challenge, "exp": time.time() + REG_TTL_S, "id": row.id}
    return options


@router.post("/api/account/passkeys/{key_id}/test")
async def passkey_test(key_id: int, request: Request, body: dict = Body(...), user: User = Depends(current_user),
                       db: AsyncSession = Depends(get_db)):
    row = await _own_key(db, user, key_id)
    state = request.session.pop("pk_test", None)
    credential = body.get("credential")
    if not state or state.get("id") != row.id or state.get("exp", 0) < time.time() or not isinstance(credential, dict):
        raise HTTPException(400, "That took too long. Try again.")
    if str(credential.get("rawId") or credential.get("id")) != row.credential_id:
        raise HTTPException(400, "That was a different passkey.")
    try:
        mfa.verify_assertion(credential, state["c"], row)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"The passkey didn't pass ({type(e).__name__}).") from None
    row.tested_at = sessions.utcnow()
    await db.commit()
    return _passkey_dict(row)


@router.delete("/api/account/passkeys/{key_id}")
async def passkey_remove(key_id: int, request: Request, user: User = Depends(current_user),
                         db: AsyncSession = Depends(get_db)):
    row = await _own_key(db, user, key_id)
    if mfa.required_for(user._tenant) and not user.totp_enabled and await mfa.passkey_count(db, user.id) <= 1:
        raise HTTPException(400, f"{user._tenant.label} requires two-step sign-in and this is your only second step. "
                                 "Set up the authenticator app or add another passkey first.")
    await db.delete(row)
    audit(db, user.tenant_id, "passkey.removed", user_id=user.id, ip=client_ip(request), name=row.name)
    await db.commit()
    return {"removed": key_id}


# ---------------------------------------------------------------- your sessions
@router.delete("/api/account/sessions/{sid}")
async def end_session(sid: str, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    row = await db.get(UserSession, sid)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, "No such session")
    await sessions.revoke(db, row, "ended by the user")
    audit(db, user.tenant_id, "session.ended", user_id=user.id, ip=client_ip(request), session_ip=row.ip)
    await db.commit()
    return {"ended": sid, "was_current": sid == request.session.get("sid")}


@router.post("/api/account/sign-out-everywhere")
async def sign_out_everywhere(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    n = await sessions.revoke_all(db, user, "signed out everywhere")
    audit(db, user.tenant_id, "session.sign_out_everywhere", user_id=user.id, ip=client_ip(request), sessions=n)
    await db.commit()
    request.session.clear()
    return {"ended": n}


# ---------------------------------------------------------------- admins: a company's users
async def _target(db: AsyncSession, scope: Scope, user_id: int, *, write: bool = False) -> User:
    target = await db.scalar(select(User).where(User.id == user_id, scope.where(User.tenant_id)))
    if target is None:
        raise HTTPException(404)
    if write:
        scope.require_write(target.tenant_id)
    return target


@router.get("/api/users/{user_id}/security")
async def user_security(user_id: int, request: Request, user: User = Depends(require_admin),
                        scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    target = await _target(db, scope, user_id)
    tenant = await db.get(Tenant, target.tenant_id)
    return {"totp_enabled": target.totp_enabled, "mfa_required": mfa.required_for(tenant),
            "recovery_left": await mfa.recovery_codes_left(db, target.id) if target.totp_enabled else 0,
            "passkeys": [_passkey_dict(c) for c in await _passkeys(db, target.id)],
            "sessions": [sessions.session_dict(r, request.session.get("sid")) for r in await sessions.active(db, [target.id])]}


class ResetIn(BaseModel):
    passkeys: bool = False


@router.post("/api/users/{user_id}/reset-2fa")
async def reset_2fa(user_id: int, body: ResetIn, request: Request, user: User = Depends(require_admin),
                    scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    target = await _target(db, scope, user_id, write=True)
    await mfa.disable_totp(db, target)
    removed = 0
    if body.passkeys:
        removed = len(await _passkeys(db, target.id))
        await db.execute(delete(WebAuthnCredential).where(WebAuthnCredential.user_id == target.id))
    audit(db, target.tenant_id, "mfa.reset", user_id=user.id, ip=client_ip(request), target=target.email,
          passkeys_removed=removed)
    await db.commit()
    return {"reset": True, "passkeys_removed": removed}


@router.delete("/api/users/{user_id}/sessions")
async def end_user_sessions(user_id: int, request: Request, user: User = Depends(require_admin),
                            scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    target = await _target(db, scope, user_id, write=True)
    keep = request.session.get("sid") if target.id == user.id else None
    n = await sessions.revoke_all(db, target, f"signed out by {user.email}", keep_sid=keep)
    audit(db, target.tenant_id, "session.revoked_by_admin", user_id=user.id, ip=client_ip(request), target=target.email,
          sessions=n)
    await db.commit()
    return {"ended": n}


@router.delete("/api/users/{user_id}/sessions/{sid}")
async def end_user_session(user_id: int, sid: str, request: Request, user: User = Depends(require_admin),
                           scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    target = await _target(db, scope, user_id, write=True)
    row = await db.get(UserSession, sid)
    if row is None or row.user_id != target.id:
        raise HTTPException(404, "No such session")
    await sessions.revoke(db, row, f"signed out by {user.email}")
    audit(db, target.tenant_id, "session.revoked_by_admin", user_id=user.id, ip=client_ip(request), target=target.email,
          session_ip=row.ip)
    await db.commit()
    return {"ended": sid}


# ---------------------------------------------------------------- company setting: require 2FA
class CompanyMfaIn(BaseModel):
    require_2fa: bool


@router.get("/api/settings/security")
async def company_security(user: User = Depends(require_admin), scope: Scope = Depends(get_scope),
                           db: AsyncSession = Depends(get_db)):
    tenant = scope.tenant or user._tenant
    snap = platform_settings.current()
    decided_by = "platform" if tenant.is_platform or snap.mfa_customers == "all" else "company"
    return {"require_2fa": mfa.required_for(tenant), "decided_by": decided_by, "company": tenant.label}


@router.put("/api/settings/security")
async def put_company_security(body: CompanyMfaIn, request: Request, user: User = Depends(require_admin),
                               scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    tid = scope.write_tenant()
    tenant = await db.get(Tenant, tid)
    if tenant.is_platform or platform_settings.current().mfa_customers == "all":
        raise HTTPException(400, "TWG Security sets this on the Platform page.")
    settings = dict(tenant.settings or {})
    settings["require_2fa"] = body.require_2fa
    tenant.settings = settings
    audit(db, tid, "settings.security", user_id=user.id, ip=client_ip(request), require_2fa=body.require_2fa)
    await db.commit()
    return {"require_2fa": body.require_2fa, "decided_by": "company", "company": tenant.label}
