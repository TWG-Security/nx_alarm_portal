"""Fleetio API client: TWG's vans (and any company's fleet vehicles) on the map from Fleetio's last known positions.

From Fleetio's published OpenAPI spec (version 2025-05-05, https://developer.fleetio.com/schemas/2025-05-05.yaml),
not yet verified against a live account:
  - Base https://secure.fleetio.com/api; every call sends "Authorization: Token <API key>" and "Account-Token: <id>"
    (both from https://secure.fleetio.com/api_keys).
  - GET /vehicles?per_page=100[&start_cursor=...] -> {records: [VehicleSummary], next_cursor, ...}; archived vehicles
    are left out. Each record has id, name, license_plate, vin, year, make, model, group_name, vehicle_status_name,
    is_sample and current_location_entry {date, address, geolocation {latitude, longitude}} (null when Fleetio has no
    position yet). GET /vehicles/{id} returns the same for one vehicle.
  - Positions come from whatever feeds Fleetio (a telematics integration, Fleetio Go on the driver's phone, fuel /
    inspection entries), so how fresh they are depends on that feed; `date` says when each was recorded.
  - Rate limits depend on the Fleetio plan; a 429 carries Retry-After (seconds).
  - The version answered is the API key's locked one unless X-Api-Version says otherwise: always sent (API_VERSION).
  - GET /vehicles/{id}/location_entries?filter[created_at][gt]=...&sort[created_at]=desc&per_page=100 -> {records:
    [LocationEntry {id, date, geolocation, ...}]}: the vehicle's position history, read for the map's trail
    (history()). The list alone only gives the newest position, and a poll a minute caught too few to draw a trail
    (the user, 2026-10-16: "the fleetio cars do not show trailing").
  - First live read (TWG, 2026-10-14 18:07 UTC): 19 vehicles listed, none with a position on the map. In case the
    list carries only current_location_entry_id, that entry is read (GET /vehicles/{id}/location_entries/{entry},
    once per entry id); `stats` says what each list held (Sites page → Test connection).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from app.config import get_settings

TIMEOUT = httpx.Timeout(10.0, connect=5.0)
REQUEST_MAX_S = 15.0
API_VERSION = "2025-05-05"  # the spec this client was written from
PAGE = 100                 # Fleetio's largest page
ENTRY_LOOKUPS_MAX = 50     # location entries read by id per round (only new entry ids cost a call)
MAX_PAGES = 20             # 2,000 vehicles: far beyond a security company's fleet, and a cap on a runaway loop


class FleetioError(Exception):
    """A problem talking to Fleetio, worded for the Sites page."""

    def __init__(self, message: str, retry_after_s: float | None = None):
        super().__init__(message)
        self.retry_after_s = retry_after_s


@dataclass
class Vehicle:
    id: str
    name: str
    describe: str              # "2021 Ford Transit · ABC1234" (shown under the name)
    plate: str
    lat: float | None
    lng: float | None
    at: datetime | None        # when Fleetio recorded the position (UTC)
    address: str


def _parse_time(s) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def vehicle(rec: dict, loc: dict | None = None) -> Vehicle | None:
    """One Fleetio vehicle record -> Vehicle (None for Fleetio's sample data or a record without an id). loc: its
    location entry when it was read separately."""
    if not isinstance(rec, dict) or rec.get("id") is None or rec.get("is_sample"):
        return None
    loc = loc or rec.get("current_location_entry") or {}
    geo = loc.get("geolocation") or {}
    lat, lng = geo.get("latitude"), geo.get("longitude")
    ok = isinstance(lat, (int, float)) and isinstance(lng, (int, float)) and -90 <= lat <= 90 and -180 <= lng <= 180 \
        and not (lat == 0 and lng == 0)
    plate = str(rec.get("license_plate") or "").strip()
    model = " ".join(str(x) for x in (rec.get("year"), rec.get("make"), rec.get("model")) if x)
    return Vehicle(id=str(rec["id"]), name=str(rec.get("name") or f"Vehicle {rec['id']}").strip()[:100],
                   describe=" · ".join(x for x in (model, plate) if x)[:200], plate=plate[:40],
                   lat=float(lat) if ok else None, lng=float(lng) if ok else None,
                   at=_parse_time(loc.get("date")) if ok else None, address=str(loc.get("address") or "")[:300])


class Client:
    def __init__(self, account_token: str, api_key: str, base: str | None = None, transport=None):
        self._base = (base or get_settings().fleetio_api_base).rstrip("/")
        self._http = httpx.AsyncClient(timeout=TIMEOUT, transport=transport, headers={
            "User-Agent": "TWG-Alarm-Portal", "Accept": "application/json", "X-Api-Version": API_VERSION,
            "Authorization": f"Token {api_key}", "Account-Token": account_token})
        self._entries: dict[str, dict] = {}       # location entry id -> that entry (an id's position never changes)
        self.stats: dict = {}                     # what the last vehicle list held (Test connection shows it)

    async def close(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict | None = None):
        try:
            r = await asyncio.wait_for(self._http.get(f"{self._base}{path}", params=params), REQUEST_MAX_S)
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            raise FleetioError(f"Fleetio didn't answer ({type(e).__name__})") from None
        if r.status_code == 401:
            raise FleetioError("Fleetio refused the API key or Account Token (check both on secure.fleetio.com/api_keys)")
        if r.status_code == 403:
            raise FleetioError("This Fleetio API key isn't allowed to read vehicles (check its permissions in Fleetio)")
        if r.status_code == 429:
            try:
                wait = float(r.headers.get("Retry-After", "60"))
            except ValueError:
                wait = 60.0
            raise FleetioError(f"Fleetio's rate limit: try again in {int(wait)} s", retry_after_s=wait)
        if r.status_code >= 400:
            raise FleetioError(f"Fleetio answered HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise FleetioError("Fleetio sent something that isn't JSON") from None

    async def vehicles(self) -> list[Vehicle]:
        """Every (non-archived, non-sample) vehicle with its last known position."""
        records, cursor = [], None
        for _ in range(MAX_PAGES):
            page = await self._get("/vehicles", {"per_page": PAGE, **({"start_cursor": cursor} if cursor else {})})
            got = page.get("records") if isinstance(page, dict) else page
            if not isinstance(got, list):
                raise FleetioError("Fleetio's vehicle list wasn't in the expected format")
            records += [r for r in got if isinstance(r, dict) and r.get("id") is not None and not r.get("is_sample")]
            cursor = page.get("next_cursor") if isinstance(page, dict) else None
            if not cursor:
                break
        stats = {"vehicles": len(records), "entry_in_list": 0, "entry_id_only": 0, "no_entry": 0, "field_missing": 0,
                 "looked_up": 0, "lookup_failed": 0}
        out, lookups, used = [], 0, {}
        for r in records:
            loc = None
            if r.get("current_location_entry"):
                stats["entry_in_list"] += 1
            elif r.get("current_location_entry_id"):
                stats["entry_id_only"] += 1
                eid = str(r["current_location_entry_id"])
                loc = self._entries.get(eid)
                if loc is None and lookups < ENTRY_LOOKUPS_MAX:
                    lookups += 1
                    try:
                        loc = await self._get(f"/vehicles/{int(r['id'])}/location_entries/{int(eid)}")
                        self._entries[eid] = loc if isinstance(loc, dict) else {}
                        stats["looked_up"] += 1
                    except FleetioError:
                        stats["lookup_failed"] += 1
                if eid in self._entries:
                    used[eid] = self._entries[eid]
            else:
                stats["no_entry"] += 1
            if "current_location_entry" not in r and "current_location_entry_id" not in r:
                stats["field_missing"] += 1
            v = vehicle(r, loc if isinstance(loc, dict) else None)
            if v is not None:
                out.append(v)
        stats["with_position"] = sum(1 for v in out if v.at is not None)
        self.stats, self._entries = stats, used              # keep only the vehicles' current entries
        return out

    async def history(self, vehicle_id: str, since: datetime) -> list[tuple[datetime, float, float]]:
        """Positions Fleetio recorded for this vehicle after `since`, oldest first (at most one page, 100)."""
        slack = since.astimezone(timezone.utc).timestamp() - 120          # created_at can trail the recorded date
        page = await self._get(f"/vehicles/{int(vehicle_id)}/location_entries", {
            "per_page": 100, "sort[created_at]": "desc",
            "filter[created_at][gt]": datetime.fromtimestamp(slack, timezone.utc).isoformat()})
        rows = page.get("records") if isinstance(page, dict) else page
        out = []
        for e in rows if isinstance(rows, list) else []:
            v = vehicle({"id": vehicle_id}, e if isinstance(e, dict) else None)
            if v is not None and v.at is not None and v.at > since:
                out.append((v.at, v.lat, v.lng))
        return sorted(set(out))

    async def vehicle(self, vehicle_id: str) -> Vehicle | None:
        rec = await self._get(f"/vehicles/{int(vehicle_id)}")
        loc = None
        if isinstance(rec, dict) and not rec.get("current_location_entry") and rec.get("current_location_entry_id"):
            loc = await self._get(f"/vehicles/{int(vehicle_id)}/location_entries/{int(rec['current_location_entry_id'])}")
        return vehicle(rec, loc if isinstance(loc, dict) else None)
