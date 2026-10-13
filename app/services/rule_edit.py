"""Editing a site's NX rules from the portal (map → site → View NX rules → Edit; administrators only).

What can change: the rule's level tag (#critical / #alarm / #warning / #ignore / none) and #24h in its Title/Comment,
the comment text, on / off, and NX's "Interval of action". Every write is a partial update (PATCH) of just those
fields, then the rule is read back and checked: the event, schedule and anything not edited must be unchanged and
the edited fields must read back as sent; otherwise the previous values are written back and the edit is reported
as failed. Rules whose action carries a login (e.g. an HTTP request to a speaker: "auth") never have their action
rewritten, since the login may not round-trip: their Interval of action is changed in NX itself.
"""
from __future__ import annotations

import re

from app.services import arming
from app.services.alarm_filter import RULE_TAG

LEVEL_TAGS = ("critical", "alarm", "warning", "ignore")
UNCHANGED = object()
SECRET_WORDS = ("auth", "password", "passwd", "secret", "token", "credential", "apikey", "api_key")


class RuleEditError(Exception):
    """Shown to the admin as is."""


def has_secrets(action: dict | None) -> bool:
    return any(any(w in k.lower() for w in SECRET_WORDS) and v not in (None, "", {}, [])
               for k, v in (action or {}).items())


def with_tags(comment: str, level=UNCHANGED, always: bool | None = None) -> str:
    """The comment with its level tag set to `level` (None: no level tag) and #24h set to `always`; the rest of the
    text kept. Tags go at the end, the way the NX rules list reads best."""
    text = comment or ""
    if level is not UNCHANGED:
        text = RULE_TAG.sub("", text)
    if always is not None:
        text = arming.RULE_24H.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    tags = []
    if always:
        tags.append("#24h")
    if level is not UNCHANGED and level:
        tags.append(f"#{level}")
    return " ".join([text, *tags]).strip()


async def edit(client, rule_id: str, *, comment: str | None = None, level=UNCHANGED, always: bool | None = None,
               enabled: bool | None = None, interval_s: int | None = None) -> tuple[dict, dict, dict]:
    """Apply the edit on the NX server. Returns (rule before, changes sent, rule after). Raises RuleEditError."""
    before = await client.get_rule(rule_id)
    if not isinstance(before, dict) or "event" not in before:
        raise RuleEditError("NX didn't return that rule")
    changes: dict = {}
    text = before.get("comment") or ""
    if comment is not None:
        text = comment.strip()
    if level is not UNCHANGED or always is not None:
        text = with_tags(text, level, always)
    if text != (before.get("comment") or ""):
        changes["comment"] = text
    if enabled is not None and enabled != (before.get("enabled") is not False):
        changes["enabled"] = enabled
    action = before.get("action") or {}
    if interval_s is not None and interval_s != int(action.get("intervalS") or 0):
        if has_secrets(action):
            raise RuleEditError("This rule's action holds a login (e.g. an HTTP request with a password), so the portal "
                                "won't rewrite it: change its Interval of action in Nx Witness")
        changes["action"] = {**action, "intervalS": interval_s}
    if not changes:
        return before, {}, before
    await client.modify_rule(rule_id, changes)
    after = await client.get_rule(rule_id)
    problems = [k for k, v in changes.items() if (after or {}).get(k) != v]
    problems += [k for k in ("event", "schedule", "action", "comment", "enabled")
                 if k not in changes and (after or {}).get(k) != before.get(k)]
    if problems:
        restore = {k: before.get(k) for k in set(changes) | set(problems)
                   if k in ("comment", "enabled", "schedule", "event") or (k == "action" and not has_secrets(before.get("action")))}
        try:
            await client.modify_rule(rule_id, restore)
        except Exception as e:  # noqa: BLE001
            raise RuleEditError(f"NX didn't apply the change as expected ({', '.join(problems)}) and putting it back "
                                f"failed ({type(e).__name__}): check this rule in Nx Witness") from None
        raise RuleEditError(f"NX didn't apply the change as expected ({', '.join(problems)}); the rule was put back")
    return before, changes, after
