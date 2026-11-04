"""Muting a site or one camera for a few minutes (the SOC, 2026-10-25: clients who spend time outside trigger
50-500 alarm events in one session; "when clicking acknowledge on an event, mute this site for 2, 5, 30 minutes").

The user's choice (2026-10-26): **silent, still listed**. A muted site's (or camera's) new security alarms are stored
and shown in the feed as usual, open and acknowledgeable, but marked `alarms.muted`: no siren, no pop-up, no texts,
no phone push. Critical alarms (#critical, forced acknowledgement, panic) and system events (site connection lost,
storage...) are never muted. Nothing is ever hidden or dropped. The mute ends by itself.

sites.mute = {"site": {...} | absent, "cameras": {device_id: {...}}}, each entry
{"until_ms", "at_ms", "by", "user_id", "name"}. Like arming, it is read at ingest time, so it never depends on a
background job; expired entries are simply ignored (and dropped on the next change).
"""

MINUTES_MAX = 24 * 60         # the user, 2026-11-04: presets up to 24 h ("Presets only, longer")


def active(mute: dict | None, now_ms: int) -> dict:
    """The entries still running: {"site": {...}?, "cameras": {...}}."""
    mute = mute or {}
    out: dict = {}
    site = mute.get("site")
    if site and int(site.get("until_ms") or 0) > now_ms:
        out["site"] = site
    cams = {d: e for d, e in (mute.get("cameras") or {}).items() if int(e.get("until_ms") or 0) > now_ms}
    if cams:
        out["cameras"] = cams
    return out


def is_muted(mute: dict | None, device_id: str, now_ms: int) -> bool:
    a = active(mute, now_ms)
    return "site" in a or bool(device_id and device_id in a.get("cameras", {}))


def applies(priority: int, category: str) -> bool:
    """Only non-critical security alarms can be muted."""
    return priority > 1 and category == "security"


def set_mute(mute: dict | None, device_id: str | None, entry: dict, now_ms: int) -> dict:
    out = active(mute, now_ms)
    if device_id:
        out.setdefault("cameras", {})[device_id] = entry
    else:
        out["site"] = entry
    return out


def clear(mute: dict | None, device_id: str | None, now_ms: int) -> dict | None:
    out = active(mute, now_ms)
    if device_id:
        out.get("cameras", {}).pop(device_id, None)
        if not out.get("cameras"):
            out.pop("cameras", None)
    else:
        out.pop("site", None)
    return out or None
