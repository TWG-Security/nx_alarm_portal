"""Pure helpers that pull portal fields out of an NX event-log row."""

import hashlib

from app.services.alarm_filter import EVENT_TYPES

ZERO_UUID = "00000000-0000-0000-0000-000000000000"

# Short captions for events that don't carry their own.
SHORT_LABELS = {
    "portalSiteOffline": "Site connection lost",
    "softTrigger": "Soft trigger", "cameraInput": "Input signal", "generic": "Generic event",
    "analytics": "Analytics event", "analyticsObject": "Object detected", "motion": "Motion",
    "deviceDisconnected": "Camera disconnected", "serverFailure": "Server failure",
    "storageIssue": "Storage issue", "networkIssue": "Network issue", "deviceIpConflict": "Camera IP conflict",
    "serverConflict": "Server conflict", "licenseIssue": "License issue", "fanError": "Fan failure",
    "poeOverBudget": "PoE over budget", "serverCertificateError": "Server certificate error",
    "saasIssue": "Services issue", "ldapSyncIssue": "LDAP sync issue",
    "integrationDiagnostic": "Plugin diagnostic", "serverStarted": "Server started",
}


def _label(event_type: str) -> str:
    return SHORT_LABELS.get(event_type) or EVENT_TYPES.get(event_type, (event_type or "Event",))[0]


def _strip_braces(value: str) -> str:
    return (value or "").strip("{}")


def event_key(row: dict) -> str:
    """Stable id for the underlying NX event, shared by all of its action rows."""
    ev = row.get("eventData") or {}
    parts = [
        ev.get("type", ""),
        str(ev.get("timestamp") or row.get("timestampMs") or ""),
        ev.get("deviceId") or ev.get("serverId") or ev.get("source") or "",
        ev.get("eventTypeId") or ev.get("objectTypeId") or ev.get("triggerId") or ev.get("caption") or "",
        ev.get("state", ""),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:40]


def device_id(row: dict) -> str:
    ev = row.get("eventData") or {}
    act = row.get("actionData") or {}
    dev = ev.get("deviceId") or ""
    if not dev:
        ids = act.get("deviceIds") or ev.get("deviceIds") or []
        dev = ids[0] if ids else ""
    dev = _strip_braces(dev)
    return "" if dev == ZERO_UUID else dev


def action_server_id(row: dict) -> str:
    act = row.get("actionData") or {}
    for key in ("serverId", "originPeerId"):
        value = _strip_braces(act.get(key) or "")
        if value and value != ZERO_UUID:
            return value
    return ""


def summarize(row: dict, device_names: dict[str, str], user_names: dict[str, str] | None = None) -> dict:
    ev = row.get("eventData") or {}
    act = row.get("actionData") or {}
    dev = device_id(row)
    event_type = ev.get("type", "")
    description = ev.get("description") or ""
    if event_type == "softTrigger":
        caption = ev.get("triggerName") or "Soft trigger"
        user = (user_names or {}).get(_strip_braces(ev.get("userId") or ""), "")
        description = description or (f"Pressed by {user}" if user else "")
    elif event_type == "cameraInput":
        port = ev.get("inputPortId") or ""
        caption = ev.get("caption") or ("Input signal" + (f" on port {port}" if port else ""))
    else:
        caption = ev.get("caption") or act.get("caption") or _label(event_type)
    source = act.get("sourceName") or device_names.get(dev, "") or ev.get("source", "")
    return {
        "event_type": ev.get("type", "unknown"),
        "event_subtype": ev.get("eventTypeId") or ev.get("objectTypeId") or "",
        "caption": caption[:500],
        "description": description,
        "source_name": source[:300],
        "device_id": dev,
        "event_ts_ms": int(row.get("timestampMs") or 0),
        "rule_id": _strip_braces(row.get("ruleId") or ""),
        "nx_action_id": _strip_braces(act.get("id") or ""),
        "nx_action_server_id": action_server_id(row),
        "nx_ack_required": bool(act.get("acknowledge")),
    }
