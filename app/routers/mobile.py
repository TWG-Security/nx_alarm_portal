"""Sign-in for the iPhone app (docs/IOS_APP_BRIEF.md section 6.1).

- POST /api/mobile/login        {email, password, device_name} -> {token, user} | {step: "totp", challenge} | {step: "enroll"}
- POST /api/mobile/login/totp   {challenge, code}: a 6-digit code or a recovery code -> {token, user}
- POST /api/mobile/logout       (Bearer) ends this phone's token only
- GET  /api/mobile/me           (Bearer) who is signed in
- GET  /api/mobile/dispatches   (Bearer) my dispatch links on calls still in progress (the guard screen's list)
- POST /api/mobile/passkey/options {device_name, challenge?} -> {challenge_id, options}; POST /api/mobile/passkey/verify
  {challenge_id, credential} -> {token, user}: a passkey sign-in (any passkey for the site), or with the two-step
  challenge only that person's passkeys as the second factor; same checks as /login/passkey/*
- GET  /.well-known/apple-app-site-association   so iOS offers the portal's passwords and passkeys in the app
- POST /api/mobile/share (Bearer) -> {token, name}: my location-share token without a dispatch ("On duty")
- POST /api/mobile/devices (Bearer) {apns_token, environment, app_version, ios_version}; DELETE /api/mobile/devices/{t}
  this iPhone's notifications (app/apns.py); PUT /api/mobile/me/push {push_level, push_when} -> Me

The same rules as the website's sign-in (app/routers/auth.py): every attempt goes to auth_events, so failures lock
the account and ban the address (a banned address gets 403 from BanGate before reaching here), a disabled company
can't sign in, and two-step sign-in is asked for or required exactly as on the web. What the app can't do yet answers
400 with what to do on the website instead: passkey-only two-step, setting up two-step, an expired password.

The token ("twgm_…", shown once) is a user_sessions row whose id is its SHA-256 (app/sessions.py start_mobile): sent
as "Authorization: Bearer" it works on every /api/ and /media/ endpoint, for the user's own company. It ends when
signed out here, revoked on the Account page or by an admin, at sign out everywhere, when the user is disabled, or
after MOBILE_IDLE_DAYS unused. The two-step challenge lives in memory (one server process): 5 minutes, single use,
MAX_CODE_TRIES wrong codes.
"""
import hashlib
import json
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import apns, mfa, passwords, sessions
from app import security_guard as guard
from app.audit import audit
from app.db import get_db
from app.deps import client_ip, current_user
from app.models import Tenant, Tracker, User, UserSession
from app.routers.auth import _DUMMY_HASH, INVALID, MAX_CODE_TRIES, MFA_TTL_S, banned_page
from app.security import verify_password

router = APIRouter()

_challenges: dict[str, dict] = {}      # sha256(challenge) -> {uid, tv, exp, tries, device}


class LoginIn(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=200)
    device_name: str = Field(default="", max_length=300)


class CodeIn(BaseModel):
    challenge: str = Field(min_length=10, max_length=200)
    code: str = Field(min_length=1, max_length=40)


def _key(challenge: str) -> str:
    return hashlib.sha256(challenge.encode()).hexdigest()


def _sweep() -> None:
    now = time.time()
    for k in [k for k, c in _challenges.items() if c["exp"] < now]:
        del _challenges[k]


async def me_dict(db: AsyncSession, user: User, tenant: Tenant) -> dict:
    guard_tracker = await db.scalar(select(Tracker.id).where(Tracker.user_id == user.id, Tracker.enabled.is_(True)).limit(1))
    return {"id": user.id, "email": user.email, "display_name": user.display_name or user.email, "role": user.role,
            "company": {"id": tenant.id, "name": tenant.label}, "permissions": sorted(user.permissions),
            "is_guard": guard_tracker is not None, "monitoring_station": bool(user.monitoring_station),
            "sms_level": user.sms_level or "off", "sms_when": user.sms_when or "raised",
            "push_level": apns.level_of(user), "push_when": user.push_when or "raised", "push_ready": apns.configured()}


async def _signed_in(request: Request, db: AsyncSession, user: User, tenant: Tenant, method: str, device: str, kind: str) -> dict:
    ip = client_ip(request)
    if passwords.expired(user):
        await guard.record(db, ip, kind, "denied", email=user.email, user=user, reason="password expired (app)")
        await db.commit()
        raise HTTPException(400, "Your password has expired. Change it on the website (sign in there), then sign in here.")
    row, token = await sessions.start_mobile(request, db, user, device)
    await guard.record(db, ip, kind, "success", email=user.email, user=user, reason=f"signed in to the iPhone app ({method})")
    audit(db, user.tenant_id, "login", user_id=user.id, ip=ip, method=f"app ({method})", device=row.user_agent)
    await db.commit()
    from app.permissions import load_permissions
    user._tenant, user._perms = tenant, await load_permissions(db, user, tenant)
    return {"token": token, "user": await me_dict(db, user, tenant)}


async def _failed(request: Request, db: AsyncSession, user: User | None, kind: str, reason: str, email: str = ""):
    ip = client_ip(request)
    ban = await guard.record(db, ip, kind, "failure", email=email or (user.email if user else ""), reason=f"{reason} (app)", user=user)
    if user is not None:
        audit(db, user.tenant_id, "login.failed", user_id=user.id, ip=ip, step=kind, reason=reason, app=True)
    await db.commit()
    if ban is not None:
        raise HTTPException(403, json.loads(banned_page(request, ban, api=True).body)["detail"])


@router.post("/api/mobile/login")
async def mobile_login(body: LoginIn, request: Request, db: AsyncSession = Depends(get_db)):
    ip, addr = client_ip(request), body.email.strip().lower()
    if await guard.is_account_locked(db, addr):
        verify_password(_DUMMY_HASH, body.password)
        await guard.record(db, ip, "login", "locked", email=addr, reason="account locked: too many failures (app)")
        await db.commit()
        raise HTTPException(401, INVALID)
    user = await db.scalar(select(User).where(func.lower(User.email) == addr, User.deleted_at.is_(None)))
    if user is None:
        verify_password(_DUMMY_HASH, body.password)
        await _failed(request, db, None, "login", "unknown email", email=addr)
        raise HTTPException(401, INVALID)
    if not verify_password(user.password_hash, body.password) or not user.is_active:
        await _failed(request, db, user, "login", "wrong password" if user.is_active else "account deactivated")
        raise HTTPException(401, INVALID)
    tenant = await db.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        await guard.record(db, ip, "login", "denied", email=addr, reason="company sign-in disabled (app)", user=user)
        audit(db, user.tenant_id, "login.failed", user_id=user.id, ip=ip, reason="company disabled", app=True)
        await db.commit()
        raise HTTPException(403, "Your company's portal access is disabled. Contact TWG Security.")
    required = mfa.required_for(tenant, user)
    has_passkeys = mfa.passkeys_enabled() and await mfa.passkey_count(db, user.id) > 0
    if user.totp_enabled or (required and has_passkeys):
        # Second factor next: the authenticator code (/login/totp) and/or a passkey (/passkey/options + verify).
        _sweep()
        challenge = secrets.token_urlsafe(32)
        _challenges[_key(challenge)] = {"uid": user.id, "tv": user.token_version or 0, "exp": time.time() + MFA_TTL_S,
                                        "tries": 0, "device": body.device_name}
        await guard.record(db, ip, "login", "success", email=addr, user=user, reason="password ok, second factor next (app)")
        await db.commit()
        out = {"step": "totp" if user.totp_enabled else "passkey", "challenge": challenge, "expires_in_s": MFA_TTL_S}
        if has_passkeys:
            out["passkey"] = True
        return out
    if required:
        await guard.record(db, ip, "login", "denied", email=addr, user=user,
                           reason="two-step needed but not set up (app)")
        await db.commit()
        return {"step": "enroll", "detail": "Your company requires two-step sign-in. Sign in on the website once to set up "
                                            "an authenticator app, then sign in here with its code."}
    return await _signed_in(request, db, user, tenant, "password", body.device_name, "login")


@router.post("/api/mobile/login/totp")
async def mobile_login_code(body: CodeIn, request: Request, db: AsyncSession = Depends(get_db)):
    _sweep()
    key = _key(body.challenge)
    c = _challenges.get(key)
    if c is None:
        raise HTTPException(401, "That took too long, or the code was already used. Sign in again.")
    user = await db.get(User, c["uid"])
    tenant = await db.get(Tenant, user.tenant_id) if user else None
    if user is None or not user.is_active or user.deleted_at is not None or (user.token_version or 0) != c["tv"] \
            or tenant is None or not tenant.is_active or not user.totp_enabled:
        _challenges.pop(key, None)
        raise HTTPException(401, "Sign in again.")
    code = body.code.strip()
    digits = code.replace(" ", "")
    if digits.isdigit() and len(digits) == 6:
        kind = "totp"
        ok, reason = mfa.check_totp(user, digits)
    else:
        kind = "recovery"
        ok, reason = (await mfa.use_recovery_code(db, user, code)), "wrong recovery code"
        if ok:
            audit(db, user.tenant_id, "mfa.recovery_used", user_id=user.id, ip=client_ip(request),
                  left=await mfa.recovery_codes_left(db, user.id), app=True)
    if ok:
        _challenges.pop(key, None)                      # single use
        return await _signed_in(request, db, user, tenant, kind, c["device"], kind)
    c["tries"] += 1
    if c["tries"] >= MAX_CODE_TRIES:
        _challenges.pop(key, None)
    await _failed(request, db, user, kind, reason)
    if c["tries"] >= MAX_CODE_TRIES:
        raise HTTPException(401, "Too many wrong codes. Sign in again.")
    raise HTTPException(400, "That code was already used. Wait for the next one." if reason == "code already used"
                        else "That code didn't work.")


@router.post("/api/mobile/logout")
async def mobile_logout(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    sid = (request.scope.get("twg_mobile") or {}).get("sid")
    if not sid:
        raise HTTPException(400, "This is for the iPhone app's token")
    row = await db.get(UserSession, sid)
    await sessions.revoke(db, row, "signed out in the app")
    audit(db, user.tenant_id, "logout", user_id=user.id, ip=client_ip(request), app=True, device=row.user_agent)
    await db.commit()
    return {"signed_out": True}


@router.get("/api/mobile/me")
async def mobile_me(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await me_dict(db, user, user._tenant)


@router.get("/api/mobile/dispatches")
async def my_dispatches(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """The calls this person was dispatched to that are still in progress, newest first, with each one's link token
    (kept encrypted for this: alarm_links.mint purpose "dispatch"). Live: the stream sends `dispatch` and `call_ended`
    to this person only (stream.PERSONAL_EVENTS)."""
    from app.models import Alarm, AlarmLink
    from app.routers.links import _guard_status
    from app.security import decrypt
    from app.services import dispatch
    now = sessions.utcnow()
    rows = (await db.execute(select(AlarmLink, Alarm).join(Alarm, Alarm.id == AlarmLink.alarm_id).where(
        AlarmLink.user_id == user.id, AlarmLink.purpose == "dispatch", AlarmLink.revoked_at.is_(None),
        AlarmLink.expires_at > now, Alarm.tenant_id == user.tenant_id, Alarm.call_state == "active")
        .order_by(AlarmLink.id.desc()))).all()
    out, seen = [], set()
    for link, alarm in rows:
        if alarm.id in seen or alarm.site is None or alarm.site.archived_at is not None or not link.token_enc:
            continue
        seen.add(alarm.id)                                   # dispatched twice: the newest link
        where = dispatch.target(alarm)
        out.append({"alarm_id": alarm.id, "link_token": decrypt(link.token_enc), "site_name": alarm.site.name,
                    "address": alarm.site.address or "", "lat": where[0] if where else None,
                    "lng": where[1] if where else None,
                    "dispatched_at": sessions.aware(link.created_at).isoformat(),
                    "status": await _guard_status(db, alarm.id, user.id)})
    return out


# ---------------------------------------------------------------- passkeys (the same ones the website uses)
PASSKEY_TTL_S = 5 * 60
_passkey: dict[str, dict] = {}       # sha256(challenge_id) -> {c (WebAuthn challenge), exp, uid (second factor) or None,
                                     #                         mfa_key (its two-step challenge), device}


class PasskeyOptionsIn(BaseModel):
    device_name: str = Field(default="", max_length=300)
    challenge: str | None = Field(default=None, max_length=200)      # the two-step challenge: second factor


class PasskeyVerifyIn(BaseModel):
    challenge_id: str = Field(min_length=10, max_length=200)
    credential: dict


@router.post("/api/mobile/passkey/options")
async def mobile_passkey_options(body: PasskeyOptionsIn, db: AsyncSession = Depends(get_db)):
    """WebAuthn options for the app's passkey sheet. The WebAuthn challenge stays here under challenge_id (5 min, one
    use). Without "challenge": any passkey for the site (passkey sign-in, no password). With the two-step challenge
    from /login: only that person's passkeys, as their second factor."""
    if not mfa.passkeys_enabled():
        raise HTTPException(400, "Passkeys are turned off.")
    now = time.time()
    for k in [k for k, v in _passkey.items() if v["exp"] < now]:
        del _passkey[k]
    uid = mfa_key = None
    allow = None
    if body.challenge:
        _sweep()
        mfa_key = _key(body.challenge)
        c = _challenges.get(mfa_key)
        if c is None:
            raise HTTPException(401, "That took too long, or the code was already used. Sign in again.")
        uid = c["uid"]
        from app.models import WebAuthnCredential
        allow = (await db.scalars(select(WebAuthnCredential).where(WebAuthnCredential.user_id == uid))).all()
        if not allow:
            raise HTTPException(400, "This account has no passkeys. Use the code from your authenticator app.")
    options, webauthn_challenge = mfa.authentication_options(list(allow) if allow is not None else None)
    challenge_id = secrets.token_urlsafe(32)
    _passkey[_key(challenge_id)] = {"c": webauthn_challenge, "exp": now + PASSKEY_TTL_S, "uid": uid, "mfa_key": mfa_key,
                                    "device": body.device_name or (_challenges.get(mfa_key, {}).get("device") if mfa_key else "")}
    return {"challenge_id": challenge_id, "options": options, "expires_in_s": PASSKEY_TTL_S}


@router.post("/api/mobile/passkey/verify")
async def mobile_passkey_verify(body: PasskeyVerifyIn, request: Request, db: AsyncSession = Depends(get_db)):
    """Check the passkey's answer and sign the app in, exactly as the website's /login/passkey/verify decides
    (bans on failures, disabled accounts and companies refused, audit), then a device token like the password login."""
    state = _passkey.pop(_key(body.challenge_id), None)              # one use, whatever happens next
    if state is None or state["exp"] < time.time():
        raise HTTPException(400, "That took too long. Try again.")
    cred = await mfa.find_credential(db, body.credential)
    user = await db.get(User, cred.user_id) if cred else None
    if cred is None or user is None or (state["uid"] and state["uid"] != user.id):
        await _failed(request, db, None, "passkey", "unknown passkey")
        raise HTTPException(400, "That passkey isn't registered here.")
    try:
        mfa.verify_assertion(body.credential, state["c"], cred)
    except Exception as e:  # noqa: BLE001 - py_webauthn raises several types
        await _failed(request, db, user, "passkey", f"passkey check failed: {type(e).__name__}")
        raise HTTPException(400, "The passkey couldn't be verified.") from None
    tenant = await db.get(Tenant, user.tenant_id)
    if not user.is_active or user.deleted_at is not None or tenant is None or not tenant.is_active:
        await guard.record(db, client_ip(request), "passkey", "denied", email=user.email, user=user,
                           reason="account or company sign-in disabled (app)")
        await db.commit()
        raise HTTPException(403, "This account can't sign in. Contact your administrator.")
    if state["mfa_key"]:                                             # second factor: that sign-in is now used up
        c = _challenges.pop(state["mfa_key"], None)
        if c is None or c["exp"] < time.time() or (user.token_version or 0) != c["tv"]:
            raise HTTPException(401, "That took too long, or the code was already used. Sign in again.")
    return await _signed_in(request, db, user, tenant, "passkey", state["device"] or "", "passkey")


@router.get("/.well-known/apple-app-site-association", include_in_schema=False)
async def apple_app_site_association():
    """Apple's devices (through Apple's CDN) read this to let the iPhone app use the portal's saved passwords and
    passkeys. No sign-in, no redirect, JSON. Empty until APPLE_TEAM_ID is set."""
    import re
    from fastapi.responses import JSONResponse
    from app.config import get_settings
    s = get_settings()
    team = (s.apple_team_id or "").strip().upper()
    apps = [f"{team}.{s.apple_bundle_id}"] if re.fullmatch(r"[A-Z0-9]{10}", team) else []
    return JSONResponse({"webcredentials": {"apps": apps}}, headers={"Cache-Control": "public, max-age=3600"})


# ---------------------------------------------------------------- push notifications (app/apns.py)
class DeviceIn(BaseModel):
    apns_token: str = Field(min_length=32, max_length=200, pattern=r"^[0-9a-fA-F]+$")
    environment: str = Field(pattern="^(sandbox|production)$")
    app_version: str = Field(default="", max_length=40)
    ios_version: str = Field(default="", max_length=40)


def _app_session(request: Request) -> str:
    sid = (request.scope.get("twg_mobile") or {}).get("sid")
    if not sid:
        raise HTTPException(400, "This is for the iPhone app's token")
    return sid


@router.post("/api/mobile/devices")
async def register_device(body: DeviceIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """This iPhone gets the signed-in person's notifications. Upsert by APNs token (a token moving to another sign-in
    or person follows the latest registration). Tied to this app sign-in: ending it drops the device."""
    from app.models import PushDevice
    sid = _app_session(request)
    token, now = body.apns_token.lower(), sessions.utcnow()
    d = await db.scalar(select(PushDevice).where(PushDevice.apns_token == token))
    new = d is None
    if new:
        d = PushDevice(apns_token=token, created_at=now)
        db.add(d)
    d.user_id, d.tenant_id, d.session_id, d.environment = user.id, user.tenant_id, sid, body.environment
    d.app_version, d.ios_version, d.last_seen_at = body.app_version, body.ios_version, now
    if new:
        audit(db, user.tenant_id, "push.device_added", user_id=user.id, ip=client_ip(request), environment=body.environment,
              app_version=body.app_version, ios_version=body.ios_version)
    await db.commit()
    return {"registered": True, "push_ready": apns.configured(), "push_level": apns.level_of(user), "push_when": user.push_when}


@router.delete("/api/mobile/devices/{apns_token}")
async def unregister_device(apns_token: str, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    from sqlalchemy import delete
    from app.models import PushDevice
    r = await db.execute(delete(PushDevice).where(PushDevice.apns_token == apns_token.lower(), PushDevice.user_id == user.id))
    if r.rowcount:
        audit(db, user.tenant_id, "push.device_removed", user_id=user.id, ip=client_ip(request))
    await db.commit()
    return {"removed": bool(r.rowcount)}


class PushIn(BaseModel):
    push_level: str | None = Field(default=None, pattern="^(off|critical|alarm|warning)$")
    push_when: str | None = Field(default=None, pattern="^(raised|confirmed|both)$")


async def save_push(db: AsyncSession, request: Request, by: User, target: User, body: PushIn) -> list[str]:
    changed = []
    if body.push_level is not None and body.push_level != target.push_level:
        target.push_level = body.push_level
        changed.append("push_level")
    if body.push_when is not None and body.push_when != target.push_when:
        target.push_when = body.push_when
        changed.append("push_when")
    if changed:
        audit(db, target.tenant_id, "push.settings", user_id=by.id, ip=client_ip(request), target=target.email,
              push_level=apns.level_of(target), push_when=target.push_when, self_service=by.id == target.id)
    return changed


@router.put("/api/mobile/me/push")
async def my_push(body: PushIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """The app's own notification settings (the same ones as the Account page)."""
    await save_push(db, request, user, user, body)
    await db.commit()
    return await me_dict(db, user, user._tenant)


@router.post("/api/mobile/share")
async def my_share_token(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """"On duty" in the app: my location-share token (POST /api/g/{token}/pos), without a dispatch. The same phone
    tracker as POST /api/a/{token}/share (made if I have none), so dispatch sees me on the map."""
    from app import guard_share
    from app.services import trackers
    from app.services.bus import bus
    _app_session(request)
    t, created = await guard_share.phone_tracker(db, user, user.tenant_id)
    url = guard_share.share_url(t)
    audit(db, user.tenant_id, "tracker.share_from_app", user_id=user.id, ip=client_ip(request), name=t.name, created=created)
    await db.commit()
    bus.publish(t.tenant_id, "tracker.moved", trackers.tracker_dict(t))
    return {"token": url.rstrip("/").rsplit("/", 1)[-1], "name": t.name}
