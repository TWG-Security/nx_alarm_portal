"""Alarm levels: which NX events become portal alarms, and how loudly they are raised.

Levels (priority number in brackets):
  critical (1) - pop-up on every page + siren repeating until acknowledged or silenced
  alarm    (2) - red card in the live feed + chime that repeats until acknowledged
  warning  (3) - amber card in the live feed, one soft tone
  ignore       - not stored

Each tenant can remap any event type (Settings page); a site can override further
via sites.alarm_types = {"<eventType>": "<level>"}.

NX only logs events that some rule fired on, so an event must be covered by at least
one NX rule (any action, e.g. "Write to log") to reach the portal.
"""

from dataclasses import dataclass, field

LEVELS = {"critical": 1, "alarm": 2, "warning": 3}
LEVEL_NAMES = {v: k for k, v in LEVELS.items()}
CHOICES = ("critical", "alarm", "warning", "ignore")

# Event type ids from GET /rest/v4/events/manifest/events (Nx 6.1): (label, category, default level)
EVENT_TYPES: dict[str, tuple[str, str, str]] = {
    "softTrigger": ("Soft trigger (panic / operator button)", "security", "critical"),
    "cameraInput": ("Input signal on camera or I/O module", "security", "critical"),
    "generic": ("Generic event (third-party integrations)", "security", "alarm"),
    "analytics": ("Analytics event (line crossing, intrusion, …)", "security", "alarm"),
    "analyticsObject": ("Analytics object detected", "security", "alarm"),
    "motion": ("Motion on camera", "security", "ignore"),
    "serverFailure": ("Server failure", "system", "alarm"),
    "storageIssue": ("Storage issue (recording may be failing)", "system", "alarm"),
    "deviceDisconnected": ("Camera disconnected", "system", "warning"),
    "networkIssue": ("Network issue", "system", "warning"),
    "deviceIpConflict": ("Camera IP conflict", "system", "warning"),
    "serverConflict": ("Server conflict", "system", "warning"),
    "licenseIssue": ("License issue", "system", "warning"),
    "fanError": ("Fan failure", "system", "warning"),
    "poeOverBudget": ("PoE over budget", "system", "warning"),
    "serverCertificateError": ("Server certificate error", "system", "warning"),
    "saasIssue": ("Services (SaaS) issue", "system", "warning"),
    "ldapSyncIssue": ("LDAP sync issue", "system", "warning"),
    "integrationDiagnostic": ("Plugin / integration diagnostics", "system", "ignore"),
    "serverStarted": ("Server started", "system", "ignore"),
}
UNKNOWN_TYPE_LEVEL = "warning"   # new NX event types show up rather than vanish


@dataclass(frozen=True)
class Policy:
    levels: dict[str, str] = field(default_factory=dict)   # tenant overrides of the defaults
    force_ack_critical: bool = True                         # NX "force acknowledgement" rules -> critical

    @classmethod
    def from_settings(cls, settings: dict | None) -> "Policy":
        s = (settings or {}).get("alarm_policy") or {}
        levels = {k: v for k, v in (s.get("levels") or {}).items() if v in CHOICES}
        return cls(levels=levels, force_ack_critical=bool(s.get("force_ack_critical", True)))

    def to_settings(self) -> dict:
        return {"levels": dict(self.levels), "force_ack_critical": self.force_ack_critical}

    def level_for(self, event_type: str, site_override: dict | None = None) -> str:
        if site_override and site_override.get(event_type) in CHOICES:
            return site_override[event_type]
        if event_type in self.levels:
            return self.levels[event_type]
        return EVENT_TYPES.get(event_type, ("", "", UNKNOWN_TYPE_LEVEL))[2]


DEFAULT_POLICY = Policy()


@dataclass(frozen=True)
class Classification:
    is_alarm: bool
    category: str = ""
    priority: int = 0

    @property
    def level(self) -> str:
        return LEVEL_NAMES.get(self.priority, "")


def category_of(event_type: str) -> str:
    return EVENT_TYPES.get(event_type, ("", "system", ""))[1]


def classify(row: dict, policy: Policy = DEFAULT_POLICY, site_override: dict | None = None) -> Classification:
    event = row.get("eventData") or {}
    action = row.get("actionData") or {}
    event_type = event.get("type", "")

    # The end of a prolonged event is not a new alarm.
    if event.get("state") == "stopped":
        return Classification(False)

    if policy.force_ack_critical and action.get("acknowledge"):
        level = "critical"
    else:
        level = policy.level_for(event_type, site_override)
    if level == "ignore":
        return Classification(False)
    return Classification(True, category_of(event_type), LEVELS[level])
