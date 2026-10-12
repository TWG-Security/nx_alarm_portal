"""Two-factor: authenticator-app codes (TOTP), recovery codes, passkeys (WebAuthn), and who must use them.

Ported from MCP-Control-Platform apps/api/src/mfa/{totp,recovery,webauthn}.ts.
- TOTP: RFC 6238, 30 s steps, one step of clock skew either way. A code's step must be newer than the
  last one accepted (last_totp_step), so a code can't be replayed, not even the one used to enrol.
- Recovery codes: 10 x 16 hex characters (xxxx-xxxx-xxxx-xxxx), stored as SHA-256, single use.
- Passkeys: py_webauthn. Sign-in with a passkey is strong on its own (no code after it). The RP ID is a
  host name, so passkeys only work on https://alarmportal.twgsecurity.net, not on the LAN IP.
"""

import base64
import hashlib
import hmac
import io
import json
import secrets
import time

import pyotp
import qrcode
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (generate_authentication_options, generate_registration_options, options_to_json,
                      verify_authentication_response, verify_registration_response)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, AuthenticatorTransport,
                                      PublicKeyCredentialDescriptor, ResidentKeyRequirement,
                                      UserVerificationRequirement)

from app import platform_settings
from app.config import get_settings
from app.models import MfaRecoveryCode, Tenant, User, WebAuthnCredential, utcnow
from app.security import decrypt, encrypt

ISSUER = "TWG Alarm Portal"
STEP_S = 30


# ---------------------------------------------------------------- who must use 2FA
def required_for(tenant: Tenant | None) -> bool:
    """Whether this company's users must have a second factor (Platform page; per company if allowed)."""
    if tenant is None:
        return False
    snap = platform_settings.current()
    if tenant.is_platform:
        return snap.mfa_require_twg
    if snap.mfa_customers == "all":
        return True
    return bool((tenant.settings or {}).get("require_2fa"))


async def passkey_count(db: AsyncSession, user_id: int) -> int:
    return await db.scalar(select(func.count()).select_from(WebAuthnCredential)
                           .where(WebAuthnCredential.user_id == user_id)) or 0


# ---------------------------------------------------------------- TOTP
def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(email: str, secret: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


def qr_data_url(text: str) -> str:
    buf = io.BytesIO()
    qrcode.make(text, box_size=6, border=2).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def totp_step(secret: str, code: str, now: float | None = None) -> int | None:
    """The time step the code matches (current, previous or next), or None."""
    code = "".join(ch for ch in code if ch.isdigit())
    if len(code) != 6:
        return None
    totp = pyotp.TOTP(secret)
    current = int((now if now is not None else time.time()) // STEP_S)
    for step in (current, current - 1, current + 1):
        if hmac.compare_digest(totp.at(step * STEP_S), code):
            return step
    return None


def check_totp(user: User, code: str) -> tuple[bool, str]:
    """(ok, reason). On success the caller stores the new last_totp_step (set here on the object)."""
    if not user.totp_secret_enc:
        return False, "not set up"
    step = totp_step(decrypt(user.totp_secret_enc), code)
    if step is None:
        return False, "wrong code"
    if user.last_totp_step is not None and step <= user.last_totp_step:
        return False, "code already used"
    user.last_totp_step = step
    return True, ""


def start_totp_enrolment(user: User) -> dict:
    """A fresh secret, stored (disabled) until a code confirms it."""
    secret = new_totp_secret()
    user.totp_secret_enc, user.totp_enabled = encrypt(secret), False
    uri = totp_uri(user.email, secret)
    return {"secret": secret, "uri": uri, "qr": qr_data_url(uri)}


def pending_totp(user: User) -> dict | None:
    """The secret being enrolled (to show the QR again after a reload)."""
    if user.totp_enabled or not user.totp_secret_enc:
        return None
    secret = decrypt(user.totp_secret_enc)
    uri = totp_uri(user.email, secret)
    return {"secret": secret, "uri": uri, "qr": qr_data_url(uri)}


async def enable_totp(db: AsyncSession, user: User, code: str) -> list[str] | None:
    """Confirm enrolment with a code; returns fresh recovery codes, or None if the code is wrong."""
    if user.totp_enabled or not user.totp_secret_enc:
        return None
    step = totp_step(decrypt(user.totp_secret_enc), code)
    if step is None:
        return None
    user.totp_enabled, user.last_totp_step = True, step       # the enrolment code can't be replayed at sign-in
    return await new_recovery_codes(db, user)


async def disable_totp(db: AsyncSession, user: User) -> None:
    user.totp_enabled, user.totp_secret_enc, user.last_totp_step = False, "", None
    await db.execute(delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id))


# ---------------------------------------------------------------- recovery codes
def _norm(code: str) -> str:
    return "".join(ch for ch in code.lower() if ch in "0123456789abcdef")


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(_norm(code).encode()).hexdigest()


async def new_recovery_codes(db: AsyncSession, user: User, n: int = 10) -> list[str]:
    await db.execute(delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id))
    codes = []
    for _ in range(n):
        raw = secrets.token_hex(8)
        codes.append("-".join(raw[i:i + 4] for i in range(0, 16, 4)))
        db.add(MfaRecoveryCode(user_id=user.id, code_hash=hash_recovery_code(raw)))
    await db.flush()
    return codes


async def use_recovery_code(db: AsyncSession, user: User, code: str) -> bool:
    if len(_norm(code)) != 16:
        return False
    row = await db.scalar(select(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id,
                                                        MfaRecoveryCode.code_hash == hash_recovery_code(code),
                                                        MfaRecoveryCode.used_at.is_(None)))
    if row is None:
        return False
    row.used_at = utcnow()
    await db.flush()
    return True


async def recovery_codes_left(db: AsyncSession, user_id: int) -> int:
    return await db.scalar(select(func.count()).select_from(MfaRecoveryCode)
                           .where(MfaRecoveryCode.user_id == user_id, MfaRecoveryCode.used_at.is_(None))) or 0


# ---------------------------------------------------------------- passkeys (WebAuthn)
def rp_id() -> str:
    return get_settings().webauthn_rp_id


def origins() -> list[str]:
    return [o.strip() for o in get_settings().webauthn_origins.split(",") if o.strip()]


def passkeys_enabled() -> bool:
    return platform_settings.current().passkeys_enabled


def _descriptor(c: WebAuthnCredential) -> PublicKeyCredentialDescriptor:
    transports = []
    for t in c.transports or []:
        try:
            transports.append(AuthenticatorTransport(t))
        except ValueError:
            pass
    return PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id), transports=transports or None)


def registration_options(user: User, existing: list[WebAuthnCredential]) -> tuple[dict, str]:
    """(options for navigator.credentials.create, the challenge to keep server-side)."""
    opts = generate_registration_options(
        rp_id=rp_id(), rp_name=get_settings().webauthn_rp_name, user_name=user.email,
        user_id=str(user.id).encode(), user_display_name=user.display_name or user.email,
        exclude_credentials=[_descriptor(c) for c in existing],
        authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.PREFERRED,
                                                               user_verification=UserVerificationRequirement.PREFERRED))
    return json.loads(options_to_json(opts)), bytes_to_base64url(opts.challenge)


def verify_registration(credential: dict, challenge: str) -> dict:
    """Raises on failure. Returns the fields to store."""
    v = verify_registration_response(credential=credential, expected_challenge=base64url_to_bytes(challenge),
                                     expected_rp_id=rp_id(), expected_origin=origins())
    transports = (credential.get("response") or {}).get("transports") or []
    return {"credential_id": bytes_to_base64url(v.credential_id), "public_key": bytes_to_base64url(v.credential_public_key),
            "sign_count": v.sign_count, "transports": [t for t in transports if isinstance(t, str)][:10]}


def authentication_options(allow: list[WebAuthnCredential] | None = None) -> tuple[dict, str]:
    """allow=None: any passkey for this site (the browser lists them; the response names the user)."""
    opts = generate_authentication_options(rp_id=rp_id(), allow_credentials=[_descriptor(c) for c in allow or []],
                                           user_verification=UserVerificationRequirement.PREFERRED)
    return json.loads(options_to_json(opts)), bytes_to_base64url(opts.challenge)


async def find_credential(db: AsyncSession, credential: dict) -> WebAuthnCredential | None:
    cid = str(credential.get("rawId") or credential.get("id") or "")
    if not cid or len(cid) > 1400:
        return None
    return await db.scalar(select(WebAuthnCredential).where(WebAuthnCredential.credential_id == cid))


def verify_assertion(credential: dict, challenge: str, cred: WebAuthnCredential) -> None:
    """Raises on failure; updates the credential's counter and last use."""
    v = verify_authentication_response(credential=credential, expected_challenge=base64url_to_bytes(challenge),
                                       expected_rp_id=rp_id(), expected_origin=origins(),
                                       credential_public_key=base64url_to_bytes(cred.public_key),
                                       credential_current_sign_count=cred.sign_count)
    cred.sign_count, cred.last_used_at = v.new_sign_count, utcnow()
    if cred.tested_at is None:
        cred.tested_at = cred.last_used_at
