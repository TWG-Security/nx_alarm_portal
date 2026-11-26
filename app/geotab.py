"""Geotab (MyGeotab) API client: a company's fleet on the map from Geotab GO devices. ZenduONE / GoFleet run on
Geotab (the OZINT SOC, 2026-10-26: "we are using ZenduitONE/Go Fleet, which has gps on all 15 cars"), so their
customers' vehicles are read here like any MyGeotab database.

From Geotab's API reference (developers.geotab.com, read 2026-10-26), not yet verified against a live database:
  - JSON-RPC 2.0 over HTTPS POST to https://<server>/apiv1: {"method": ..., "params": {...}} -> {"result": ...} or
    {"error": {"message", "data": {"type"}, "errors": [{"name"}]}} (HTTP 200 either way).
  - Authenticate {database, userName, password} -> {credentials: {database, userName, sessionId}, path}. path is
    "ThisServer" or the server that holds the database: later calls go there. 10 Authenticate calls per minute.
    Sessions last 14 days; a password change or more than 100 sessions ends them early. InvalidUserException = wrong
    login, or a session that is no longer valid ("authenticate again").
  - Get {typeName, search?, resultsLimit?, credentials}: Device (id, name, serialNumber, activeTo; licensePlate and
    vehicleIdentificationNumber on GO devices), DeviceStatusInfo (device {id}, latitude, longitude, dateTime,
    bearing, speed km/h (NaN = unknown), isDeviceCommunicating; 900 Gets per minute), LogRecord (GPS history:
    device, dateTime, latitude, longitude, speed; LogRecordSearch {deviceSearch {id}, fromDate, toDate}).
  - A GO device reports at least every 100 s while driving; parked, the last position stays (dateTime ages).
"""
from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone

import httpx

from app.config import get_settings
from app.fleetio import Vehicle, _parse_time

TIMEOUT = httpx.Timeout(10.0, connect=5.0)
REQUEST_MAX_S = 15.0
DEVICES_EVERY_S = 600       # the device list (names, plates, retired) is re-read this often; positions every round
HISTORY_LIMIT = 500
REFUSED_WAIT_S = 600        # after Geotab refuses the login, don't try it again for this long (it could lock the user)
KMH = 1 / 3.6


class GeotabError(Exception):
    """A problem talking to Geotab, worded for the Sites page."""

    def __init__(self, message: str, retry_after_s: float | None = None):
        super().__init__(message)
        self.retry_after_s = retry_after_s


def _error_name(err: dict) -> str:
    data = err.get("data") if isinstance(err.get("data"), dict) else {}
    errors = err.get("errors") if isinstance(err.get("errors"), list) else []
    return str(data.get("type") or (errors[0].get("name") if errors and isinstance(errors[0], dict) else "") or "")


def _num(x) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def vehicle(dev: dict, status: dict | None) -> Vehicle | None:
    """A Geotab Device (+ its DeviceStatusInfo) -> Vehicle; None for a record without an id."""
    if not isinstance(dev, dict) or not dev.get("id"):
        return None
    st = status if isinstance(status, dict) else {}
    lat, lng = _num(st.get("latitude")), _num(st.get("longitude"))
    ok = lat is not None and lng is not None and -90 <= lat <= 90 and -180 <= lng <= 180 and not (lat == 0 and lng == 0)
    speed = _num(st.get("speed"))
    plate = str(dev.get("licensePlate") or "").strip()
    serial = str(dev.get("serialNumber") or "").strip()
    return Vehicle(id=str(dev["id"]), name=str(dev.get("name") or f"Device {dev['id']}").strip()[:100],
                   describe=" · ".join(x for x in (plate, f"GO {serial}" if serial else "") if x)[:200], plate=plate[:40],
                   lat=lat if ok else None, lng=lng if ok else None,
                   at=_parse_time(st.get("dateTime")) if ok else None, address="",
                   speed=round(max(speed, 0) * KMH, 2) if ok and speed is not None else None)


def _active(dev: dict, now: datetime) -> bool:
    """Retired devices have activeTo in the past (Geotab's default is far in the future)."""
    end = _parse_time(dev.get("activeTo"))
    return end is None or end > now


class Client:
    def __init__(self, database: str, username: str, password: str, server: str = "", transport=None):
        self._db, self._user, self._password = database, username, password
        server = (server or "").strip().removeprefix("https://").removeprefix("http://").strip("/")
        self._base = f"https://{server}" if server else get_settings().geotab_api_base.rstrip("/")
        self._http = httpx.AsyncClient(timeout=TIMEOUT, transport=transport,
                                       headers={"User-Agent": "TWG-Alarm-Portal", "Accept": "application/json"})
        self._creds: dict | None = None
        self._refused: tuple[float, str] | None = None     # (monotonic time, message) of the last refused login
        self._devices: tuple[float, list[dict]] | None = None
        self.stats: dict = {}                              # what the last read held (Test connection shows it)

    async def close(self) -> None:
        await self._http.aclose()

    async def _post(self, method: str, params: dict):
        try:
            r = await asyncio.wait_for(self._http.post(f"{self._base}/apiv1", json={"method": method, "params": params}),
                                       REQUEST_MAX_S)
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            raise GeotabError(f"Geotab didn't answer ({type(e).__name__})") from None
        if r.status_code == 429:
            raise GeotabError("Geotab's rate limit: trying again in a minute", retry_after_s=60)
        if r.status_code >= 400:
            raise GeotabError(f"Geotab answered HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError:
            raise GeotabError("Geotab sent something that isn't JSON") from None
        if not isinstance(body, dict):
            raise GeotabError("Geotab's answer wasn't in the expected format")
        return body

    @staticmethod
    def _raise(err: dict) -> None:
        name, msg = _error_name(err), str(err.get("message") or "")[:200]
        if name == "OverLimitException":
            raise GeotabError("Geotab's rate limit: trying again in a minute", retry_after_s=60)
        if name == "DbUnavailableException":
            raise GeotabError("Geotab says the database is unavailable (maintenance, or the database name is wrong)")
        raise GeotabError(f"Geotab refused the request: {msg or name or 'no reason given'}")

    async def _login(self) -> None:
        if self._refused and time.monotonic() - self._refused[0] < REFUSED_WAIT_S:
            raise GeotabError(self._refused[1])
        body = await self._post("Authenticate", {"database": self._db, "userName": self._user, "password": self._password})
        if "error" in body:
            err = body["error"] if isinstance(body["error"], dict) else {}
            if _error_name(err) == "InvalidUserException" or "nvalid" in str(err.get("message", "")):
                msg = ("Geotab refused the database, user name or password (not tried again for 10 minutes, "
                       "or until they're saved again)")
                self._refused = (time.monotonic(), msg)
                raise GeotabError(msg)
            self._raise(err)
        res = body.get("result") if isinstance(body.get("result"), dict) else {}
        creds = res.get("credentials")
        if not isinstance(creds, dict) or not creds.get("sessionId"):
            raise GeotabError("Geotab's sign-in answer had no session")
        path = str(res.get("path") or "ThisServer")
        if path != "ThisServer":                      # the database lives on another Geotab server: use it from now on
            self._base = f"https://{path.removeprefix('https://').strip('/')}"
        self._refused, self._creds = None, creds

    async def _call(self, method: str, params: dict):
        """A call with the session; a session Geotab no longer accepts is renewed once."""
        for attempt in (1, 2):
            if self._creds is None:
                await self._login()
            body = await self._post(method, {**params, "credentials": self._creds})
            if "error" not in body:
                return body.get("result")
            err = body["error"] if isinstance(body["error"], dict) else {}
            expired = _error_name(err) == "InvalidUserException" or "invalid session" in str(err.get("message", "")).lower()
            if expired and attempt == 1:
                self._creds = None
                continue
            self._raise(err)

    async def _get(self, type_name: str, search: dict | None = None, limit: int | None = None) -> list[dict]:
        params: dict = {"typeName": type_name}
        if search:
            params["search"] = search
        if limit:
            params["resultsLimit"] = limit
        got = await self._call("Get", params)
        if not isinstance(got, list):
            raise GeotabError(f"Geotab's {type_name} list wasn't in the expected format")
        return [r for r in got if isinstance(r, dict)]

    async def _device_list(self) -> list[dict]:
        if self._devices is None or time.monotonic() - self._devices[0] > DEVICES_EVERY_S:
            self._devices = (time.monotonic(), await self._get("Device", limit=5000))
        return self._devices[1]

    async def vehicles(self) -> list[Vehicle]:
        """Every active device with its last known position."""
        devices = await self._device_list()
        statuses = {str((s.get("device") or {}).get("id")): s for s in await self._get("DeviceStatusInfo", limit=5000)
                    if isinstance(s.get("device"), dict)}
        now = datetime.now(timezone.utc)
        active = [d for d in devices if _active(d, now)]
        out = [v for v in (vehicle(d, statuses.get(str(d.get("id")))) for d in active) if v is not None]
        self.stats = {"devices": len(devices), "active": len(active), "statuses": len(statuses),
                      "with_position": sum(1 for v in out if v.at is not None)}
        return out

    async def history(self, vehicle_id: str, since: datetime) -> list[tuple[datetime, float, float]]:
        """GPS points Geotab logged for this device after `since`, oldest first."""
        rows = await self._get("LogRecord", {"deviceSearch": {"id": vehicle_id}, "fromDate": _iso(since),
                                             "toDate": _iso(datetime.now(timezone.utc))}, limit=HISTORY_LIMIT)
        out = []
        for r in rows:
            lat, lng, at = _num(r.get("latitude")), _num(r.get("longitude")), _parse_time(r.get("dateTime"))
            if at is not None and at > since and lat is not None and lng is not None and not (lat == 0 and lng == 0):
                out.append((at, lat, lng))
        return sorted(set(out))

    async def vehicle(self, vehicle_id: str) -> Vehicle | None:
        devs = await self._get("Device", {"id": vehicle_id})
        if not devs:
            return None
        st = await self._get("DeviceStatusInfo", {"deviceSearch": {"id": vehicle_id}})
        return vehicle(devs[0], st[0] if st else None)
