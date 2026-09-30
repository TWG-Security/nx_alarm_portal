"""Clip pipeline tests. They need ffmpeg/ffprobe (set FFMPEG_PATH / FFPROBE_PATH, or have them on PATH)."""

import json
import shutil
import subprocess
import time

import httpx
import pytest
import respx

from app.config import get_settings
from app.services import clips
from app.services.poller import ingest
from tests.conftest import NX, login, make_site, make_tenant_user, nx_row

FFMPEG = shutil.which(get_settings().ffmpeg_path)
FFPROBE = shutil.which(get_settings().ffprobe_path)
pytestmark = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not available")


def synthetic_clip(path, codec: str, start_ms: int, seconds: int = 3) -> bytes:
    """A test-pattern MP4 tagged the way NX tags exports (comment JSON with startTimeMs)."""
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=10",
                    "-t", str(seconds), "-c:v", codec, "-pix_fmt", "yuv420p",
                    "-metadata", "comment=" + json.dumps({"startTimeMs": str(start_ms), "version": 4}), str(path)],
                   check=True)
    return path.read_bytes()


@pytest.fixture(autouse=True)
def media_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "media_cache_dir", str(tmp_path / "media"))
    monkeypatch.setattr(get_settings(), "prefetch_clips", False)
    clips._errors.clear()


async def _alarm(session, tenant, ts_ms, **kw):
    site = await make_site(session, tenant)
    [alarm] = await ingest(session, site, [nx_row(ts_ms, **kw)], {})
    await session.commit()
    return alarm


def _mock_nx(body: bytes):
    respx.post(f"{NX}/rest/v3/login/sessions").mock(return_value=httpx.Response(200, json={"token": "tok"}))
    return respx.get(url__regex=rf"{NX}/rest/v4/devices/dev-1/media\.mp4.*").mock(
        return_value=httpx.Response(200, content=body, headers={"content-type": "video/mp4"}))


@respx.mock
async def test_non_h264_clip_is_transcoded_and_start_time_read(session, admin, tmp_path):
    tenant, _ = admin
    ts = int(time.time() * 1000) - 120_000
    alarm = await _alarm(session, tenant, ts)
    route = _mock_nx(synthetic_clip(tmp_path / "src.mp4", "mpeg4", ts - 10_978))

    info = await clips.get_clip(alarm, wait_s=60)

    assert info.status == "ready" and info.transcoded and info.codec == "mpeg4"
    assert info.start_ms == ts - 10_978                           # true first-frame time from NX's tag
    q = route.calls.last.request.url.params
    assert q["positionMs"] == str(ts - 10_000) and q["durationMs"] == "30000" and q["stream"] == "secondary"
    out = clips.clip_path(clips.clip_key(alarm.id, 10, 20, "sd"))
    probe = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v", "-show_entries", "stream=codec_name",
                            "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout.strip()
    assert probe == "h264"
    assert (await clips.get_clip(alarm)).status == "ready" and route.call_count == 1   # cached


@respx.mock
async def test_h264_clip_is_only_rewrapped(session, admin, tmp_path):
    tenant, _ = admin
    ts = int(time.time() * 1000) - 120_000
    alarm = await _alarm(session, tenant, ts)
    _mock_nx(synthetic_clip(tmp_path / "src.mp4", "libx264", ts - 10_000))
    info = await clips.get_clip(alarm, wait_s=60)
    assert info.status == "ready" and not info.transcoded


async def test_clip_pending_until_footage_exists(session, admin):
    tenant, _ = admin
    alarm = await _alarm(session, tenant, int(time.time() * 1000))
    info = await clips.get_clip(alarm)
    assert info.status == "pending" and 20 <= info.ready_in_s <= 25


@respx.mock
async def test_bad_nx_response_reports_error(session, admin):
    tenant, _ = admin
    alarm = await _alarm(session, tenant, int(time.time() * 1000) - 120_000)
    _mock_nx(b"not a video")
    info = await clips.get_clip(alarm, wait_s=30)
    assert info.status == "error" and "no playable video" in info.message


@respx.mock
async def test_clip_endpoints_serve_ranges_and_enforce_tenant(client, session, admin, tmp_path):
    tenant, user = admin
    ts = int(time.time() * 1000) - 120_000
    alarm = await _alarm(session, tenant, ts)
    _mock_nx(synthetic_clip(tmp_path / "src.mp4", "libx264", ts - 10_000))
    await login(client, user.email)
    info = (await client.get(f"/api/alarms/{alarm.id}/clip")).json()
    for _ in range(50):
        if info["status"] == "ready":
            break
        info = (await client.get(f"/api/alarms/{alarm.id}/clip")).json()
    assert info["status"] == "ready"
    r = await client.get(info["url"], headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100 and r.headers["content-type"] == "video/mp4"

    _, other = await make_tenant_user(session, "Other", "x@other.test", role="operator")
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    await login(client, other.email)
    assert (await client.get(info["url"])).status_code == 404
    assert (await client.get(f"/api/alarms/{alarm.id}/clip")).status_code == 404


@respx.mock
async def test_objects_returns_per_frame_boxes(session, admin):
    tenant, _ = admin
    ts = int(time.time() * 1000) - 120_000
    alarm = await _alarm(session, tenant, ts, type_="analyticsObject", objectTrackId="{trk-1}")
    respx.post(f"{NX}/rest/v3/login/sessions").mock(return_value=httpx.Response(200, json={"token": "tok"}))
    respx.get(f"{NX}/rest/v4/analytics/objectTracks").mock(return_value=httpx.Response(200, json=[
        {"id": "trk-1", "objectTypeId": "nx.base.Person", "startTimeMs": ts - 1000, "endTimeMs": ts + 2000,
         "attributes": [{"name": "Color", "value": "Red"}]},
        {"id": "trk-2", "objectTypeId": "nx.base.Car", "startTimeMs": ts, "endTimeMs": ts + 500, "attributes": []}]))
    respx.get(f"{NX}/rest/v4/analytics/objectTracks/trk-1/objectMetadata").mock(return_value=httpx.Response(200, json=[
        {"timestampMs": ts, "durationMs": 0, "boundingBox": "0.1,0.2,0.3x0.4"},
        {"timestampMs": ts + 200, "durationMs": 0, "boundingBox": "0.11,0.2,0.3x0.4"}]))
    respx.get(f"{NX}/rest/v4/analytics/objectTracks/trk-2/objectMetadata").mock(return_value=httpx.Response(200, json=[
        {"timestampMs": ts, "durationMs": 0, "boundingBox": "0,0,0x0"}]))    # empty box -> track dropped

    tracks = await clips.objects(alarm, ts - 10_000, ts + 20_000)

    assert [t["id"] for t in tracks] == ["trk-1"]
    t = tracks[0]
    assert t["primary"] and t["label"] == "Person" and t["attributes"] == {"Color": "Red"}
    assert t["boxes"][0] == [ts, 0, 0.1, 0.2, 0.3, 0.4]
