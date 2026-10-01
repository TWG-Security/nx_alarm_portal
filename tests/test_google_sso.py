"""Google sign-in (OIDC): state, PKCE, ID token checks, matching existing accounts only."""
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from sqlalchemy import select

from app import platform_settings
from app.models import AuthEvent, PlatformSettings, Tenant, User
from app.routers.sso import TOKEN_URL
from app.security import hash_password
from tests.conftest import PASSWORD, login

CLIENT_ID = "123-abc.apps.googleusercontent.com"


def jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'RS256', 'kid': 'x'})}.{enc(claims)}.c2ln"


@pytest.fixture
async def twg(session):
    t = Tenant(name="TWG Security", kind="platform")
    session.add(t)
    await session.flush()
    u = User(tenant_id=t.id, email="boss@twgsecurity.com", display_name="Boss", password_hash=hash_password(PASSWORD), role="admin")
    session.add(u)
    await session.commit()
    return t, u


async def google_on(client, admin, session, domains=""):
    await login(client, admin.email)
    r = await client.put("/api/platform/settings/google", json={"enabled": True, "client_id": CLIENT_ID,
                                                                "client_secret": "GOCSPX-secret-value", "domains": domains})
    assert r.status_code == 200, r.text
    assert "GOCSPX" not in r.text and r.json()["google"]["secret_set"]
    row = await session.get(PlatformSettings, 1)
    row.portal_url = "http://portal.test"
    await session.commit()
    await platform_settings.refresh()
    assert "GOCSPX" not in row.google_client_secret_enc
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)


async def start(client):
    r = await client.get("/auth/google")
    assert r.status_code == 303 and r.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    q = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
    assert q["client_id"] == CLIENT_ID and q["redirect_uri"] == "http://portal.test/auth/google/callback"
    assert q["code_challenge_method"] == "S256" and q["scope"] == "openid email profile"
    return q


def token_route(q, **claims):
    """Google's token endpoint: checks PKCE and returns an ID token."""
    def answer(request):
        form = parse_qs(request.content.decode())
        verifier = form["code_verifier"][0]
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if expected != q["code_challenge"] or form["client_secret"][0] != "GOCSPX-secret-value":
            return httpx.Response(400, json={"error": "invalid_grant"})
        base = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "exp": time.time() + 300, "nonce": q["nonce"],
                "email": "Boss@TWGsecurity.com", "email_verified": True}
        return httpx.Response(200, json={"access_token": "at", "id_token": jwt({**base, **claims})})
    return respx.post(TOKEN_URL).mock(side_effect=answer)


@respx.mock
async def test_google_sign_in_matches_the_existing_account(client, session, twg):
    await google_on(client, twg[1], session)
    assert "Sign in with Google" in (await client.get("/login")).text
    q = await start(client)
    token_route(q)
    r = await client.get(f"/auth/google/callback?code=abc&state={q['state']}")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert (await client.get("/api/account")).json()["sessions"][0]["method"] == "sso"
    ev = await session.scalar(select(AuthEvent).where(AuthEvent.kind == "sso", AuthEvent.outcome == "success"))
    assert ev.email == "boss@twgsecurity.com"


@respx.mock
@pytest.mark.parametrize("claims,message", [
    ({"iss": "https://evil.example"}, "didn't check out"),
    ({"aud": "someone-else.apps.googleusercontent.com"}, "didn't check out"),
    ({"exp": time.time() - 5}, "didn't check out"),
    ({"nonce": "replayed"}, "didn't check out"),
    ({"email_verified": False}, "isn't verified"),
    ({"email": "stranger@gmail.com"}, "No portal account uses that Google email"),
])
async def test_google_answers_that_dont_check_out_are_refused(client, session, twg, claims, message):
    await google_on(client, twg[1], session)
    q = await start(client)
    token_route(q, **claims)
    r = await client.get(f"/auth/google/callback?code=abc&state={q['state']}")
    assert r.status_code == 200 and message in r.text.replace("&#39;", "'")
    assert (await client.get("/api/sites")).status_code == 401
    assert not (await session.scalars(select(User).where(User.email == "stranger@gmail.com"))).all()     # never created


@respx.mock
async def test_state_must_match_and_is_single_use(client, session, twg):
    await google_on(client, twg[1], session)
    q = await start(client)
    route = token_route(q)
    assert "took too long" in (await client.get("/auth/google/callback?code=abc&state=forged")).text
    assert "took too long" in (await client.get(f"/auth/google/callback?code=abc&state={q['state']}")).text  # popped
    assert not route.called
    assert "cancelled" in (await client.get("/auth/google/callback?error=access_denied")).text


@respx.mock
async def test_domain_limit_disabled_accounts_and_required_2fa(client, session, twg):
    await google_on(client, twg[1], session, domains="acme.com")
    q = await start(client)
    assert q["hd"] == "acme.com"
    token_route(q)
    assert "domain isn" in (await client.get(f"/auth/google/callback?code=abc&state={q['state']}")).text
    await google_on(client, twg[1], session, domains="")
    row = await session.get(PlatformSettings, 1)
    row.mfa_require_twg = True
    await session.commit()
    await platform_settings.refresh()
    q = await start(client)
    token_route(q)
    r = await client.get(f"/auth/google/callback?code=abc&state={q['state']}")
    assert r.headers["location"] == "/login/enroll"                     # must still set up a second step
    twg[1].is_active = False
    await session.commit()
    q = await start(client)
    token_route(q)
    assert "can't sign in" in (await client.get(f"/auth/google/callback?code=abc&state={q['state']}")).text.replace("&#39;", "'")


async def test_google_settings_validation_and_lan_start(client, session, twg):
    await login(client, twg[1].email)
    assert (await client.put("/api/platform/settings/google", json={"enabled": True, "client_id": CLIENT_ID})).status_code == 400
    assert (await client.put("/api/platform/settings/google", json={"enabled": False, "client_id": "nope"})).status_code == 400
    assert (await client.put("/api/platform/settings/google", json={"enabled": False, "domains": "not a domain!"})).status_code == 400
    client.cookies.clear()
    assert "Sign in with Google" not in (await client.get("/login")).text
    assert "isn't switched on" in (await client.get("/auth/google")).text.replace("&#39;", "'")
    await login(client, twg[1].email)
    await client.put("/api/platform/settings/google", json={"enabled": True, "client_id": CLIENT_ID, "client_secret": "x"})
    client.cookies.clear()
    r = await client.get("/auth/google")             # portal.test isn't the portal address: move there first
    assert r.status_code == 303 and r.headers["location"] == "https://alarmportal.twgsecurity.net/auth/google"
