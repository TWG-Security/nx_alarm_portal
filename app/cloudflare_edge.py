"""Cloudflare edge bans: push each IP ban to Cloudflare so the attacker is dropped before the tunnel.

Ported from MCP-Control-Platform apps/api/src/security/cloudflare-edge.ts. Uses zone IP Access Rules
(mode "block"). Strictly best-effort: Cloudflare being slow, down or misconfigured never blocks, delays
or undoes the local ban. The last success/error is stored on platform_settings for the Platform page.

Only rules whose notes start with NOTE_PREFIX are ever looked up or deleted, so blocks someone made by
hand in Cloudflare are never touched.
"""

import logging
from datetime import datetime, timezone

import httpx

from app import platform_settings
from app.db import sessionmaker

log = logging.getLogger(__name__)

API = "https://api.cloudflare.com/client/v4"
TIMEOUT_S = 8.0
NOTE_PREFIX = "twg-alarm-portal auto-ban"


class EdgeError(Exception):
    pass


def _creds() -> tuple[str, str] | None:
    s = platform_settings.current()
    return (s.cf_token, s.cf_zone_id) if s.cf_token and s.cf_zone_id else None


def can_push() -> bool:
    """New bans go to Cloudflare only while the integration is switched on."""
    return platform_settings.current().cf_enabled and _creds() is not None


def can_remove() -> bool:
    """Cleanup runs whenever credentials exist, even switched off, so no rule we made is left behind."""
    return _creds() is not None


async def _record(ok: bool, error: str = "") -> None:
    from app.models import PlatformSettings
    try:
        async with sessionmaker()() as db:
            row = await db.get(PlatformSettings, 1)
            if row is None:
                return
            now = datetime.now(timezone.utc)
            if ok:
                row.cf_last_ok_at, row.cf_last_error, row.cf_last_error_at = now, "", None
            else:
                row.cf_last_error, row.cf_last_error_at = error[:500], now
            await db.commit()
    except Exception as e:  # noqa: BLE001 - status is cosmetic
        log.warning("cloudflare: could not store status: %s", e)


async def _call(method: str, path: str, body: dict | None = None, creds: tuple[str, str] | None = None) -> dict:
    token, zone = creds or _creds() or ("", "")
    if not token or not zone:
        raise EdgeError("Cloudflare isn't configured (API token and Zone ID)")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as c:
            r = await c.request(method, f"{API}/zones/{zone}{path}", json=body,
                                headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as e:
        raise EdgeError(f"Cloudflare unreachable: {type(e).__name__}") from None
    try:
        data = r.json()
    except ValueError:
        raise EdgeError(f"Cloudflare answered HTTP {r.status_code} with an unreadable body") from None
    if not isinstance(data, dict):
        raise EdgeError(f"Cloudflare answered HTTP {r.status_code} unexpectedly")
    data["_status"] = r.status_code
    return data


def _errors(data: dict) -> str:
    msgs = [f"{e.get('message', '')} ({e.get('code')})" for e in data.get("errors") or [] if isinstance(e, dict)]
    return "; ".join(msgs) or f"HTTP {data.get('_status')}"


async def find_rule(ip: str) -> str | None:
    """Our rule for this IP, if any (matched by our notes prefix only)."""
    data = await _call("GET", f"/firewall/access_rules/rules?configuration.value={ip}&per_page=20")
    if not data.get("success"):
        raise EdgeError(_errors(data))
    for rule in data.get("result") or []:
        if str(rule.get("notes", "")).startswith(NOTE_PREFIX):
            return rule.get("id")
    return None


async def push_ban(ip: str, reason: str) -> str | None:
    """Block the IP at Cloudflare's edge. Returns the rule id, or None if it didn't work (local ban stands)."""
    try:
        data = await _call("POST", "/firewall/access_rules/rules", {
            "mode": "block",
            "configuration": {"target": "ip6" if ":" in ip else "ip", "value": ip},
            "notes": f"{NOTE_PREFIX}: {reason}"[:200],
        })
        if data.get("success"):
            await _record(True)
            return data["result"]["id"]
        # A rule for this IP already exists (say a re-ban raced the sweep): adopt it if it's ours.
        if "duplicate" in _errors(data).lower():
            existing = await find_rule(ip)
            if existing:
                await _record(True)
                return existing
        raise EdgeError(_errors(data))
    except Exception as e:  # noqa: BLE001 - best-effort by design
        log.warning("cloudflare: edge ban for %s failed: %s (the local ban is in force)", ip, e)
        await _record(False, f"Blocking {ip}: {e}")
        return None


async def remove_ban(ip: str, rule_id: str = "") -> bool:
    """Remove our rule for this IP. True when the edge is clean, including "already gone"."""
    if not can_remove():
        return not rule_id            # nothing we can do; clean only if we never pushed one
    try:
        rid = rule_id or await find_rule(ip)
        if not rid:
            await _record(True)
            return True
        data = await _call("DELETE", f"/firewall/access_rules/rules/{rid}")
        err = _errors(data).lower()
        if data.get("success") or data.get("_status") == 404 or "not found" in err or "does not exist" in err:
            await _record(True)
            return True
        raise EdgeError(_errors(data))
    except Exception as e:  # noqa: BLE001
        log.warning("cloudflare: removing the edge ban for %s failed: %s (will retry)", ip, e)
        await _record(False, f"Unblocking {ip}: {e}")
        return False


async def test_connection() -> dict:
    """Read-only check for the Platform page: lists our zone's access rules."""
    if _creds() is None:
        return {"ok": False, "error": "Save the API token and Zone ID first."}
    try:
        data = await _call("GET", "/firewall/access_rules/rules?per_page=50")
        if not data.get("success"):
            raise EdgeError(_errors(data))
        rules = data.get("result") or []
        ours = sum(1 for r in rules if str(r.get("notes", "")).startswith(NOTE_PREFIX))
        total = (data.get("result_info") or {}).get("total_count", len(rules))
        await _record(True)
        return {"ok": True, "rules": total, "ours": ours}
    except Exception as e:  # noqa: BLE001
        await _record(False, f"Test: {e}")
        return {"ok": False, "error": str(e)}
