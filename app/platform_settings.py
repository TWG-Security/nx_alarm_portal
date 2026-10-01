"""TWG-wide settings (the single platform_settings row), with an in-memory snapshot for hot paths.

client_ip() runs on every request and the ban checks on every sign-in, so they read current(): a
snapshot refreshed after every save in this process, and at least every 30 s otherwise (the CLI runs
in its own process). If the database can't be read, the last good snapshot (or the env defaults) stays.
"""

import ipaddress
import logging
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import sessionmaker

log = logging.getLogger(__name__)

MAX_AGE_S = 30.0
Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class BanRules:
    max_fails: int = 5            # failures from one IP within the window trigger a ban
    window_min: int = 10
    first_min: int = 15           # first ban; each later one lasts 4x longer...
    max_min: int = 10080          # ...up to this (7 days)
    permanent_after: int = 5      # ban number N is permanent (0 = never)
    account_lock_max: int = 10    # failures for one email from any IP within the window (0 = off)


@dataclass(frozen=True)
class Snapshot:
    rules: BanRules
    trusted: tuple[Network, ...]                 # may assert CF-Connecting-IP (env + Platform page)
    allow: tuple[tuple[Network, str], ...]       # never banned: (range, label)
    cf_enabled: bool = False
    cf_zone_id: str = ""
    cf_token: str = ""                           # decrypted; "" when unset
    mfa_require_twg: bool = False
    mfa_customers: str = "company"               # company (each company decides) | all
    passkeys_enabled: bool = True
    session_closed_h: int = 12
    session_max_h: int = 0
    idle_timeout_min: int = 0
    password_policy: object = None               # app.passwords.Policy (None = defaults)
    pw_expiry_days: int = 0
    portal_url: str = "https://alarmportal.twgsecurity.net"
    google_ready: bool = False                   # switched on with a client ID and secret


def parse_networks(text: str) -> list[Network]:
    """Comma/space/newline separated IPs or CIDR ranges. Raises ValueError naming a bad entry."""
    out = []
    for part in text.replace("\n", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            raise ValueError(f"'{part}' is not an IP address or range") from None
    return out


def parse_env(text: str) -> list[Network]:
    try:
        return parse_networks(text)
    except ValueError as e:
        log.error("ignoring bad network list in settings: %s", e)
        return []


def _default() -> Snapshot:
    s = get_settings()
    return Snapshot(rules=BanRules(), trusted=tuple(parse_env(s.trusted_proxy_ips)),
                    allow=tuple((n, "from IP_ALLOWLIST") for n in parse_env(s.ip_allowlist)))


_snap: Snapshot | None = None
_loaded_at = 0.0


def current() -> Snapshot:
    global _snap
    if _snap is None:
        _snap = _default()
    return _snap


async def get_row(db: AsyncSession):
    """The settings row, created with defaults if missing."""
    from app.models import PlatformSettings
    row = await db.get(PlatformSettings, 1)
    if row is None:
        row = PlatformSettings(id=1)
        db.add(row)
        await db.flush()
    return row


async def refresh() -> Snapshot:
    """Reload from the database. Never raises: on failure the previous snapshot stays in force."""
    global _snap, _loaded_at
    from app.models import IpAllow, PlatformSettings
    from app.passwords import Policy
    from app.security import decrypt
    try:
        async with sessionmaker()() as db:
            row = await db.get(PlatformSettings, 1)
            allow_rows = (await db.scalars(select(IpAllow))).all()
    except Exception as e:  # noqa: BLE001 - keep serving with what we had
        log.warning("platform settings: reload failed (%s); keeping previous values", e)
        _loaded_at = time.monotonic()
        return current()
    s = get_settings()
    trusted = parse_env(s.trusted_proxy_ips)
    allow = [(n, "from IP_ALLOWLIST") for n in parse_env(s.ip_allowlist)]
    for a in allow_rows:
        try:
            allow.append((ipaddress.ip_network(a.ip, strict=False), a.label))
        except ValueError:
            log.error("allowlist entry %r is not an IP or range; ignored", a.ip)
    if row is None:
        snap = Snapshot(rules=BanRules(), trusted=tuple(trusted), allow=tuple(allow))
    else:
        trusted += parse_env(row.trusted_proxies or "")
        token = ""
        if row.cf_api_token_enc:
            try:
                token = decrypt(row.cf_api_token_enc)
            except Exception:  # noqa: BLE001 - FERNET_KEY changed; show as not configured
                log.error("platform settings: the Cloudflare token can't be decrypted (FERNET_KEY changed?)")
        snap = Snapshot(
            rules=BanRules(max_fails=row.ban_max_fails, window_min=row.ban_window_min, first_min=row.ban_first_min,
                           max_min=row.ban_max_min, permanent_after=row.ban_permanent_after,
                           account_lock_max=row.account_lock_max),
            trusted=tuple(trusted), allow=tuple(allow),
            cf_enabled=row.cf_enabled, cf_zone_id=row.cf_zone_id or "", cf_token=token,
            mfa_require_twg=row.mfa_require_twg, mfa_customers=row.mfa_customers or "company",
            passkeys_enabled=row.passkeys_enabled, session_closed_h=row.session_closed_h,
            session_max_h=row.session_max_h, idle_timeout_min=row.idle_timeout_min,
            password_policy=Policy(min_length=row.pw_min_length, upper=row.pw_upper, lower=row.pw_lower,
                                   number=row.pw_number, symbol=row.pw_symbol),
            pw_expiry_days=row.pw_expiry_days, portal_url=(row.portal_url or "").rstrip("/"),
            google_ready=bool(row.google_enabled and row.google_client_id and row.google_client_secret_enc))
    _snap, _loaded_at = snap, time.monotonic()
    return snap


async def fresh() -> Snapshot:
    """current(), reloaded first if it's older than MAX_AGE_S."""
    if _snap is None or time.monotonic() - _loaded_at > MAX_AGE_S:
        return await refresh()
    return _snap


def reset_cache() -> None:
    """Tests: forget the snapshot (each test has a fresh database)."""
    global _snap, _loaded_at
    _snap, _loaded_at = None, 0.0
