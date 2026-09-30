"""Fake NX Witness server for local end-to-end testing of the portal.

Run:   uvicorn tools.fake_nx:app --port 8199
Add a site in the portal with host http://127.0.0.1:8199 (any username/password).
Fire events:  curl -X POST localhost:8199/_inject/panic   (critical: soft trigger)
              curl -X POST localhost:8199/_inject/line    (alarm: analytics line crossing)
              curl -X POST localhost:8199/_inject/dock    (warning: camera disconnected)
Inspect NX-side write-backs:  curl localhost:8199/_state
"""
import io, time, uuid
from fastapi import FastAPI, Request
from fastapi.responses import Response, JSONResponse
app = FastAPI()
EVENTS, ACKS, BOOKMARKS = [], [], []
DEV = [{"id": "{11111111-1111-1111-1111-111111111111}", "name": "Front Gate PTZ", "deviceType": "Camera"},
       {"id": "{22222222-2222-2222-2222-222222222222}", "name": "Loading Dock", "deviceType": "Camera"}]
# 1x1 grey JPEG
JPG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b080001000101011100ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbfcffd9")

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
@app.get("/rest/v4/devices/{d}/image")
async def image(d: str): return Response(JPG, media_type="image/jpeg")
@app.post("/rest/v4/events/acknowledges")
async def ack(req: Request): ACKS.append(await req.json()); return {"id": str(uuid.uuid4())}
@app.post("/rest/v4/devices/{d}/bookmarks")
async def bm(d: str, req: Request): BOOKMARKS.append(await req.json()); return {"id": str(uuid.uuid4())}
@app.get("/_state")
async def state(): return {"acks": ACKS, "bookmarks": BOOKMARKS, "events": len(EVENTS)}
@app.post("/_inject/{kind}")
async def inject(kind: str):
    ts = int(time.time()*1000); aid = str(uuid.uuid4())
    base = {"timestampMs": ts, "ruleId": str(uuid.uuid4()), "flags": "noFlags", "aggregatedInfo": {"total": 1}}
    dev = DEV[0]["id"] if kind != "dock" else DEV[1]["id"]
    if kind == "panic":
        ev = {"type": "softTrigger", "state": "instant", "deviceId": dev, "userId": "{u-1}", "triggerName": "", "timestamp": str(ts*1000)}
        act = {"id": aid, "type": "desktopNotification", "acknowledge": False, "serverId": "{srv-1}", "sourceName": "Front Gate PTZ", "deviceIds": [dev]}
    elif kind == "line":
        ev = {"type": "analytics", "state": "instant", "deviceId": dev, "caption": "Rule Engine: Line Detector - Crossed", "eventTypeId": "nx.onvif.RuleEngine.LineDetector.Crossed", "timestamp": str(ts*1000)}
        act = {"id": aid, "type": "bookmark", "serverId": "{srv-1}", "sourceName": "Front Gate PTZ", "deviceIds": [dev]}
    else:
        ev = {"type": "deviceDisconnected", "state": "instant", "deviceId": DEV[1]["id"], "timestamp": str(ts*1000)}
        act = {"id": aid, "type": "desktopNotification", "serverId": "{srv-1}", "sourceName": "Loading Dock", "deviceIds": [DEV[1]["id"]]}
    EVENTS.append({**base, "eventData": ev, "actionData": act})
    return {"ok": True}
