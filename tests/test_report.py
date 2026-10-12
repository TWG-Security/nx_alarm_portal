"""Incident report PDF and evidence package. Needs ffmpeg (like the clip tests)."""
import hashlib
import io
import json
import time
import zipfile

import httpx
import pytest
import respx
from PIL import Image
from pypdf import PdfReader
from sqlalchemy import select

from app.models import AuditLog
from app.services import clips
from app.services.poller import ingest
from tests.conftest import NX, login, make_site, nx_row
from tests.test_clips import FFMPEG, FFPROBE, media_dir, synthetic_clip  # noqa: F401  (media_dir is an autouse fixture)

pytestmark = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not available")


def _jpeg(color=(40, 90, 160), size=(1280, 720)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def _mock_nx(tmp_path, ts: int):
    body = synthetic_clip(tmp_path / "nx.mp4", "libx264", ts - 10_500, seconds=31)   # NX starts on the keyframe before
    respx.post(f"{NX}/rest/v3/login/sessions").mock(return_value=httpx.Response(200, json={"token": "tok"}))
    respx.get(url__regex=rf"{NX}/rest/v4/devices/dev-1/media\.mp4.*").mock(
        return_value=httpx.Response(200, content=body, headers={"content-type": "video/mp4"}))
    respx.get(url__regex=rf"{NX}/rest/v4/devices/dev-1/image.*").mock(
        return_value=httpx.Response(200, content=_jpeg(), headers={"content-type": "image/jpeg"}))
    respx.post(url__regex=rf"{NX}/rest/v4/devices/dev-1/bookmarks").mock(return_value=httpx.Response(200, json={"id": "bm"}))
    respx.get(f"{NX}/rest/v4/analytics/objectTracks").mock(return_value=httpx.Response(200, json=[
        {"id": "trk-1", "objectTypeId": "nx.base.Person", "startTimeMs": ts - 3000, "endTimeMs": ts + 6000,
         "attributes": [{"name": "Clothing", "value": "Dark jacket"}]}]))
    respx.get(f"{NX}/rest/v4/analytics/objectTracks/trk-1/objectMetadata").mock(return_value=httpx.Response(200, json=[
        {"timestampMs": ts + i * 200 - 3000, "durationMs": 0, "boundingBox": "0.3,0.3,0.2x0.4"} for i in range(46)]))


async def _acked_alarm(client, session, admin, ts):
    tenant, user = admin
    site = await make_site(session, tenant)
    site.address, site.timezone = "4251 Chestnut St, Emmaus, PA", "America/New_York"
    await session.commit()
    [alarm] = await ingest(session, site, [nx_row(ts, type_="analyticsObject", caption="Person in restricted area",
                                                  objectTrackId="{trk-1}")], {})
    await session.commit()
    await login(client, user.email)
    r = await client.post(f"/api/alarms/{alarm.id}/ack", json={"note": "Verified on camera: contractor, no action needed", "verdict": "false"})
    assert r.status_code == 200, r.text
    return alarm


@respx.mock
async def test_pdf_report_has_summary_times_stills_timeline_and_notes(client, session, admin, tmp_path):
    ts = int(time.time() * 1000) - 300_000
    _mock_nx(tmp_path, ts)
    alarm = await _acked_alarm(client, session, admin, ts)

    await client.post(f"/api/alarms/{alarm.id}/notes", json={"text": "Contractor's office confirmed the visit"})
    r = await client.get(f"/api/alarms/{alarm.id}/export", params={"format": "pdf", "note": "Police case 26-4411"})

    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert f'incident-report_alarm-{alarm.id}.pdf' in r.headers["content-disposition"]
    pdf = PdfReader(io.BytesIO(r.content))
    text = "\n".join(p.extract_text() for p in pdf.pages)
    for expected in ("INCIDENT REPORT", "Person in restricted area", "Test Site", "4251 Chestnut St", "Front Door Cam",
                     "Event-time frame", "Sequence from the clip", "Detected by analytics", "Dark jacket",
                     "Timeline", "Acknowledged", "Operator notes", "contractor, no action needed",
                     "Police case 26-4411", "Clip SHA-256", "UTC", "Page 1 of", "FALSE ALARM", "False alarm.", "Follow-up, by", "office confirmed the visit", "Note added"):
        assert expected in text, expected
    assert "EDT" in text or "EST" in text                                # site time zone
    images = sum(len(p.images) for p in pdf.pages)
    assert images >= 7                                                    # event frame + 6 stills (+ logo)
    audit = (await session.scalars(select(AuditLog).where(AuditLog.action == "alarm.exported"))).one()
    assert audit.detail["format"] == "pdf" and audit.detail["note"] == "Police case 26-4411"
    assert audit.detail["clip_sha256"] in text.replace("\n", "")


@respx.mock
async def test_zip_package_has_clip_stills_and_matching_checksums(client, session, admin, tmp_path):
    ts = int(time.time() * 1000) - 300_000
    _mock_nx(tmp_path, ts)
    alarm = await _acked_alarm(client, session, admin, ts)

    r = await client.get(f"/api/alarms/{alarm.id}/export", params={"format": "zip"})

    assert r.status_code == 200, r.text
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    root = names[0].split("/")[0]
    assert root.startswith(f"alarm-{alarm.id}_") and r.headers["content-disposition"].endswith(f'{root}.zip"')
    files = {n.split("/", 1)[1]: z.read(n) for n in names}
    assert {"incident-report.pdf", "clip.mp4", "manifest.json", "SHA256SUMS.txt"} <= set(files)
    stills = [n for n in files if n.startswith("frames/") and not n.startswith("frames/annotated/")]
    annotated = [n for n in files if n.startswith("frames/annotated/")]
    # Event frame + 6 stills; the person is in view from -3 s to +6 s, so -5 s and +10 s get no box.
    assert len(stills) == 7 and len(annotated) == 5
    assert not any("-5s" in n or "+10s" in n for n in annotated)
    manifest = json.loads(files["manifest.json"])
    for name, digest in manifest["files"].items():
        assert hashlib.sha256(files[name]).hexdigest() == digest, name
    assert manifest["ack_note"].startswith("Verified on camera") and manifest["clip"]["start_ms"] == ts - 10_500
    sums = dict(line.split("  ")[::-1] for line in files["SHA256SUMS.txt"].decode().splitlines())
    assert sums["clip.mp4"] == hashlib.sha256(files["clip.mp4"]).hexdigest()
    # Originals are untouched; the annotated copy differs where the box is.
    assert files[stills[1]] != files[annotated[1]]


async def test_export_while_recording_says_when_to_retry(client, session, admin):
    tenant, user = admin
    site = await make_site(session, tenant)
    [alarm] = await ingest(session, site, [nx_row(int(time.time() * 1000))], {})
    await session.commit()
    await login(client, user.email)
    r = await client.get(f"/api/alarms/{alarm.id}/export", params={"format": "zip", "post": 200})
    assert r.status_code == 409 and "still being recorded" in r.json()["detail"]


async def test_alarm_without_camera_exports_a_pdf_without_video(client, session, admin):
    tenant, user = admin
    site = await make_site(session, tenant)
    [alarm] = await ingest(session, site, [nx_row(8_000_000, type_="storageIssue", device="", caption="Disk failed")], {})
    await session.commit()
    await login(client, user.email)
    assert (await client.get(f"/api/alarms/{alarm.id}/export", params={"format": "pdf"})).status_code == 200
    # The media endpoints 404 alarms without a camera; exports must still find the alarm.
    text = PdfReader(io.BytesIO((await client.get(f"/api/alarms/{alarm.id}/export")).content)).pages[0].extract_text()
    assert "Disk failed" in text


async def test_export_is_tenant_scoped(client, session, admin):
    from tests.conftest import make_tenant_user
    tenant, _ = admin
    _, other = await make_tenant_user(session, "Other Co", "ops@other.test", role="operator")
    site = await make_site(session, tenant)
    [alarm] = await ingest(session, site, [nx_row(9_000_000, device="")], {})
    await session.commit()
    await login(client, other.email)
    assert (await client.get(f"/api/alarms/{alarm.id}/export")).status_code == 404
