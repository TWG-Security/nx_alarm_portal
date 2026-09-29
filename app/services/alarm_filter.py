"""Decides which NX event-log rows become portal alarms, and how urgent they are.

NX only writes an event to its log when some rule fired for it, so an event must be
covered by at least one NX rule (any action, e.g. "Write to log") to reach the portal.
"""

from dataclasses import dataclass

# Event type ids from GET /rest/v4/events/manifest/events (Nx 6.1).
SECURITY_TYPES = {"generic", "softTrigger", "cameraInput", "analytics", "analyticsObject", "motion"}
SYSTEM_TYPES = {
    "deviceDisconnected", "deviceIpConflict", "networkIssue", "serverFailure", "serverConflict",
    "storageIssue", "licenseIssue", "fanError", "poeOverBudget", "serverCertificateError",
    "saasIssue", "ldapSyncIssue",
}

# Default: everything above except camera motion (too noisy across dozens of sites)
# and plugin diagnostics / server-started notices.
DEFAULT_INCLUDE = (SECURITY_TYPES | SYSTEM_TYPES) - {"motion"}


@dataclass(frozen=True)
class Classification:
    is_alarm: bool
    category: str = ""
    priority: int = 0


def classify(row: dict, site_override: dict | None = None) -> Classification:
    """site_override: {"include": [...], "exclude": [...]} adjusts DEFAULT_INCLUDE for one site."""
    event = row.get("eventData") or {}
    action = row.get("actionData") or {}
    event_type = event.get("type", "")

    # The end of a prolonged event is not a new alarm.
    if event.get("state") == "stopped":
        return Classification(False)

    include = set(DEFAULT_INCLUDE)
    if site_override:
        include |= set(site_override.get("include") or [])
        include -= set(site_override.get("exclude") or [])

    ack_required = bool(action.get("acknowledge"))
    if not ack_required and event_type not in include:
        return Classification(False)

    category = "system" if event_type in SYSTEM_TYPES else "security"
    if ack_required:
        priority = 1
    elif category == "security":
        priority = 2
    else:
        priority = 3
    return Classification(True, category, priority)
