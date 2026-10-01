"""Google sign-in (OpenID Connect, authorization-code flow with PKCE). Ported from MCP-Control-Platform
apps/api/src/routes/sso.ts.

- Only EXISTING accounts are matched, by their Google-verified email; nobody is ever created here.
- The state, PKCE verifier and nonce live in the signed session for 10 minutes. The callback checks the
  state, swaps the code at Google's token endpoint (directly, over TLS, with our client secret), and checks
  the ID token: issuer is Google, audience is our client ID, not expired, nonce matches, email verified.
  (OIDC Core 3.1.3.7: with the token straight from the token endpoint, TLS stands in for the signature.)
- Google's sign-in replaces the local authenticator-app step, but someone who must have two-step sign-in
  and has none set up is still walked through it. Password expiry still applies.
- The redirect URI is <portal address>/auth/google/callback; a start on another host (the LAN IP) first
  moves to the portal address, because the session cookie belongs to one host.
"""

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import mfa, platform_settings
from app import security_guard as guard
from app.db import get_db
from app.deps import client_ip
from app.models import PlatformSettings, Tenant, User
from app.routers.auth import ENROLL_TTL_S, _finish, _login_page, _set_pending, banned_page
from app.security import decrypt

router = APIRouter()

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
STATE_TTL_S = 10 * 60
ERRORS = {
    "off": "Google sign-in isn't switched on.",
    "state": "That sign-in took too long or came from another browser. Try again.",
    "denied": "Google sign-in was cancelled.",
    "exchange": "Google couldn't confirm the sign-in. Try again.",
    "token": "Google's answer didn't check out. Try again.",
    "unverified": "That Google account's email isn't verified.",
    "domain": "That Google account's domain isn't allowed here.",
    "no_account": "No portal account uses that Google email. Ask your administrator to add you.",
    "disabled": "This account can't sign in. Contact your administrator.",
}


def callback_url() -> str:
    return f"{platform_settings.current().portal_url}/auth/google/callback"


async def _config(db: AsyncSession) -> tuple[str, str, list[str]] | None:
    row = await db.get(PlatformSettings, 1)
    if row is None or not (row.google_enabled and row.google_client_id and row.google_client_secret_enc):
        return None
    try:
        secret = decrypt(row.google_client_secret_enc)
    except Exception:  # noqa: BLE001
        return None
    domains = [d.strip().lower().lstrip("@") for d in (row.google_domains or "").replace(" ", ",").split(",") if d.strip()]
    return row.google_client_id, secret, domains


def _error(request: Request, code: str):
    request.session.pop("sso", None)
    return _login_page(request, error=ERRORS.get(code, ERRORS["exchange"]))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def id_token_claims(token: str) -> dict:
    """The ID token's claims (payload). Raises ValueError if it isn't a JWT."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("not a JWT")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


@router.get("/auth/google")
async def google_start(request: Request, db: AsyncSession = Depends(get_db)):
    cfg = await _config(db)
    if cfg is None:
        return _error(request, "off")
    portal = urlparse(platform_settings.current().portal_url)
    if request.url.hostname != portal.hostname:          # e.g. the LAN IP: continue on the portal's own address
        return RedirectResponse(f"{platform_settings.current().portal_url}/auth/google", 303)
    verifier = secrets.token_urlsafe(48)
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    request.session["sso"] = {"state": state, "verifier": verifier, "nonce": nonce, "exp": time.time() + STATE_TTL_S}
    params = {"client_id": cfg[0], "redirect_uri": callback_url(), "response_type": "code", "scope": "openid email profile",
              "state": state, "nonce": nonce, "prompt": "select_account",
              "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}
    if len(cfg[2]) == 1:
        params["hd"] = cfg[2][0]                           # Google pre-selects accounts of that domain
    return RedirectResponse(f"{AUTHORIZE_URL}?{urlencode(params)}", 303)


@router.get("/auth/google/callback")
async def google_callback(request: Request, code: str = "", state: str = "", error: str = "",
                          db: AsyncSession = Depends(get_db)):
    ip = client_ip(request)
    sso = request.session.pop("sso", None)
    if error:
        return _error(request, "denied")
    if not sso or sso.get("exp", 0) < time.time() or not state or not secrets.compare_digest(state, sso.get("state", "")):
        return _error(request, "state")
    cfg = await _config(db)
    if cfg is None:
        return _error(request, "off")
    client_id, client_secret, domains = cfg
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                              "client_secret": client_secret, "redirect_uri": callback_url(),
                                              "code_verifier": sso["verifier"]})
        if r.status_code != 200:
            raise ValueError(f"token endpoint answered HTTP {r.status_code}")
        claims = id_token_claims(r.json()["id_token"])
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("google sign-in: code exchange failed: %s", e)
        return _error(request, "exchange")
    aud = claims.get("aud")
    if (claims.get("iss") not in ISSUERS or (aud != client_id and not (isinstance(aud, list) and client_id in aud))
            or float(claims.get("exp", 0)) < time.time() or claims.get("nonce") != sso.get("nonce")):
        await guard.record(db, ip, "sso", "failure", email=str(claims.get("email", ""))[:320], reason="ID token didn't check out")
        await db.commit()
        return _error(request, "token")
    email = str(claims.get("email", "")).strip().lower()
    if not email or claims.get("email_verified") is not True:
        await guard.record(db, ip, "sso", "failure", email=email, reason="Google email not verified")
        await db.commit()
        return _error(request, "unverified")
    if domains and email.split("@")[-1] not in domains:
        await guard.record(db, ip, "sso", "failure", email=email, reason="domain not allowed")
        await db.commit()
        return _error(request, "domain")
    user = await db.scalar(select(User).where(func.lower(User.email) == email))
    if user is None:
        ban = await guard.record(db, ip, "sso", "failure", email=email, reason="no portal account for this Google email")
        await db.commit()
        return banned_page(request, ban) if ban else _error(request, "no_account")
    tenant = await db.get(Tenant, user.tenant_id)
    if not user.is_active or tenant is None or not tenant.is_active:
        await guard.record(db, ip, "sso", "denied", email=email, user=user, reason="account or company sign-in disabled")
        await db.commit()
        return _error(request, "disabled")
    has_second = user.totp_enabled or (mfa.passkeys_enabled() and await mfa.passkey_count(db, user.id) > 0)
    if mfa.required_for(tenant) and not has_second:
        _set_pending(request, user, "enroll", ENROLL_TTL_S)
        await guard.record(db, ip, "sso", "success", email=email, user=user, reason="Google ok, must set up 2FA")
        await db.commit()
        return RedirectResponse("/login/enroll", 303)
    return RedirectResponse(await _finish(request, db, user, "sso", kind="sso"), 303)
