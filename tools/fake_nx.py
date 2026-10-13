"""Fake NX Witness server for local end-to-end testing of the portal.

Run:   uvicorn tools.fake_nx:app --port 8199
Add a site in the portal with host http://127.0.0.1:8199 (any username/password).
Fire events:  curl -X POST localhost:8199/_inject/panic   (critical: soft trigger)
              curl -X POST localhost:8199/_inject/line    (alarm: analytics line crossing)
              curl -X POST localhost:8199/_inject/dock    (warning: camera disconnected)
              curl -X POST localhost:8199/_inject/panic24 (critical soft trigger from a rule tagged #24h)
Inspect NX-side write-backs:  curl localhost:8199/_state
Clips: /rest/v4/devices/{id}/media.mp4 returns an ffmpeg test pattern (MPEG-4 Part 2, like many
NX secondary streams) tagged with NX's startTimeMs comment; set FAKE_NX_FFMPEG if ffmpeg isn't on PATH.
Injected events are also pushed to JSON-RPC subscribers (wss /jsonrpc), like NX 6.1.
Every injected event also gets an analytics object track with a box that moves across the frame.
Also a fake Twilio (dev_up.sh points TWILIO_API_BASE here): texts land in GET /_twilio;
POST /_twilio/mode/{ok|fail|hang|mediafail} makes it accept, reject (400, code 30034), hang for 30 s, or accept
an MMS and then report it failed (63019, picture not downloadable) on GET .../Messages/{sid}.json.
POST /_frames/{normal|late|none} makes recorded frames (timestampMs != -1) answer 200 with 0 bytes, like NX for a
motion-recording camera that isn't recording yet: "late" for 1.5 s after each injected event, "none" always.
"""
import json, os, subprocess, tempfile, time, uuid
import asyncio
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, JSONResponse
app = FastAPI()
EVENTS, ACKS, BOOKMARKS, TRACKS = [], [], [], []
SUBSCRIBERS: set = set()   # JSON-RPC websockets subscribed to rest.v4.events.log
FFMPEG = os.environ.get("FAKE_NX_FFMPEG", "ffmpeg")
DEV = [{"id": "{11111111-1111-1111-1111-111111111111}", "name": "Front Gate PTZ", "deviceType": "Camera"},
       {"id": "{22222222-2222-2222-2222-222222222222}", "name": "Loading Dock", "deviceType": "Camera"}]
# 1x1 grey JPEG
JPG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b080001000101011100ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbfcffd9")

@app.post("/rest/v4/login/tickets")
async def ticket(): return {"token": "ticket"}

@app.websocket("/jsonrpc")
async def jsonrpc(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            req = await ws.receive_json()
            if req.get("method") == "rest.v4.events.log.subscribe":
                start = int((req.get("params") or {}).get("startTimeMs") or 0)
                SUBSCRIBERS.add(ws)
                await ws.send_json({"jsonrpc": "2.0", "id": req.get("id"), "result": [e for e in EVENTS if e["timestampMs"] >= start]})
            else:
                await ws.send_json({"jsonrpc": "2.0", "id": req.get("id"), "error": {"code": -32601, "message": "API handler is not found"}})
    except WebSocketDisconnect:
        SUBSCRIBERS.discard(ws)

@app.post("/rest/v3/login/sessions")
async def login(): return {"token": "fake"}
@app.get("/rest/v4/site/info")
async def info(): return {"name": "Fake Test Site", "version": "6.1.2.42921", "synchronizedTimeMs": int(time.time()*1000)}
@app.get("/rest/v4/devices")
async def devices(): return DEV
@app.get("/rest/v4/users")
async def users(): return [{"id": "{u-1}", "name": "operator1", "fullName": "Test Operator"}]
@app.get("/rest/v4/events/log")
async def log(startTimeMs: int = 0): return [e for e in EVENTS if e["timestampMs"] >= startTimeMs]
# One fixed rule per inject kind; panic24's is tagged #24h (raises even while the site is disarmed).
# The line-crossing rule has NX's "Interval of action" set, so the portal flags it.
RULES = {k: {"id": "{%s}" % uuid.uuid5(uuid.NAMESPACE_URL, k), "comment": c, "enabled": True,
             "event": {"type": t}, "action": {"type": "writeToLog", "intervalS": i}}
         for k, c, t, i in (("panic", "Panic button", "softTrigger", 0), ("line", "Line crossing", "analytics", 30),
                            ("dock", "Camera offline", "deviceDisconnected", 0),
                            ("panic24", "Hold-up button #24h", "softTrigger", 0))}
@app.get("/rest/v4/events/rules")
async def rules(): return list(RULES.values())
def _rule_by_id(rid: str):
    return next((r for r in RULES.values() if r["id"].strip("{}") == rid.strip("{}")), None)

@app.get("/rest/v4/events/rules/{rid}")
async def rule_one(rid: str):
    r = _rule_by_id(rid)
    return r if r else JSONResponse({"error": "not found"}, status_code=404)

RULE_PATCHES: list = []
@app.patch("/rest/v4/events/rules/{rid}")
async def rule_patch(rid: str, req: Request):
    """Like NX: a partial update of the fields sent (RULE_PATCHES records them for tests)."""
    r = _rule_by_id(rid)
    if not r:
        return JSONResponse({"error": "not found"}, status_code=404)
    body = await req.json()
    RULE_PATCHES.append({"id": rid, **body})
    r.update(body)
    return r

@app.get("/_rule_patches")
async def rule_patches(): return RULE_PATCHES

@app.post("/_rules/{kind}")
async def set_rule_comment(kind: str, comment: str = ""):
    """Tests: change a rule's Title/Comment (its #tags), like editing it in NX."""
    RULES[kind]["comment"] = comment
    return RULES[kind]
_FRAME: list = []
FRAMES = {"mode": "normal", "last_inject": 0.0}
@app.post("/_frames/{mode}")
async def frames_mode(mode: str):
    FRAMES["mode"] = mode
    return FRAMES
@app.get("/rest/v4/devices/{d}/image")
async def image(d: str, timestampMs: int = -1):
    if timestampMs != -1 and (FRAMES["mode"] == "none" or
                              (FRAMES["mode"] == "late" and time.time() - FRAMES["last_inject"] < 1.5)):
        return Response(b"", media_type="image/jpeg")       # NX: no recording at or after that moment (yet)
    if not _FRAME:   # a real 1280x720 test-pattern frame (the 1x1 JPG is the fallback without ffmpeg)
        try:
            _FRAME.append(subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=1",
                                          "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
                                         capture_output=True, check=True).stdout)
        except Exception:
            _FRAME.append(JPG)
    return Response(_FRAME[0], media_type="image/jpeg")
@app.post("/rest/v4/events/acknowledges")
async def ack(req: Request): ACKS.append(await req.json()); return {"id": str(uuid.uuid4())}
@app.post("/rest/v4/devices/{d}/bookmarks")
async def bm(d: str, req: Request): BOOKMARKS.append(await req.json()); return {"id": str(uuid.uuid4())}
@app.get("/rest/v4/devices/{d}/media.mp4")
async def media(d: str, positionMs: int, durationMs: int = 10000):
    start = positionMs - 500                       # NX starts on the keyframe before positionMs
    with tempfile.TemporaryDirectory() as tmp:
        out = f"{tmp}/clip.mp4"
        # In a thread and niced: the real NX makes clips on its own server, so building them here must
        # not hold up this fake's event push (or starve the portal under test of CPU).
        await asyncio.to_thread(subprocess.run, ["nice", "-n", "19", FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15",
                        "-t", str((durationMs + 500) / 1000), "-c:v", "mpeg4", "-q:v", "5",
                        "-metadata", "comment=" + json.dumps({"startTimeMs": str(start), "version": 4}), out], check=True)
        return Response(open(out, "rb").read(), media_type="video/mp4")

@app.get("/media/{d}.webm")
async def live_webm(d: str):
    """Live view like NX's /media/<id>.webm: a VP8 test pattern streamed in real time."""
    from fastapi.responses import StreamingResponse
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-v", "error", "-re", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15", "-c:v", "libvpx",
        "-deadline", "realtime", "-b:v", "600k", "-f", "webm", "pipe:1", stdout=asyncio.subprocess.PIPE)
    async def body():
        try:
            while chunk := await proc.stdout.read(16384):
                yield chunk
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
    return StreamingResponse(body(), media_type="video/webm")

@app.get("/rest/v4/analytics/objectTracks")
async def tracks(startTimeMs: int = 0, endTimeMs: int = 2**62):
    return [t for t in TRACKS if t["endTimeMs"] >= startTimeMs and t["startTimeMs"] <= endTimeMs]

@app.get("/rest/v4/analytics/objectTracks/{tid}")
async def track(tid: str): return next(t for t in TRACKS if t["id"] == tid)

@app.get("/rest/v4/analytics/objectTracks/{tid}/objectMetadata")
async def metadata(tid: str):
    t = next(t for t in TRACKS if t["id"] == tid)
    n = (t["endTimeMs"] - t["startTimeMs"]) // 200
    return [{"timestampMs": t["startTimeMs"] + i * 200, "durationMs": 0, "attributes": [],
             "boundingBox": f"{0.1 + 0.6 * i / n:.4f},0.35,0.18x0.4"} for i in range(n + 1)]

@app.get("/_state")
async def state(): return {"acks": ACKS, "bookmarks": BOOKMARKS, "events": len(EVENTS)}
@app.post("/_inject/{kind}")
async def inject(kind: str):
    ts = int(time.time()*1000); aid = str(uuid.uuid4()); FRAMES["last_inject"] = time.time()
    base = {"timestampMs": ts, "ruleId": RULES.get(kind, RULES["panic"])["id"], "flags": "noFlags", "aggregatedInfo": {"total": 1}}
    dev = DEV[0]["id"] if kind != "dock" else DEV[1]["id"]
    if kind in ("panic", "panic24"):
        ev = {"type": "softTrigger", "state": "instant", "deviceId": dev, "userId": "{u-1}", "triggerName": "", "timestamp": str(ts*1000)}
        act = {"id": aid, "type": "desktopNotification", "acknowledge": False, "serverId": "{srv-1}", "sourceName": "Front Gate PTZ", "deviceIds": [dev]}
    elif kind == "line":
        ev = {"type": "analytics", "state": "instant", "deviceId": dev, "caption": "Rule Engine: Line Detector - Crossed", "eventTypeId": "nx.onvif.RuleEngine.LineDetector.Crossed", "timestamp": str(ts*1000)}
        act = {"id": aid, "type": "bookmark", "serverId": "{srv-1}", "sourceName": "Front Gate PTZ", "deviceIds": [dev]}
    else:
        ev = {"type": "deviceDisconnected", "state": "instant", "deviceId": DEV[1]["id"], "timestamp": str(ts*1000)}
        act = {"id": aid, "type": "desktopNotification", "serverId": "{srv-1}", "sourceName": "Loading Dock", "deviceIds": [DEV[1]["id"]]}
    TRACKS.append({"id": str(uuid.uuid4()), "deviceId": dev.strip("{}"), "objectTypeId": "nx.base.Person",
                   "startTimeMs": ts - 2000, "endTimeMs": ts + 4000, "attributes": [{"name": "Clothing", "value": "Dark jacket"}]})
    ev["objectTrackId"] = TRACKS[-1]["id"]
    EVENTS.append({**base, "eventData": ev, "actionData": act})
    for ws in list(SUBSCRIBERS):   # push like NX does
        try: await ws.send_json({"jsonrpc": "2.0", "method": "rest.v4.events.log.update", "params": EVENTS[-1]})
        except Exception: SUBSCRIBERS.discard(ws)
    return {"ok": True}


# ---------------------------------------------------------------- fake Twilio (alarm texts)
TEXTS, TWILIO_MODE, STATUS = [], {"mode": "ok"}, {}

@app.post("/2010-04-01/Accounts/{sid}/Messages.json")
async def twilio_send(sid: str, request: Request):
    form = await request.form()
    if TWILIO_MODE["mode"] == "hang":
        await asyncio.sleep(30)
    if TWILIO_MODE["mode"] == "fail":
        return JSONResponse({"code": 30034, "message": "Message from an unregistered number", "status": 400}, status_code=400)
    TEXTS.append({"to": form.get("To"), "from": form.get("From") or form.get("MessagingServiceSid"), "body": form.get("Body"),
                  "media": form.get("MediaUrl") or "",
                  "at_ms": int(time.time() * 1000)})
    sid = "SM" + uuid.uuid4().hex
    TEXTS[-1]["sid"] = sid
    STATUS[sid] = ({"status": "failed", "error_code": 63019} if TWILIO_MODE["mode"] == "mediafail" and form.get("MediaUrl")
                   else {"status": "delivered", "error_code": None})
    return JSONResponse({"sid": sid, "status": "queued"}, status_code=201)

@app.get("/2010-04-01/Accounts/{acct}/Messages/{sid}.json")
async def twilio_status(acct: str, sid: str):
    return {"sid": sid, **STATUS.get(sid, {"status": "sent", "error_code": None})}

@app.get("/_twilio")
async def twilio_texts(): return TEXTS

@app.post("/_twilio/mode/{mode}")
async def twilio_mode(mode: str):
    TWILIO_MODE["mode"] = mode
    return TWILIO_MODE


# ---------------------------------------------------------------- fake Peplink InControl2 (guard tracking)
# dev_up.sh points PEPLINK_API_BASE at /_ic2. Client ID "dev-client", secret "dev-secret". Shapes copied from
# the real IC2 (2026-10-13): answers wrapped {"resp_code": "SUCCESS", "caller_ref", "server_ref", "data"};
# errors 404 {"error", "error_description"}. The per-device /loc is live with ts in the group's LOCAL time and
# "group_timezone" / "group_time" in the envelope; the org-wide /loc is a stale cache with ts in UTC.
# The patrol car (device 3) drives a 150 m circle around the dev site, one lap in 10 minutes, a new position
# every IC2_STEP_S. POST /_ic2/mode/{ok|fail|hang|stop}: normal, 500s, 30 s hangs, or the car stops reporting.
import math as _math
from datetime import datetime as _dt, timezone as _tz
from zoneinfo import ZoneInfo as _Zone
IC2 = {"mode": "ok", "tokens": set(), "stopped_at": None, "calls": [], "names": {}, "status": {}}
IC2_STEP_S, IC2_SITE = 20, (40.2665, -76.8838)

def _ic2_local(t):
    return _dt.fromtimestamp(t, _tz.utc).astimezone(_Zone("America/New_York")).strftime("%Y-%m-%dT%H:%M:%S")

def _ok(data, **extra):
    return {"resp_code": "SUCCESS", "caller_ref": "fake", "server_ref": "fake", **extra, "data": data}

def _ic2_utc(t):
    return _dt.fromtimestamp(t, _tz.utc).strftime("%Y-%m-%dT%H:%M:%S")

def _ic2_car(local=True, age_s=0):
    t = (IC2["stopped_at"] or time.time()) - age_s
    t = t - t % IC2_STEP_S
    a = 2 * _math.pi * (t % 600) / 600
    lat = IC2_SITE[0] + 150 * _math.cos(a) / 111320
    lng = IC2_SITE[1] + 150 * _math.sin(a) / (111320 * _math.cos(_math.radians(IC2_SITE[0])))
    return {"la": round(lat, 6), "lo": round(lng, 6), "sp": 1.57, "at": 152.0, "ts": _ic2_local(t) if local else _ic2_utc(t),
            "isExist": True, "isStatic": False, "speed_mph": 3.51}

TRAILER = {"la": 40.2671, "lo": -76.8829, "isExist": True}
TRAILER_T = time.time() - 86400                      # last reported a day ago

def _ic2_devices(org_wide=True):
    """org_wide: like IC2's org-wide /loc, positions are a cache 30 min old with UTC timestamps.
    Names can be changed with POST /_ic2/rename/{device}?name=... (like renaming a router in InControl2), and a
    router switched off with POST /_ic2/status/{device}?status=offline."""
    return [{**d, "name": IC2["names"].get(d["id"], d["name"]), "status": IC2["status"].get(d["id"], d["status"])}
            for d in _ic2_devices_raw(org_wide)]

def _ic2_devices_raw(org_wide=True):
    trailer = {**TRAILER, "ts": _ic2_utc(TRAILER_T) if org_wide else _ic2_local(TRAILER_T)}
    return [{"id": 3, "group_id": 38, "sn": "293B-1E37-276B", "name": "Patrol Car 1", "status": "online",
             "product_name": "Pepwave MAX BR1 Pro 5G", "points": [_ic2_car(local=not org_wide, age_s=1800 if org_wide else 0)]},
            {"id": 7, "group_id": 38, "sn": "192F-C07C-CB87", "name": "Parked Trailer", "status": "offline",
             "product_name": "Peplink MAX Transit Duo Pro", "points": [trailer]},
            {"id": 9, "group_id": 2, "sn": "2933-7A2D-DA42", "name": "Office AP", "status": "online",
             "product_name": "Pepwave AP One",       # like real IC2: no GPS = its configured address, stamped "now"
             "points": [{"la": 40.2668, "lo": -76.8840, "ts": _ic2_utc(time.time()) if org_wide else _ic2_local(time.time()),
                         "isExist": False, "isStatic": True}]}]

async def _ic2_gate(request: Request):
    IC2["calls"].append(request.url.path)
    if IC2["mode"] == "hang":
        await asyncio.sleep(30)
    if IC2["mode"] == "fail":
        return JSONResponse({"error": "server_error", "error_description": "Internal error"}, status_code=500)
    tok = request.headers.get("authorization", "").removeprefix("Bearer ")
    if tok not in IC2["tokens"]:
        return JSONResponse({"error": "invalid_access", "error_description": "Invalid Access, Access Denied."}, status_code=401)
    return None

@app.post("/_ic2/api/oauth2/token")
async def ic2_token(request: Request):
    form = await request.form()
    if form.get("client_id") != "dev-client" or form.get("client_secret") != "dev-secret":
        return JSONResponse({"error": "invalid_client", "error_description": "Bad client credentials"}, status_code=401)
    tok = uuid.uuid4().hex
    IC2["tokens"].add(tok)
    return {"access_token": tok, "token_type": "bearer", "expires_in": 172800}

@app.get("/_ic2/rest/o")
async def ic2_orgs(request: Request):
    return await _ic2_gate(request) or _ok([{"id": "devorg", "name": "TWG Dev (fake)", "org_code": "ABC123", "status": "active",
                                              "branch_id": 5, "url": "https://example.test/o/devorg/overview"},
                                             {"id": "blocked", "name": "Blocked Customer (fake)", "org_code": "DEF456", "status": "active",
                                              "branch_id": 5, "url": "https://example.test/o/blocked/overview"}])

@app.get("/_ic2/rest/o/{org}/loc")
async def ic2_org_loc(org: str, request: Request):
    return await _ic2_gate(request) or _ok(_ic2_devices(org_wide=True))

@app.get("/_ic2/rest/o/{org}/g/{group}/d/{dev}")
async def ic2_device(org: str, group: int, dev: int, request: Request):
    d = next((x for x in _ic2_devices() if x["id"] == dev), None)
    if d is None:
        return JSONResponse({"error": "not_found", "error_description": "Device not found"}, status_code=404)
    return await _ic2_gate(request) or _ok({
        **{k: d[k] for k in ("id", "group_id", "sn", "name", "status", "product_name")},
        "gps_support": True, "gps_exist": True, "group_timezone_label": "America/New_York",
        "admin_conf": {"admin_name": "admin", "admin_password": "must-never-be-stored"}})

@app.get("/_ic2/rest/o/{org}/g/{group}/d/{dev}/loc")
async def ic2_device_loc(org: str, group: int, dev: int, request: Request):
    d = next((x for x in _ic2_devices(org_wide=False) if x["id"] == dev), None)
    if d is None:
        return JSONResponse({"error": "not_found", "error_description": "Device not found"}, status_code=404)
    return await _ic2_gate(request) or _ok(d["points"], group_time=_ic2_local(time.time()), group_timezone="America/New_York")

@app.post("/_ic2/mode/{mode}")
async def ic2_mode(mode: str):
    if mode == "reset":
        IC2["names"].clear()
        IC2["status"].clear()
    IC2["mode"] = "ok" if mode in ("stop", "reset") else mode
    IC2["stopped_at"] = time.time() if mode == "stop" else None
    return {"mode": mode}

@app.post("/_ic2/status/{dev}")
async def ic2_status(dev: int, status: str):
    IC2["status"][dev] = status
    return {"ok": True}

@app.post("/_ic2/rename/{dev}")
async def ic2_rename(dev: int, name: str):
    IC2["names"][dev] = name
    return {"ok": True}

@app.get("/_ic2/calls")
async def ic2_calls(): return IC2["calls"][-200:]

# IC2 groups (Organization → Group → Router picker). A second org with API access blocked, like CSTK.
@app.get("/_ic2/rest/o/{org}/g")
async def ic2_groups(org: str, request: Request):
    if org == "blocked":
        return JSONResponse({"error": "invalid_access", "error_description": "Authentication fail! API access blocked in organization settings."}, status_code=401)
    return await _ic2_gate(request) or _ok([
        {"id": 38, "name": "Security Pod", "online_device_count": 1, "offline_device_count": 1, "timezone": "America/New_York", "is_gps_tracking_disabled": False},
        {"id": 2, "name": "Office", "online_device_count": 1, "offline_device_count": 0, "timezone": "America/New_York", "is_gps_tracking_disabled": False}])

@app.get("/_ic2/rest/o/{org}/g/{group}/loc")
async def ic2_group_loc(org: str, group: int, request: Request):
    rows = [{**d, "group_time": _ic2_local(time.time()), "is_gps_tracking_disabled": False}
            for d in _ic2_devices(org_wide=False) if d["group_id"] == group]
    return await _ic2_gate(request) or _ok(rows)
