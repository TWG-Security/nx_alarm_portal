"""What a site's NX rule engine supports, for the portal's rule editor (map → site → NX rules → Edit / New rule).

Everything the editor's drop-downs offer comes from the site itself, read on demand and cached for CACHE_S:
- the event and action catalogues (NX's own manifests: /rest/v4/events/manifest/events|actions, verified on NX 6.1),
- cameras (with their analytics plugins and I/O ports), servers, users and user groups,
- per camera, the analytics event types, object types and **zones** it can raise, merged from every source that
  answers: the plugin's device-agent manifest and settings (zones drawn in the plugin: polygons, lines, boxes),
  NX's analytics taxonomy, the existing rules, NX's event log (last LOG_DAYS days) and the portal's own alarms.
  AXIS Object Analytics, for one, raises one event type per scenario ("Front Lot" on 2026-10-14, eventTypeId
  `nx.axis.{…}`, description "Front Lot started"), so a zone is found from its events even when no plugin endpoint
  describes it.
Endpoints that a given NX version doesn't have just add a line to `sources` ("not available"); nothing here is
required except the manifests. Discovery uses its own NX connection (never the alarm pipeline's), runs without a DB
session held, and is capped at BUILD_MAX_S.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import httpx
from sqlalchemy import func, select

from app.db import sessionmaker
from app.models import Alarm, Site
from app.security import decrypt
from app.services.sites import make_client

log = logging.getLogger("portal.rules")

CACHE_S = 300
BUILD_MAX_S = 40
CALL_MAX_S = 10
PARALLEL = 4
LOG_DAYS = 7
LOG_LIMIT = 2000
HISTORY_DAYS = 60

# Event field types the editor knows how to set. Others are shown and kept as they are.
EVENT_FIELD_TYPES = {"state", "device", "user", "server", "analyticsEventType", "analyticsObjectType", "textLookup",
                     "objectLookup", "analyticsAttributes", "customizableText", "customizableIcon", "customizableFlag",
                     "uniqueId", "inputPort", "analyticsEngine", "analyticsEventLevel"}
ACTION_FIELD_TYPES = {"users", "flag", "time", "optionalTime", "text", "textWithFields", "devices", "volume",
                      "streamQuality", "fps", "outputPort", "servers", "sound"}
# Actions the portal writes. Left out on purpose: http / siteHttp (logins, arbitrary URLs), layouts and PTZ presets
# (need lists the portal doesn't read), acknowledge (system). Rules with those keep their action untouched.
EDITABLE_ACTIONS = ("writeToLog", "desktopNotification", "pushNotification", "sendEmail", "bookmark",
                    "showOnAlarmLayout", "textOverlay", "speak", "deviceRecording", "deviceOutput", "panicRecording",
                    "buzzer", "playSound", "repeatSound")
# NX's predefined user groups (ids are the same on every NX 6 system).
PREDEFINED_GROUPS = {"00000000-0000-0000-0000-100000000000": "Administrators",
                     "00000000-0000-0000-0000-100000000001": "Power Users",
                     "00000000-0000-0000-0000-100000000002": "Advanced Viewers",
                     "00000000-0000-0000-0000-100000000003": "Viewers",
                     "00000000-0000-0000-0000-100000000004": "Live Viewers",
                     "00000000-0000-0000-0000-100000000005": "System Health Viewers"}
STATE_LABELS = {"started": "When it starts", "stopped": "When it stops", "instant": "Each time (instant)",
                "none": "Whole duration"}
ZONE_FIGURES = {"PolygonFigure": "area", "BoxFigure": "box", "LineFigure": "line"}
_ZONE_FROM_TEXT = re.compile(r"^(.*?)\s+(started|stopped|active|inactive|begin|end|on|off)\s*$", re.I)

_cache: dict[int, dict] = {}
_locks: dict[int, asyncio.Lock] = {}


def uid(v) -> str:
    return str(v or "").strip("{}").lower()


def rule_key(f: dict, side: str) -> str:
    """The key a manifest field has in a rule's `event` / `action` (verified against NX 6.1 rules: deviceId →
    devices, userId → users, interval → intervalS, recordBefore → recordBeforeS …)."""
    t, n = f.get("type", ""), f.get("fieldName", "")
    if t in ("device", "devices"):
        return "devices"
    if t in ("user", "users"):
        return "users"
    if t in ("server", "servers"):
        return "servers"
    if t in ("time", "optionalTime"):
        return n + "S"
    return n


def _field(f: dict, side: str) -> dict:
    p = f.get("properties") or {}
    s = p.get("openApiSchema") or {}
    known = EVENT_FIELD_TYPES if side == "event" else ACTION_FIELD_TYPES
    out = {"key": rule_key(f, side), "name": f.get("fieldName", ""), "type": f.get("type", ""),
           "label": f.get("displayName") or "", "help": re.sub(r"<[^>]+>", "", f.get("description") or "")[:300],
           "visible": p.get("visible") is not False, "optional": bool(p.get("optional")),
           "accept_all": bool(p.get("acceptAll")) or bool(p.get("allowEmptySelection")),
           "default": s.get("default"), "editable": f.get("type") in known}
    enum = s.get("enum") or ((s.get("properties") or {}).get("checkType") or {}).get("enum")
    if enum:
        out["enum"] = enum
    if "value" in p:
        out["fixed"] = p["value"]
    return out


def shape_manifest(m: dict, side: str) -> list[dict]:
    out = []
    for tid, t in (m or {}).items():
        if not isinstance(t, dict):
            continue
        flags = t.get("flags", "")
        item = {"id": t.get("id") or tid, "name": t.get("displayName") or tid,
                "description": re.sub(r"<[^>]+>", "", t.get("description") or ""), "group": t.get("groupId") or "",
                "flags": flags, "fields": [_field(f, side) for f in t.get("fields") or []]}
        if side == "action":
            item["editable"] = item["id"] in EDITABLE_ACTIONS and "system" not in flags
        out.append(item)
    return sorted(out, key=lambda x: (x["group"] != "", x["name"].lower()))


# ------------------------------------------------------------------ analytics (event types, object types, zones)
def _pretty_type(type_id: str) -> str:
    """'nx.onvif.VideoAnalytics.ObjectClass.Human' → 'Object Class · Human'; 'cvedia.rt.armed_person' → 'Armed person'."""
    t = re.sub(r"\{[0-9a-fA-F-]+\}", "", type_id or "").strip(".")
    parts = [p for p in t.split(".") if p and p.lower() not in ("nx", "onvif", "rt", "base", "axis", "cvedia", "sys")]
    tail = parts[-2:] if len(parts) > 1 else parts
    words = [re.sub(r"(?<=[a-z])(?=[A-Z])", " ", p).replace("_", " ").strip() for p in tail]
    s = " · ".join(w for w in words if w)
    return (s[:1].upper() + s[1:]) if s else (type_id or "")


class _Analytics:
    """Per camera: event types, object types and zones, each remembering where it was found."""
    def __init__(self) -> None:
        self.dev: dict[str, dict] = {}

    def _d(self, dev: str) -> dict:
        return self.dev.setdefault(uid(dev), {"event_types": {}, "object_types": {}, "zones": []})

    def event_type(self, dev, tid, name="", engine="", zone="", source="", seen=0, last_ms=0, key=""):
        if not tid:
            return
        d = self._d(dev)["event_types"]
        e = d.setdefault(tid, {"id": tid, "name": "", "engine": "", "zones": [], "sources": [], "seen": 0, "last_ms": 0, "keys": []})
        if name and (not e["name"] or source in ("plugin", "taxonomy")):
            e["name"] = name
        e["engine"] = e["engine"] or engine
        if zone and zone not in e["zones"]:
            e["zones"].append(zone)
        if key and key not in e["keys"] and len(e["keys"]) < 5:
            e["keys"].append(key)
        if source and source not in e["sources"]:
            e["sources"].append(source)
        if source == "NX event log":                     # one row per rule that fired: counted per source, the
            e["_log"] = e.get("_log", 0) + seen           # larger count wins (the portal's alarms and NX's log overlap)
        elif seen:
            e["_portal"] = e.get("_portal", 0) + seen
        e["seen"] = max(e.get("_log", 0), e.get("_portal", 0))
        e["last_ms"] = max(e["last_ms"], last_ms or 0)

    def object_type(self, dev, tid, name="", source=""):
        if not tid:
            return
        d = self._d(dev)["object_types"]
        o = d.setdefault(tid, {"id": tid, "name": "", "sources": []})
        if name and not o["name"]:
            o["name"] = name
        if source and source not in o["sources"]:
            o["sources"].append(source)

    def zone(self, dev, name, kind="area", engine="", points=None, source=""):
        zones = self._d(dev)["zones"]
        if not name:
            return
        z = next((z for z in zones if z["name"].lower() == name.lower() and (z["engine"] == engine or not z["engine"] or not engine)), None)
        if z is None:
            zones.append({"name": name, "kind": kind, "engine": engine, "points": points or [], "sources": [source]})
        else:
            z["points"] = z["points"] or points or []
            z["engine"] = z["engine"] or engine
            if source not in z["sources"]:
                z["sources"].append(source)

    def out(self, names: dict[str, str]) -> dict:
        res = {}
        for dev, d in self.dev.items():
            ets = sorted(d["event_types"].values(), key=lambda e: (-e["seen"], (e["name"] or e["id"]).lower()))
            for e in ets:
                e.pop("_log", None), e.pop("_portal", None)
                if not e["name"]:
                    e["name"] = (e["zones"][0] if e["zones"] else "") or _pretty_type(e["id"])
                e["label"] = e["name"] if not e["zones"] or e["zones"][0] in e["name"] else f'{e["name"]} · {", ".join(e["zones"])}'
            ots = sorted(d["object_types"].values(), key=lambda o: (o["name"] or o["id"]).lower())
            for o in ots:
                o["name"] = o["name"] or _pretty_type(o["id"])
            res[dev] = {"name": names.get(dev, ""), "event_types": ets, "object_types": ots, "zones": d["zones"]}
        return res


def zone_from_text(text: str) -> str:
    """'Front Lot started' → 'Front Lot' (AXIS Object Analytics scenario events)."""
    m = _ZONE_FROM_TEXT.match((text or "").strip())
    return m.group(1).strip() if m else ""


def _manifest_types(m: dict) -> tuple[list[dict], list[dict]]:
    """Event and object types (id + name) from a plugin / device-agent manifest, wherever NX puts them."""
    if not isinstance(m, dict):
        return [], []
    m = m.get("manifest") if isinstance(m.get("manifest"), dict) else m
    lib = m.get("typeLibrary") or {}
    ev = list(m.get("eventTypes") or []) + list(lib.get("eventTypes") or [])
    ob = list(m.get("objectTypes") or []) + list(lib.get("objectTypes") or [])
    for s in m.get("supportedTypes") or []:
        if s.get("eventTypeId"):
            ev.append({"id": s["eventTypeId"]})
        if s.get("objectTypeId"):
            ob.append({"id": s["objectTypeId"]})
    return ([{"id": e.get("id"), "name": e.get("name") or ""} for e in ev if isinstance(e, dict) and e.get("id")],
            [{"id": o.get("id"), "name": o.get("name") or ""} for o in ob if isinstance(o, dict) and o.get("id")])


def zones_from_settings(settings: dict) -> list[dict]:
    """Zones drawn in a plugin's camera settings: every Polygon / Box / Line figure in the settings model, with its
    label (the drawn figure's own label, else the field's caption) and its points (0..1 of the frame)."""
    if not isinstance(settings, dict):
        return []
    values = settings.get("values") or {}
    out = []

    def walk(node):
        if isinstance(node, dict):
            t = node.get("type")
            if t in ZONE_FIGURES and node.get("name"):
                v = values.get(node["name"])
                if isinstance(v, str):
                    try:
                        v = json.loads(v)
                    except ValueError:
                        v = {}
                v = v if isinstance(v, dict) else {}
                fig = v.get("figure") if isinstance(v.get("figure"), dict) else {}
                pts = fig.get("points") or v.get("points") or []
                if pts or v.get("label"):                       # an empty slot isn't a zone
                    out.append({"name": (v.get("label") or node.get("caption") or node["name"]).strip(),
                                "kind": ZONE_FIGURES[t], "points": [[round(float(x), 4), round(float(y), 4)]
                                                                    for x, y in pts if isinstance(x, (int, float))][:64],
                                "field": node["name"]})
            for k in ("items", "sections", "model"):
                walk(node.get(k))
        elif isinstance(node, list):
            for n in node:
                walk(n)
    walk(settings.get("model") or settings)
    return out


# ------------------------------------------------------------------ build
async def _call(client, sources: dict, name: str, path: str, params: dict | None = None):
    """One NX GET; records per source how many answered and the first failure. Returns None on failure."""
    s = sources.setdefault(name, {"name": name, "ok": 0, "failed": 0, "detail": ""})
    try:
        r = await asyncio.wait_for(client._get(path, params), CALL_MAX_S)
        s["ok"] += 1
        return r
    except httpx.HTTPStatusError as e:
        s["failed"] += 1
        code = e.response.status_code
        s["detail"] = s["detail"] or ("not available on this NX" if code in (404, 405, 501) else
                                      "the portal's NX account isn't allowed" if code in (401, 403) else f"HTTP {code}")
    except (asyncio.TimeoutError, httpx.HTTPError, ValueError) as e:
        s["failed"] += 1
        s["detail"] = s["detail"] or type(e).__name__
    return None


async def _history(site_id: int, an: _Analytics) -> None:
    """The portal's own alarms: analytics event types and zones seen per camera."""
    since = int(time.time() * 1000) - HISTORY_DAYS * 86400_000
    async with sessionmaker()() as db:
        rows = (await db.execute(
            select(Alarm.device_id, Alarm.event_subtype, Alarm.event_type, Alarm.description, Alarm.caption,
                   func.count(), func.max(Alarm.event_ts_ms))
            .where(Alarm.site_id == site_id, Alarm.event_ts_ms >= since, Alarm.event_subtype != "",
                   Alarm.event_type.in_(("analytics", "analyticsObject", "analyticsSdkEvent", "analyticsSdkObjectDetected")))
            .group_by(Alarm.device_id, Alarm.event_subtype, Alarm.event_type, Alarm.description, Alarm.caption)
            .limit(2000))).all()
    for dev, sub, et, desc, cap, n, last in rows:
        if et in ("analyticsObject", "analyticsSdkObjectDetected"):
            an.object_type(dev, sub, source="portal alarms")
            continue
        zone = zone_from_text(desc)
        an.event_type(dev, sub, name=cap if cap and not cap.startswith("AXIS") else "", zone=zone,
                      source="portal alarms", seen=n, last_ms=last or 0)
        if zone:
            an.zone(dev, zone, kind="scenario", source="events")


def _from_log(rows: list, an: _Analytics) -> None:
    for row in rows or []:
        ev = row.get("eventData") or {}
        dev = ev.get("deviceId") or ""
        if ev.get("objectTypeId") and not ev.get("eventTypeId"):
            an.object_type(dev, ev["objectTypeId"], source="NX event log")
            continue
        tid = ev.get("eventTypeId")
        if not tid:
            continue
        cap = ev.get("caption") or ""
        zone = zone_from_text(ev.get("description") or "")
        an.event_type(dev, tid, name=cap, engine=uid(ev.get("engineId")), zone=zone, source="NX event log", seen=1,
                      last_ms=int(row.get("timestampMs") or 0), key=(ev.get("key") or "").rsplit("/", 1)[-1])
        if zone:
            an.zone(dev, zone, kind="scenario", engine=uid(ev.get("engineId")), source="events")


async def build(site: Site, rules: list[dict] | None = None) -> dict:
    t0 = time.monotonic()
    client = make_client(site.host, site.nx_user, decrypt(site.nx_pass_enc))
    sources: dict[str, dict] = {}
    an = _Analytics()
    try:
        ev_m, act_m, devices, engines_raw, users, groups, servers = await asyncio.gather(
            _call(client, sources, "Event catalogue", "/rest/v4/events/manifest/events"),
            _call(client, sources, "Action catalogue", "/rest/v4/events/manifest/actions"),
            _call(client, sources, "Cameras", "/rest/v4/devices"),
            _call(client, sources, "Analytics plugins", "/rest/v4/analytics/engines"),
            _call(client, sources, "Users", "/rest/v4/users"),
            _call(client, sources, "User groups", "/rest/v4/userGroups"),
            _call(client, sources, "Servers", "/rest/v4/servers"))
        if not isinstance(ev_m, dict) or not isinstance(act_m, dict):
            raise RuntimeError("NX didn't return its rule catalogue (" + "; ".join(
                f'{s["name"]}: {s["detail"]}' for s in sources.values() if s["failed"]) + ")")
        devices = [d for d in devices or [] if isinstance(d, dict) and (d.get("deviceType") or "Camera") != "Server"]
        names = {uid(d.get("id")): d.get("name") or "" for d in devices}
        engines = {uid(e.get("id")): e for e in engines_raw or [] if isinstance(e, dict)}

        # engine-level manifests (some NX versions include them in the engine list) + the analytics taxonomy
        engine_types: dict[str, tuple[list, list]] = {eid: _manifest_types(e) for eid, e in engines.items()}
        tax = await _call(client, sources, "Analytics taxonomy", "/rest/v4/analytics/taxonomy")
        tax_names: dict[str, str] = {}
        if isinstance(tax, dict):
            for k in ("eventTypes", "objectTypes"):
                v = tax.get(k)
                for t in (v.values() if isinstance(v, dict) else v or []):
                    if isinstance(t, dict) and t.get("id"):
                        tax_names[t["id"]] = t.get("name") or ""

        # per camera × enabled analytics plugin: device-agent manifest (types) and settings (zones)
        sem = asyncio.Semaphore(PARALLEL)
        async def agent(dev: dict, eid: str):
            did, ename = uid(dev.get("id")), (engines.get(eid) or {}).get("name", "")
            async with sem:
                m = await _call(client, sources, "Plugin camera manifests", f"/rest/v4/analytics/engines/{eid}/deviceAgents/{did}/manifest")
                st = await _call(client, sources, "Plugin camera settings (zones)", f"/rest/v4/analytics/engines/{eid}/deviceAgents/{did}/settings")
            evs, obs = _manifest_types(m) if m else engine_types.get(eid, ([], []))
            for e in evs:
                an.event_type(did, e["id"], name=e["name"] or tax_names.get(e["id"], ""), engine=eid, source="plugin")
            for o in obs:
                an.object_type(did, o["id"], name=o["name"] or tax_names.get(o["id"], ""), source="plugin")
            for z in zones_from_settings(st or {}):
                an.zone(did, z["name"], kind=z["kind"], engine=eid, points=z["points"], source=f"{ename} settings")
        jobs = [agent(d, uid(e)) for d in devices for e in (d.get("userEnabledAnalyticsEngineIds") or [])]

        async def log_scan():
            since = int(time.time() * 1000) - LOG_DAYS * 86400_000
            rows = await _call(client, sources, "NX event log (analytics, last 7 days)", "/rest/v4/events/log",
                               {"eventType": "analytics", "startTimeMs": since, "limit": LOG_LIMIT, "order": "desc"})
            _from_log(rows if isinstance(rows, list) else [], an)
        budget = max(5.0, BUILD_MAX_S - (time.monotonic() - t0))
        try:                                    # a slow site still opens the editor with whatever was found in time
            await asyncio.wait_for(asyncio.gather(*jobs, log_scan(), _history(site.id, an)), budget)
        except asyncio.TimeoutError:
            sources["Time limit"] = {"name": "Time limit", "ok": 0, "failed": 1,
                                     "detail": f"stopped after {budget:.0f} s; some cameras weren't checked"}

        for r in rules or []:                                   # what existing rules already filter on
            ev = r.get("event") or {}
            ids = (ev.get("devices") or {}).get("ids") or [""]
            for d in ids:
                if ev.get("eventTypeId"):
                    an.event_type(d, ev["eventTypeId"], source="existing rules")
                if ev.get("objectTypeId"):
                    an.object_type(d, ev["objectTypeId"], source="existing rules")
        for tid, n in tax_names.items():                          # taxonomy names for anything found elsewhere
            for d in an.dev.values():
                if tid in d["event_types"] and not d["event_types"][tid]["name"]:
                    d["event_types"][tid]["name"] = n
                if tid in d["object_types"] and not d["object_types"][tid]["name"]:
                    d["object_types"][tid]["name"] = n
    finally:
        await client.close()

    group_list = [{"id": uid(g.get("id")), "name": g.get("name") or ""} for g in groups or [] if isinstance(g, dict)]
    known = {g["id"] for g in group_list}
    group_list += [{"id": i, "name": n} for i, n in PREDEFINED_GROUPS.items() if i not in known]
    cameras = []
    for d in devices:
        io = (d.get("options") or {}).get("ioSettings") or []
        cameras.append({"id": uid(d.get("id")), "name": d.get("name") or "", "model": d.get("model") or "",
                        "engines": [{"id": uid(e), "name": (engines.get(uid(e)) or {}).get("name", "")}
                                    for e in d.get("userEnabledAnalyticsEngineIds") or []],
                        "inputs": [{"id": p.get("id"), "name": p.get("inputName") or p.get("id")} for p in io if p.get("portType") == "input"],
                        "outputs": [{"id": p.get("id"), "name": p.get("outputName") or p.get("id")} for p in io if p.get("portType") == "output"]})
    took = round(time.monotonic() - t0, 1)
    log.info("site %s rule catalogue built in %.1f s: %s", site.id, took,
             ", ".join(f'{s["name"]} {s["ok"]}/{s["ok"] + s["failed"]}' for s in sources.values()))
    return {"read_at": time.time(), "took_s": took,
            "events": shape_manifest(ev_m, "event"), "actions": shape_manifest(act_m, "action"),
            "cameras": sorted(cameras, key=lambda c: c["name"].lower()),
            "servers": [{"id": uid(s.get("id")), "name": s.get("name") or ""} for s in servers or [] if isinstance(s, dict)],
            "users": sorted(({"id": uid(u.get("id")), "name": u.get("fullName") or u.get("name") or ""} for u in users or []
                             if isinstance(u, dict) and u.get("isEnabled", True) is not False), key=lambda u: u["name"].lower()),
            "groups": sorted(group_list, key=lambda g: g["name"].lower()),
            "engines": sorted(({"id": i, "name": e.get("name") or ""} for i, e in engines.items()), key=lambda e: e["name"].lower()),
            "analytics": an.out(names),
            "sources": [{**s, "ok": s["ok"], "failed": s["failed"]} for s in sources.values()]}


async def get(site: Site, rules: list[dict] | None = None, refresh: bool = False) -> dict:
    """The cached catalogue for a site (built at most once at a time per site)."""
    c = _cache.get(site.id)
    if c and not refresh and time.time() - c["read_at"] < CACHE_S:
        return c
    lock = _locks.setdefault(site.id, asyncio.Lock())
    async with lock:
        c = _cache.get(site.id)
        if c and not refresh and time.time() - c["read_at"] < CACHE_S:
            return c
        if c and refresh and time.time() - c["read_at"] < 2:          # two clicks on "Read again"
            return c
        c = await build(site, rules)
        _cache[site.id] = c
        return c


def forget(site_id: int) -> None:
    _cache.pop(site_id, None)
