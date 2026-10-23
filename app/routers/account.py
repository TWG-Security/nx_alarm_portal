"""Your account (every signed-in user) and the admin's view of a company's users' sign-in security.

- GET  /account                                   the page
- GET  /api/account                               profile, 2FA, passkeys, sessions
- POST /api/account/password                      change it (current password needed); other sessions end
- POST /api/account/totp/setup|enable|disable     authenticator app
- POST /api/account/recovery-codes                new set (old ones stop working)
- POST /api/account/passkeys/options, POST /api/account/passkeys        add a passkey
- POST /api/account/passkeys/{id}/test/options|test, DELETE /api/account/passkeys/{id}
- DELETE /api/account/sessions/{sid}, POST /api/account/sign-out-everywhere
- GET|PUT /api/account/texts, POST /api/account/texts/opt-in|test   your own alarm texts (number, which, when, sites)
- GET|POST /api/account/tokens, DELETE /api/account/tokens/{id}      your API tokens for the MCP server (/mcp)
Admins (their company, or TWG support):
- GET  /api/users/{id}/security                   2FA state and signed-in browsers
- POST /api/users/{id}/reset-2fa                  clears the authenticator app and recovery codes (optionally passkeys)
- PUT  /api/users/{id}/mfa-exempt                 turn two-step sign-in off for one person (or require it again)
- DELETE /api/users/{id}/sessions[/{sid}]         sign them out (everywhere, or one browser; everywhere ends API tokens too)
- DELETE /api/users/{id}/tokens/{tid}             revoke one of their API tokens
"""

import time

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import api_tokens, mfa, passwords, platform_settings, sessions
from app.audit import audit
from app.db import get_db
from app.deps import client_ip, csrf_protect, current_user, render, require_admin
from app.models import ApiToken, Tenant, User, UserSession, WebAuthnCredential
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
        "mfa_required": mfa.required_for(user._tenant, user), "totp_enabled": user.totp_enabled,
        "totp_pending": bool(user.totp_secret_enc) and not user.totp_enabled,
        "recovery_left": await mfa.recovery_codes_left(db, user.id) if user.totp_enabled else 0,
        "passkeys_enabled": mfa.passkeys_enabled(), "rp_id": mfa.rp_id(),
        "passkeys": [_passkey_dict(c) for c in keys],
        "sessions": [sessions.session_dict(r, request.session.get("sid")) for r in rows],
        "password_hint": passwords.hint(),
    }


# ---------------------------------------------------------------- your own alarm texts
OPT_IN_PER_DAY = 5             # opt-in requests one person can trigger in 24 h (numbers typed here get texted)


async def _texts(db: AsyncSession, user: User) -> dict:
    from app.models import Site, SmsLog
    last = await db.scalar(select(SmsLog).where(SmsLog.user_id == user.id).order_by(SmsLog.id.desc()).limit(1))
    sites = (await db.scalars(select(Site).where(Site.tenant_id == user.tenant_id, Site.archived_at.is_(None))
                              .order_by(Site.name))).all()
    return {"ready": platform_settings.current().sms_ready, "phone": user.phone, "sms_level": user.sms_level,
            "sms_when": user.sms_when, "sms_sites": user.sms_sites, "sms_consent": user.sms_consent,
            "sms_consent_at": _iso(user.sms_consent_at), "sms_optin_sent_at": _iso(user.sms_optin_sent_at),
            "last_text": {"outcome": last.outcome, "ts": _iso(last.ts), "error": last.error, "purpose": last.purpose} if last else None,
            "sites": [{"id": x.id, "name": x.name} for x in sites]}


async def _opt_in_budget(db: AsyncSession, user: User) -> None:
    from datetime import timedelta
    from sqlalchemy import func
    from app.models import SmsLog
    since = sessions.utcnow() - timedelta(days=1)
    n = await db.scalar(select(func.count()).select_from(SmsLog).where(SmsLog.user_id == user.id, SmsLog.purpose == "optin",
                                                                        SmsLog.ts >= since))
    if (n or 0) >= OPT_IN_PER_DAY:
        raise HTTPException(429, f"That's {OPT_IN_PER_DAY} opt-in texts in a day. Try again tomorrow, or ask an admin.")


@router.get("/api/account/texts")
async def my_texts(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await _texts(db, user)


class MyTextsIn(BaseModel):
    phone: str = Field(default="", max_length=30)
    sms_level: str = Field(pattern="^(off|critical|alarm)$")
    sms_when: str = Field(default="raised", pattern="^(raised|confirmed|both)$")
    sms_sites: list[int] | None = Field(default=None, max_length=1000)       # None = every site


@router.put("/api/account/texts")
async def set_my_texts(body: MyTextsIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Your own number and which alarms text you. A new number gets the "reply YES" request first, like on the Users page."""
    from app.routers.api import _ask_consent, _phone, _site_choice
    me = await db.get(User, user.id)
    phone = _phone(body.phone)
    level = body.sms_level if phone else "off"
    sites = await _site_choice(db, me.tenant_id, body.sms_sites)
    changed = [k for k, v in (("phone", phone), ("sms_level", level), ("sms_when", body.sms_when), ("sms_sites", sites))
               if getattr(me, k) != v]
    if not changed:
        return await _texts(db, me)
    if "phone" in changed and phone:
        await _opt_in_budget(db, me)
    me.phone, me.sms_level, me.sms_when, me.sms_sites = phone, level, body.sms_when, sites
    if "phone" in changed:                        # consent belongs to a number: a new one is asked again
        me.sms_consent, me.sms_consent_at, me.sms_optin_sent_at = "none", None, None
    ask = bool({"phone", "sms_level"} & set(changed)) and _ask_consent(me)
    audit(db, me.tenant_id, "user.updated", user_id=me.id, ip=client_ip(request), target=me.email, fields=changed, self_service=True)
    await db.commit()
    if ask:
        from app import sms
        await sms.request_opt_in(me.id, me.label)
    return await _texts(db, me)


@router.post("/api/account/texts/opt-in")
async def my_opt_in(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    from datetime import datetime, timedelta, timezone
    from app import sms
    me = await db.get(User, user.id)
    if not me.phone:
        raise HTTPException(400, "Add your mobile number first")
    if not platform_settings.current().sms_ready:
        raise HTTPException(400, "Text messages aren't switched on for this portal yet")
    if me.sms_consent == "yes":
        raise HTTPException(400, "You've already replied YES")
    if me.sms_consent == "stopped":
        raise HTTPException(400, "You replied STOP, so carriers block texts to you. Text START or YES to the portal's number to sign up again.")
    sent = sessions.aware(me.sms_optin_sent_at)
    if sent is not None and sent > datetime.now(timezone.utc) - timedelta(minutes=1):
        raise HTTPException(429, "An opt-in request went less than a minute ago")
    await _opt_in_budget(db, me)
    if me.sms_level == "off":
        me.sms_level = "alarm"
    me.sms_consent = "pending"
    audit(db, me.tenant_id, "sms.opt_in_requested", user_id=me.id, ip=client_ip(request), target=me.email, self_service=True)
    await db.commit()
    await sms.request_opt_in(me.id, me.label)
    return await _texts(db, me)


@router.post("/api/account/texts/test")
async def my_test_text(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    from app import sms
    me = await db.get(User, user.id)
    if not me.phone or me.sms_consent != "yes":
        raise HTTPException(400, "Reply YES to the opt-in text first")
    audit(db, me.tenant_id, "user.test_text", user_id=me.id, ip=client_ip(request), target=me.email, self_service=True)
    await db.commit()
    outcome, error = await sms.send_test(me.phone, me.label, user=me)
    return {"outcome": outcome, "error": error}


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
    if mfa.required_for(user._tenant, user) and not await mfa.passkey_count(db, user.id):
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
    if mfa.required_for(user._tenant, user) and not user.totp_enabled and await mfa.passkey_count(db, user.id) <= 1:
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


# ---------------------------------------------------------------- your iPhone app notifications (app/apns.py)
async def _push_view(db: AsyncSession, user: User) -> dict:
    from app import apns
    from app.models import PushDevice
    rows = (await db.scalars(select(PushDevice).where(PushDevice.user_id == user.id).order_by(PushDevice.id))).all()
    return {"ready": apns.configured(), "push_level": apns.level_of(user), "push_when": user.push_when or "raised",
            "devices": [{"id": d.id, "environment": d.environment, "app_version": d.app_version, "ios_version": d.ios_version,
                         "created_at": _iso(d.created_at), "last_push_at": _iso(d.last_push_at)} for d in rows]}


@router.get("/api/account/push")
async def get_push(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await _push_view(db, user)


@router.put("/api/account/push")
async def put_push(request: Request, body: dict = Body(...), user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    from app.routers.mobile import PushIn, save_push
    await save_push(db, request, user, user, PushIn(**{k: body.get(k) for k in ("push_level", "push_when")}))
    await db.commit()
    return await _push_view(db, user)


@router.delete("/api/account/push/devices/{device_id}")
async def remove_push_device(device_id: int, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    from app.models import PushDevice
    d = await db.get(PushDevice, device_id)
    if d is None or d.user_id != user.id:
        raise HTTPException(404, "No such iPhone")
    await db.delete(d)
    audit(db, user.tenant_id, "push.device_removed", user_id=user.id, ip=client_ip(request))
    await db.commit()
    return await _push_view(db, user)


@router.post("/api/account/push/test")
async def test_push(user: User = Depends(current_user)):
    from app import apns
    if not apns.configured():
        raise HTTPException(400, "App notifications aren't set up on the server yet")
    results = await apns.send_test(user.id)
    if not results:
        raise HTTPException(400, "No iPhone is signed in to the app as you")
    return {"results": results}


# ---------------------------------------------------------------- your API tokens (the MCP server, app/mcp_server.py)
class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    days: int | None = Field(default=None, ge=1, le=3650)        # None = until revoked


def _no_mcp(request: Request) -> None:
    """An AI holding a token can't make, list or revoke tokens: only a person signed in to the website can."""
    if request.scope.get("twg_mcp"):
        raise HTTPException(403, "API tokens are managed on the Account page, not through the MCP server")


def mcp_url(request: Request) -> str:
    from app.config import get_settings
    return get_settings().webauthn_origins.split(",")[0].strip().rstrip("/") + "/mcp"


@router.get("/api/account/tokens")
async def list_tokens(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    _no_mcp(request)
    return {"tokens": [api_tokens.token_dict(t) for t in await api_tokens.active(db, user.id)], "mcp_url": mcp_url(request)}


@router.post("/api/account/tokens")
async def create_token(body: TokenIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    _no_mcp(request)
    try:
        row, token = await api_tokens.create(db, user, body.name, body.days)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    audit(db, user.tenant_id, "api_token.created", user_id=user.id, ip=client_ip(request), name=row.name,
          hint=row.hint, days=body.days)
    await db.commit()
    return {**api_tokens.token_dict(row), "token": token, "mcp_url": mcp_url(request)}


@router.delete("/api/account/tokens/{token_id}")
async def revoke_token(token_id: int, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    _no_mcp(request)
    row = await db.get(ApiToken, token_id)
    if row is None or row.user_id != user.id or row.revoked_at is not None:
        raise HTTPException(404, "No such token")
    await api_tokens.revoke(row, "revoked by the user")
    audit(db, user.tenant_id, "api_token.revoked", user_id=user.id, ip=client_ip(request), name=row.name, hint=row.hint)
    await db.commit()
    return {"revoked": token_id}


# ---------------------------------------------------------------- admins: a company's users
async def _target(db: AsyncSession, scope: Scope, user_id: int, *, write: bool = False) -> User:
    target = await db.scalar(select(User).where(User.id == user_id, scope.where(User.tenant_id), User.deleted_at.is_(None)))
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
    return {"totp_enabled": target.totp_enabled, "mfa_required": mfa.required_for(tenant, target),
            "mfa_exempt": target.mfa_exempt, "company_requires": mfa.required_for(tenant),
            "recovery_left": await mfa.recovery_codes_left(db, target.id) if target.totp_enabled else 0,
            "passkeys": [_passkey_dict(c) for c in await _passkeys(db, target.id)],
            "sessions": [sessions.session_dict(r, request.session.get("sid")) for r in await sessions.active(db, [target.id])],
            "tokens": [api_tokens.token_dict(t) for t in await api_tokens.active(db, target.id)]}


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


class ExemptIn(BaseModel):
    exempt: bool


@router.put("/api/users/{user_id}/mfa-exempt")
async def mfa_exempt(user_id: int, body: ExemptIn, request: Request, user: User = Depends(require_admin),
                     scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Turn two-step sign-in off for one person (the company requirement no longer applies to them, and their
    authenticator app + recovery codes are removed so a password signs them in; passkeys stay, they're a sign-in of
    their own), or require it again (asked to set it up at their next sign-in)."""
    target = await _target(db, scope, user_id, write=True)
    target.mfa_exempt = body.exempt
    if body.exempt:
        await mfa.disable_totp(db, target)
    audit(db, target.tenant_id, "mfa.exempted" if body.exempt else "mfa.required_again", user_id=user.id,
          ip=client_ip(request), target=target.email)
    await db.commit()
    return {"mfa_exempt": target.mfa_exempt, "totp_enabled": target.totp_enabled}


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


@router.delete("/api/users/{user_id}/tokens/{token_id}")
async def revoke_user_token(user_id: int, token_id: int, request: Request, user: User = Depends(require_admin),
                            scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    _no_mcp(request)
    target = await _target(db, scope, user_id, write=True)
    row = await db.get(ApiToken, token_id)
    if row is None or row.user_id != target.id or row.revoked_at is not None:
        raise HTTPException(404, "No such token")
    await api_tokens.revoke(row, f"revoked by {user.email}")
    audit(db, target.tenant_id, "api_token.revoked_by_admin", user_id=user.id, ip=client_ip(request), target=target.email,
          name=row.name, hint=row.hint)
    await db.commit()
    return {"revoked": token_id}


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
