"""Editing a site's NX rules from the portal (map → site → View NX rules → Edit; administrators only).

What can change: the rule's level tag (#critical / #alarm / #warning / #ignore / none) and #24h in its Title/Comment,
the comment text, on / off, and NX's "Interval of action". Every write is a partial update (PATCH) of just those
fields, then the rule is read back and checked: the event, schedule and anything not edited must be unchanged and
the edited fields must read back as sent; otherwise the previous values are written back and the edit is reported
as failed. Rules whose action carries a login (e.g. an HTTP request to a speaker: "auth") never have their action
rewritten, since the login may not round-trip: their Interval of action is changed in NX itself.
"""
from __future__ import annotations

import copy
import re

import httpx

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


class _Retrying:
    """The NX client with idempotent calls (reads, PATCH, PUT, DELETE) tried twice on the relay's one-off 502/503/504.
    Creating a rule (POST) is never repeated."""
    def __init__(self, client):
        self._c = client

    def __getattr__(self, name):
        fn = getattr(self._c, name)
        if name == "create_rule":
            return fn
        return lambda *a, **kw: _retry(fn, *a, **kw)


async def _retry(fn, *a, **kw):
    import asyncio
    for attempt in range(2):
        try:
            return await fn(*a, **kw)
        except httpx.HTTPStatusError as e:
            if attempt or e.response.status_code not in (502, 503, 504):
                raise
            await asyncio.sleep(0.5)


# ------------------------------------------------------------------ the full editor (event, filters, action, new, delete)
UUID_RE = re.compile(r"^\{?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}?$")
TEXT_MAX = 1000


def redact(rule: dict) -> dict:
    """A rule as the browser may see it: secret values in the action (an HTTP login) removed, `locked` set."""
    r = copy.deepcopy(rule or {})
    act = r.get("action") or {}
    r["action_locked"] = has_secrets(act)
    for k in list(act):
        if any(w in k.lower() for w in SECRET_WORDS):
            act[k] = "(hidden)"
    r["id"] = (r.get("id") or "").strip("{}")
    r.pop("etag", None)
    return r


def _norm(v):
    """For comparing what was sent with what NX reads back: uuids without braces in lower case, id lists in any order."""
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    if isinstance(v, list):
        n = [_norm(x) for x in v]
        return sorted(n) if all(isinstance(x, str) for x in n) else n
    if isinstance(v, str) and UUID_RE.match(v):
        return v.strip("{}").lower()
    if isinstance(v, float):
        return round(v, 4)
    return v


def _within(sent, got) -> bool:
    """Everything sent reads back the same (NX may add defaults of its own)."""
    s, g = _norm(sent), _norm(got)
    if isinstance(s, dict):
        return isinstance(g, dict) and all(k in g and _within(v, g[k]) or (v in (None, "", [], {}) and k not in g)
                                           for k, v in s.items())
    return s == g


def _ids(v, field: str) -> list[str]:
    ids = v if isinstance(v, list) else []
    if len(ids) > 500 or not all(isinstance(i, str) and UUID_RE.match(i) for i in ids):
        raise RuleEditError(f"{field}: not a valid selection")
    return [i.strip("{}").lower() for i in ids]


def _text(v, label: str, maxlen: int = TEXT_MAX) -> str:
    if v is None:
        return ""
    if not isinstance(v, (str, int, float)) or len(str(v)) > maxlen:
        raise RuleEditError(f"{label}: too long or not text")
    return str(v)


def _value(f: dict, v, side: str):
    """One field's value from the browser, checked against its NX type. Raises RuleEditError."""
    t, label = f["type"], f["label"] or f["name"]
    if t in ("device", "user", "server", "users", "servers"):
        v = v if isinstance(v, dict) else {}
        out = {"acceptAll": bool(v.get("acceptAll")), "ids": _ids(v.get("ids") or [], label)}
        if out["acceptAll"]:
            out["ids"] = []
        return out
    if t == "devices":                                              # action targets: listed cameras and/or the event's own
        v = v if isinstance(v, dict) else {}
        return {"ids": _ids(v.get("ids") or [], label), "useSource": bool(v.get("useSource"))}
    if t in ("textLookup", "objectLookup"):
        v = v if isinstance(v, dict) else {}
        check = v.get("checkType") or ("hasAttributes" if t == "objectLookup" else "containsKeywords")
        if f.get("enum") and check not in f["enum"]:
            raise RuleEditError(f"{label}: unknown match type")
        out = {"checkType": check}
        val = _text(v.get("value"), label).strip()
        if val:
            out["value"] = val
        return out
    if t in ("customizableFlag", "flag"):
        return bool(v)
    if t in ("time", "optionalTime", "fps"):
        try:
            n = int(v or 0)
        except (TypeError, ValueError):
            raise RuleEditError(f"{label}: not a number") from None
        if not 0 <= n <= 86400:
            raise RuleEditError(f"{label}: out of range")
        return n
    if t == "volume":
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            raise RuleEditError(f"{label}: not a number") from None
    if t == "analyticsEventLevel":
        parts = [p for p in str(v or "").split("|") if p]
        if any(p not in ("info", "warning", "error") for p in parts):
            raise RuleEditError(f"{label}: unknown level")
        return "|".join(parts)
    if t in ("state", "streamQuality", "httpMethod"):
        if f.get("enum") and v not in f["enum"]:
            raise RuleEditError(f"{label}: pick one of {', '.join(map(str, f['enum']))}")
        return v
    if t in ("analyticsEngine",) and v and not UUID_RE.match(str(v)):
        raise RuleEditError(f"{label}: not a valid plugin")
    return _text(v, label, 400 if t not in ("text", "textWithFields") else TEXT_MAX)


def build_part(side: str, spec: dict | None, before: dict | None, kinds: list[dict]) -> dict:
    """The rule's new `event` or `action`: the chosen type's editable fields from `spec`, checked against NX's own
    catalogue; fields the portal can't edit (and hidden ones) keep their current values."""
    spec = spec or {}
    t = spec.get("type") or (before or {}).get("type")
    kind = next((k for k in kinds if k["id"] == t), None)
    if kind is None:
        raise RuleEditError(f"NX on this site has no {side} type {t!r}")
    if side == "action" and not kind.get("editable"):
        raise RuleEditError(f"The portal doesn't write {kind['name']} actions: keep the current action or pick another")
    same = (before or {}).get("type") == t
    out = copy.deepcopy(before) if same else {"type": t}
    values = spec.get("fields") or {}
    for f in kind["fields"]:
        key = f["key"]
        if not f["editable"] or (not f["visible"] and side == "event"):
            continue                                                    # kept as NX has it (or NX's default)
        if key in values:
            out[key] = _value(f, values[key], side)
        elif not same:
            if f["type"] == "uniqueId":
                import uuid as _uuid
                out[key] = str(_uuid.uuid4())
            elif f["type"] in ("textLookup",):
                out[key] = {"checkType": "containsKeywords"}
            elif f.get("default") not in (None, "") and f["visible"]:
                out[key] = f["default"]
        sel = out.get(key) if isinstance(out.get(key), dict) else {}
        if f["type"] in ("device", "user") and f["visible"] and not f["optional"] and not f.get("accept_all") \
                and not sel.get("acceptAll") and not sel.get("ids"):
            raise RuleEditError(f"{kind['name']}: pick at least one for “{f['label'] or f['name']}”")
        if f["type"] in ("analyticsEventType", "analyticsObjectType") and f["visible"] and not f["optional"] and not out.get(key):
            raise RuleEditError(f"{kind['name']}: pick “{f['label'] or 'Of Type'}”")
    for k in list(out):                                                 # empty text filters read back as absent
        if out[k] is None:
            out.pop(k)
    return out


async def _check(client, rule_id: str, sent: dict, before: dict | None) -> tuple[dict, list[str]]:
    after = await client.get_rule(rule_id)
    problems = [k for k, v in sent.items() if not _within(v, (after or {}).get(k))]
    if before:
        problems += [k for k in ("event", "schedule", "action", "comment", "enabled")
                     if k not in sent and _norm((after or {}).get(k)) != _norm(before.get(k))]
        for k in ("event", "action"):                   # a type change must not leave the old type's fields behind
            if k in sent and isinstance((after or {}).get(k), dict) and (before.get(k) or {}).get("type") != sent[k].get("type"):
                old_only = (set(after[k]) - set(sent[k])) & (set(before.get(k) or {}) - set(sent[k]))
                if old_only:
                    problems.append(f"{k} (left {', '.join(sorted(old_only))})")
    return after or {}, problems


async def save(client, rule_id: str, catalog: dict, *, event: dict | None = None, action: dict | None = None,
               comment: str | None = None, level=UNCHANGED, always: bool | None = None,
               enabled: bool | None = None) -> tuple[dict, dict, dict]:
    """Full edit of an existing rule. Returns (before, changes sent, after). Raises RuleEditError (put back first)."""
    client = _Retrying(client)
    before = await client.get_rule(rule_id)
    if not isinstance(before, dict) or "event" not in before:
        raise RuleEditError("NX didn't return that rule")
    changes: dict = {}
    if event is not None:
        ev = build_part("event", event, before.get("event"), catalog["events"])
        if _norm(ev) != _norm(before.get("event")):
            changes["event"] = ev
    if action is not None:
        old = before.get("action") or {}
        wanted = action.get("type") or old.get("type")
        if has_secrets(old):
            if wanted != old.get("type") or action.get("fields"):
                raise RuleEditError("This rule's action holds a login (e.g. an HTTP request with a password), so the "
                                    "portal won't rewrite it: change the action in Nx Witness")
        else:
            kinds = catalog["actions"]
            same_locked = wanted == old.get("type") and not next((k for k in kinds if k["id"] == wanted), {}).get("editable")
            if not same_locked:
                act = build_part("action", action, old, kinds)
                if _norm(act) != _norm(old):
                    changes["action"] = act
    text = before.get("comment") or ""
    if comment is not None:
        text = comment.strip()
    if level is not UNCHANGED or always is not None:
        text = with_tags(text, level, always)
    if text != (before.get("comment") or ""):
        changes["comment"] = text
    if enabled is not None and enabled != (before.get("enabled") is not False):
        changes["enabled"] = enabled
    if not changes:
        return before, {}, before
    await client.modify_rule(rule_id, changes)
    after, problems = await _check(client, rule_id, changes, before)
    if problems and not has_secrets(before.get("action")) and {"event", "action"} & set(changes):
        # NX may merge a partial update into the old event/action: send the whole rule instead
        full = {k: before.get(k) for k in ("event", "action", "comment", "enabled", "schedule") if k in before}
        full.update(changes)
        await client.replace_rule(rule_id, full)
        after, problems = await _check(client, rule_id, changes, before)
    if problems:
        await _put_back(client, rule_id, before, set(changes) | {p.split(" ")[0] for p in problems})
        raise RuleEditError(f"NX didn't apply the change as expected ({', '.join(problems)}); the rule was put back")
    return before, changes, after


async def _put_back(client, rule_id: str, before: dict, keys: set[str]) -> None:
    restore = {k: before.get(k) for k in keys
               if k in ("comment", "enabled", "schedule", "event") or (k == "action" and not has_secrets(before.get("action")))}
    try:
        await client.modify_rule(rule_id, restore)
        _, problems = await _check(client, rule_id, restore, None)
        if problems and not has_secrets(before.get("action")):
            await client.replace_rule(rule_id, {k: before.get(k) for k in ("event", "action", "comment", "enabled", "schedule") if k in before})
    except Exception as e:  # noqa: BLE001
        raise RuleEditError(f"NX didn't apply the change as expected and putting it back failed ({type(e).__name__}): "
                            "check this rule in Nx Witness") from None


async def create(client, catalog: dict, *, event: dict, action: dict, comment: str = "", level=None,
                 always: bool = False, enabled: bool = True) -> dict:
    """A new rule on the site's NX server, read back and checked (deleted again if NX stored something else)."""
    client = _Retrying(client)
    rule = {"event": build_part("event", event, None, catalog["events"]),
            "action": build_part("action", action, None, catalog["actions"]),
            "comment": with_tags((comment or "").strip(), level, always), "enabled": bool(enabled), "schedule": []}
    made = await client.create_rule(rule)
    rid = ((made or {}).get("id") or "").strip("{}") if isinstance(made, dict) else ""
    if not rid:
        raise RuleEditError("NX didn't say which rule it created: look for it in Nx Witness")
    after, problems = await _check(client, rid, {k: v for k, v in rule.items() if k != "schedule"}, None)
    if problems:
        try:
            await client.delete_rule(rid)
        except Exception:  # noqa: BLE001
            raise RuleEditError(f"NX stored the new rule differently ({', '.join(problems)}) and deleting it failed: "
                                "check it in Nx Witness") from None
        raise RuleEditError(f"NX stored the new rule differently ({', '.join(problems)}), so it was deleted again")
    return after


async def delete(client, rule_id: str) -> dict:
    """Delete a rule; returns it as it was (for the audit log). Checked by reading it back (must be gone)."""
    before = await _retry(client.get_rule, rule_id)
    if not isinstance(before, dict) or "event" not in before:
        raise RuleEditError("NX didn't return that rule")
    try:
        await _retry(client.delete_rule, rule_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:                       # 404: a retried delete after the first one worked
            raise
    try:
        still = await _retry(client.get_rule, rule_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return before                                       # gone, as asked
        raise RuleEditError("NX took the delete but the portal couldn't confirm it: check the rules list") from None
    if isinstance(still, dict) and still.get("event"):
        raise RuleEditError("NX still has the rule after deleting it: check it in Nx Witness")
    return before
