"""Sign-in: password, then a second factor when the account has one (or must), and passkey sign-in.

Stages (ported from MCP-Control-Platform apps/api/src/routes/auth.ts):
  POST /login                password. Then one of:
    - straight in (no second factor and none required)
    - /login/2fa             authenticator code, or /login/recovery (a recovery code), or a passkey
    - /login/enroll          2FA is required but not set up: scan the QR, confirm a code, get recovery codes
  POST /login/passkey/*      "Sign in with a passkey" (no password, no code), or the passkey as second factor
Between stages the signed session cookie holds only `pending` (user id, stage, expiry), never `uid`, so a
half-signed-in browser can't call anything else. Every attempt goes to auth_events (bans, locks).
"""

import secrets
import time

from fastapi import APIRouter, Body, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import mfa, sessions
from app import security_guard as guard
from app.audit import audit
from app.db import get_db
from app.deps import client_ip, csrf_protect, render
from app.models import Tenant, User, UserSession, WebAuthnCredential
from app.security import hash_password, verify_password

router = APIRouter()

INVALID = "Invalid email or password."
MFA_TTL_S, ENROLL_TTL_S, PASSKEY_TTL_S = 5 * 60, 15 * 60, 5 * 60
MAX_CODE_TRIES = 5
# Checked against when the email is unknown, so the answer takes as long as for a real account.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def banned_page(request: Request, ban, *, api: bool = False):
    """403 for a banned IP: the blocked page, or JSON for /api/ calls."""
    until = None if ban.permanent else guard.aware(ban.expires_at)
    msg = "This address is blocked after repeated failed sign-ins."
    if api:
        msg += f" Try again after {until.strftime('%Y-%m-%d %H:%M')} UTC." if until else " Contact TWG Security to lift it."
        return JSONResponse({"detail": msg}, status_code=403)
    if not until:
        msg += " The block is permanent."
    response = render(request, "banned.html", until=until.isoformat() if until else None, ip=ban.ip, message=msg)
    response.status_code = 403
    return response


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//") and not value.startswith("/login"):
        return value[:500]
    return "/"


def _login_page(request: Request, **ctx):
    return render(request, "login.html", passkeys=mfa.passkeys_enabled(), rp_id=mfa.rp_id(), **ctx)


# ---------------------------------------------------------------- pending (between stages)
def _set_pending(request: Request, user: User, stage: str, ttl: int) -> None:
    nxt = (request.session.get("pending") or {}).get("next") or request.session.get("next")
    request.session.pop("uid", None)
    request.session["pending"] = {"uid": user.id, "stage": stage, "exp": time.time() + ttl,
                                  "tv": user.token_version or 0, "tries": 0, "next": _safe_next(nxt)}


async def _pending_user(request: Request, db: AsyncSession, stage: str) -> User | None:
    p = request.session.get("pending")
    if not p or p.get("stage") != stage or p.get("exp", 0) < time.time():
        return None
    user = await db.get(User, p["uid"])
    if user is None or not user.is_active or (user.token_version or 0) != p.get("tv", 0):
        return None
    tenant = await db.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        return None
    user._tenant = tenant
    return user


async def _finish(request: Request, db: AsyncSession, user: User, method: str, *, kind: str = "login") -> str:
    """Fully signed in: start the session, record it; returns where the user was headed."""
    nxt = _safe_next((request.session.get("pending") or {}).get("next") or request.session.get("next"))
    ip = client_ip(request)
    await sessions.start(request, db, user, method)
    await guard.record(db, ip, kind, "success", email=user.email, user=user, reason=f"signed in ({method})")
    audit(db, user.tenant_id, "login", user_id=user.id, ip=ip, method=method)
    await db.commit()
    return nxt


async def _failed(request: Request, db: AsyncSession, user: User | None, kind: str, reason: str, *, email: str = ""):
    """Record a failed attempt; returns the ban if this one crossed the line."""
    ip = client_ip(request)
    ban = await guard.record(db, ip, kind, "failure", email=email or (user.email if user else ""), reason=reason, user=user)
    if user is not None:
        audit(db, user.tenant_id, "login.failed", user_id=user.id, ip=ip, step=kind, reason=reason)
    await db.commit()
    return ban


# ---------------------------------------------------------------- password
@router.get("/login")
async def login_page(request: Request, next: str = "", reason: str = ""):
    if request.session.get("uid") and not reason:
        return RedirectResponse(_safe_next(next), 303)
    if reason:                    # sent here by a page that was signed out: whatever the cookie says is dead
        request.session.clear()
    request.session.pop("pending", None)
    if next:
        request.session["next"] = _safe_next(next)
    notice = {"signed_out": "You were signed out. Sign in again to keep receiving alarms on this screen.",
              "idle": "You were signed out after a period of inactivity."}.get(reason)
    return _login_page(request, notice=notice)


@router.post("/login", dependencies=[Depends(csrf_protect)])
async def login(request: Request, email: str = Form(...), password: str = Form(...),
                db: AsyncSession = Depends(get_db)):
    ip = client_ip(request)
    addr = email.strip().lower()
    # (A banned IP never gets here: app.main.BanGate answers it with the blocked page.)
    if await guard.is_account_locked(db, addr):
        verify_password(_DUMMY_HASH, password)
        await guard.record(db, ip, "login", "locked", email=addr, reason="account locked: too many failures")
        await db.commit()
        return _login_page(request, error=INVALID, email=email)
    user = await db.scalar(select(User).where(func.lower(User.email) == addr))
    if user is None:
        verify_password(_DUMMY_HASH, password)
        ban = await _failed(request, db, None, "login", "unknown email", email=addr)
        return banned_page(request, ban) if ban else _login_page(request, error=INVALID, email=email)
    if not verify_password(user.password_hash, password) or not user.is_active:
        ban = await _failed(request, db, user, "login", "wrong password" if user.is_active else "account deactivated")
        return banned_page(request, ban) if ban else _login_page(request, error=INVALID, email=email)
    tenant = await db.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        await guard.record(db, ip, "login", "denied", email=addr, reason="company sign-in disabled", user=user)
        audit(db, user.tenant_id, "login.failed", user_id=user.id, ip=ip, reason="company disabled")
        await db.commit()
        return _login_page(request, error="Your company's portal access is disabled. Contact TWG Security.", email=email)

    required = mfa.required_for(tenant)
    passkeys = mfa.passkeys_enabled() and await mfa.passkey_count(db, user.id) > 0
    if user.totp_enabled or (required and passkeys):
        _set_pending(request, user, "mfa", MFA_TTL_S)
        await guard.record(db, ip, "login", "success", email=addr, user=user, reason="password ok, second factor next")
        await db.commit()
        return RedirectResponse("/login/2fa", 303)
    if required:
        _set_pending(request, user, "enroll", ENROLL_TTL_S)
        await guard.record(db, ip, "login", "success", email=addr, user=user, reason="password ok, must set up 2FA")
        await db.commit()
        return RedirectResponse("/login/enroll", 303)
    return RedirectResponse(await _finish(request, db, user, "password"), 303)


# ---------------------------------------------------------------- second factor
async def _mfa_page(request: Request, db: AsyncSession, user: User, template="login_2fa.html", **ctx):
    has_passkeys = mfa.passkeys_enabled() and await mfa.passkey_count(db, user.id) > 0
    return render(request, template, email=user.email, totp=user.totp_enabled, passkeys=has_passkeys,
                  rp_id=mfa.rp_id(), **ctx)


def _restart(request: Request, message="That took too long. Sign in again."):
    request.session.pop("pending", None)
    return _login_page(request, error=message)


async def _code_attempt(request: Request, db: AsyncSession, check, kind: str, template: str):
    user = await _pending_user(request, db, "mfa")
    if user is None:
        return _restart(request)
    ok, reason = await check(user)
    if ok:
        return RedirectResponse(await _finish(request, db, user, kind, kind=kind), 303)
    p = dict(request.session["pending"])
    p["tries"] = p.get("tries", 0) + 1
    request.session["pending"] = p
    ban = await _failed(request, db, user, kind, reason)
    if ban:
        return banned_page(request, ban)
    if p["tries"] >= MAX_CODE_TRIES:
        return _restart(request, "Too many wrong codes. Sign in again.")
    msg = "That code was already used. Wait for the next one." if reason == "code already used" else "That code didn't work."
    return await _mfa_page(request, db, user, template, error=msg)


@router.get("/login/2fa")
async def mfa_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _pending_user(request, db, "mfa")
    return await _mfa_page(request, db, user) if user else RedirectResponse("/login", 303)


@router.post("/login/2fa", dependencies=[Depends(csrf_protect)])
async def mfa_submit(request: Request, code: str = Form(...), db: AsyncSession = Depends(get_db)):
    async def check(user):
        if not user.totp_enabled:
            return False, "no authenticator app set up"
        return mfa.check_totp(user, code)
    return await _code_attempt(request, db, check, "totp", "login_2fa.html")


@router.get("/login/recovery")
async def recovery_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _pending_user(request, db, "mfa")
    return await _mfa_page(request, db, user, "login_recovery.html") if user else RedirectResponse("/login", 303)


@router.post("/login/recovery", dependencies=[Depends(csrf_protect)])
async def recovery_submit(request: Request, code: str = Form(...), db: AsyncSession = Depends(get_db)):
    async def check(user):
        if user.totp_enabled and await mfa.use_recovery_code(db, user, code):
            left = await mfa.recovery_codes_left(db, user.id)
            audit(db, user.tenant_id, "mfa.recovery_used", user_id=user.id, ip=client_ip(request), left=left)
            return True, ""
        return False, "wrong recovery code"
    return await _code_attempt(request, db, check, "recovery", "login_recovery.html")


# ---------------------------------------------------------------- forced enrolment
@router.get("/login/enroll")
async def enroll_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _pending_user(request, db, "enroll")
    if user is None:
        return RedirectResponse("/login", 303)
    setup = mfa.pending_totp(user)
    if setup is None:
        setup = mfa.start_totp_enrolment(user)
        await db.commit()
    return render(request, "login_enroll.html", email=user.email, setup=setup, company=user._tenant.label)


@router.post("/login/enroll", dependencies=[Depends(csrf_protect)])
async def enroll_submit(request: Request, code: str = Form(...), db: AsyncSession = Depends(get_db)):
    user = await _pending_user(request, db, "enroll")
    if user is None:
        return _restart(request)
    codes = await mfa.enable_totp(db, user, code)
    if codes is None:
        ban = await _failed(request, db, user, "totp", "wrong code while setting up 2FA")
        if ban:
            return banned_page(request, ban)
        return render(request, "login_enroll.html", email=user.email, setup=mfa.pending_totp(user),
                      company=user._tenant.label,
                      error="That code didn't work. Check the phone's clock and try the next one.")
    audit(db, user.tenant_id, "mfa.enabled", user_id=user.id, ip=client_ip(request), during="sign-in")
    nxt = await _finish(request, db, user, "totp", kind="totp")
    return render(request, "recovery_codes.html", codes=codes, next=nxt)


# ---------------------------------------------------------------- passkeys
@router.post("/login/passkey/options", dependencies=[Depends(csrf_protect)])
async def passkey_options(request: Request, db: AsyncSession = Depends(get_db)):
    """Any passkey for this site; as a second factor, only the pending user's (their keys listed)."""
    if not mfa.passkeys_enabled():
        return JSONResponse({"detail": "Passkeys are turned off."}, status_code=400)
    pending = await _pending_user(request, db, "mfa")
    allow = None
    if pending is not None:
        allow = (await db.scalars(select(WebAuthnCredential).where(WebAuthnCredential.user_id == pending.id))).all()
    options, challenge = mfa.authentication_options(allow)
    request.session["pk_login"] = {"c": challenge, "exp": time.time() + PASSKEY_TTL_S,
                                   "uid": pending.id if pending else None}
    return options


@router.post("/login/passkey/verify", dependencies=[Depends(csrf_protect)])
async def passkey_verify(request: Request, body: dict = Body(...), db: AsyncSession = Depends(get_db)):
    state = request.session.pop("pk_login", None)
    credential = body.get("credential")
    if not state or state.get("exp", 0) < time.time() or not isinstance(credential, dict):
        return JSONResponse({"detail": "That took too long. Try again."}, status_code=400)
    cred = await mfa.find_credential(db, credential)
    user = await db.get(User, cred.user_id) if cred else None
    if cred is None or user is None or (state.get("uid") and state["uid"] != user.id):
        ban = await _failed(request, db, None, "passkey", "unknown passkey")
        return JSONResponse({"detail": "That passkey isn't registered here."}, status_code=403 if ban else 400)
    try:
        mfa.verify_assertion(credential, state["c"], cred)
    except Exception as e:  # noqa: BLE001 - py_webauthn raises several types
        ban = await _failed(request, db, user, "passkey", f"passkey check failed: {type(e).__name__}")
        return JSONResponse({"detail": "The passkey couldn't be verified."}, status_code=403 if ban else 400)
    tenant = await db.get(Tenant, user.tenant_id)
    if not user.is_active or tenant is None or not tenant.is_active:
        await guard.record(db, client_ip(request), "passkey", "denied", email=user.email, user=user,
                           reason="account or company sign-in disabled")
        await db.commit()
        return JSONResponse({"detail": "This account can't sign in. Contact your administrator."}, status_code=403)
    return {"redirect": await _finish(request, db, user, "passkey", kind="passkey")}


@router.post("/logout", dependencies=[Depends(csrf_protect)])
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    uid, tid, sid = request.session.get("uid"), request.session.get("tid"), request.session.get("sid")
    if uid and tid:
        row = await db.get(UserSession, sid) if sid else None
        if row is not None and row.user_id == uid:
            await sessions.revoke(db, row, "signed out")
        audit(db, tid, "logout", user_id=uid, ip=client_ip(request))
        await db.commit()
    request.session.clear()
    return RedirectResponse("/login", 303)
