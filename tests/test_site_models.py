"""Site 3D models: chunked upload, conversion in a separate process, placement, serving, and the converter
itself (Terra OBJ + metadata.xml, 3D Tiles, glTF/STL, textures, unsafe zips)."""
import asyncio
import io
import json
import math
import struct
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from app.config import get_settings
from app.routers import site_models
from app.services import model_build
from tests.conftest import login, make_site, make_tenant_user
from tools.sample_model import make as sample_zip


@pytest.fixture(autouse=True)
def model_dir(tmp_path):
    s = get_settings()
    old = s.model_dir
    s.model_dir = str(tmp_path / "models")
    yield Path(s.model_dir)
    s.model_dir = old


async def _drain():
    for _ in range(600):
        if not site_models._tasks:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("conversion didn't finish")


async def _upload(client, site_id, data: bytes, name="TWG_Office.zip", chunk=None, up="auto"):
    r = await client.post(f"/api/sites/{site_id}/model/uploads", json={"name": name, "size": len(data), "up": up})
    assert r.status_code == 200, r.text
    uid, step = r.json()["upload_id"], chunk or r.json()["chunk_bytes"]
    for off in range(0, len(data), step):
        r = await client.put(f"/api/sites/{site_id}/model/uploads/{uid}?offset={off}", content=data[off:off + step])
        assert r.status_code == 200, r.text
    assert r.json()["done"]
    await _drain()
    return (await client.get(f"/api/sites/{site_id}/model")).json()


def _glb_json(data: bytes) -> dict:
    n = struct.unpack_from("<I", data, 12)[0]
    return json.loads(data[20:20 + n])


async def test_terra_obj_upload_is_converted_placed_and_served(client, session):
    tenant, admin = await make_tenant_user(session)
    site = await make_site(session, tenant)
    await login(client, admin.email)
    data = sample_zip(40.2665, -76.8838)
    m = await _upload(client, site.id, data, chunk=len(data) // 3 + 1)
    ready = m["ready"]
    assert m["job"] is None and ready["kind"] == "mesh" and ready["pieces"] == 2 and ready["faces"] == 12
    assert ready["georef"].startswith("Terra metadata.xml (EPSG:32618)")
    p = ready["placement"]
    assert abs(p["lat"] - 40.2665) < 1e-5 and abs(p["lng"] + 76.8838) < 1e-5
    # UTM grid north is ~1.2 degrees west of true north at this longitude (zone 18, central meridian -75).
    assert abs(((p["heading"] + 180) % 360 - 180) - (-1.88 * math.sin(math.radians(40.2665)))) < 0.05
    assert p["height"] == 0 and p == ready["origin"]

    ts = await client.get(ready["url"])
    assert ts.status_code == 200 and "immutable" in ts.headers["cache-control"]
    tileset = ts.json()
    assert tileset["asset"]["version"] == "1.1" and len(tileset["root"]["children"]) == 2
    glb = await client.get(ready["url"].replace("tileset.json", "p0001.glb"))
    doc = _glb_json(glb.content)
    assert glb.headers["content-type"] == "model/gltf-binary"
    assert "KHR_materials_unlit" in doc["extensionsUsed"] and doc["images"][0]["mimeType"] == "image/jpeg"
    # Stored Y-up (glTF): the 6 m tall building's height is on Y, its 20 m length on X, 12 m depth on Z.
    pos = doc["accessors"][doc["meshes"][0]["primitives"][0]["attributes"]["POSITION"]]
    ext = [round(hi - lo, 1) for lo, hi in zip(pos["min"], pos["max"])]
    assert ext == [20.0, 6.0, 12.0]

    # Every textured piece carries texture coordinates (a texture without them is an invalid glTF that
    # breaks Cesium's shaders): Terra's OBJ uses separate position / texture indices.
    for piece in ("p0001.glb", "p0002.glb"):
        d = _glb_json((await client.get(ready["url"].replace("tileset.json", piece))).content)
        assert all("TEXCOORD_0" in prim["attributes"] for m in d["meshes"] for prim in m["primitives"]), piece

    # Only model files are served, only from inside the folder, only the version on show.
    base = ready["url"].rsplit("/", 2)[0]
    for bad in ("job.json", "result.json", "src/TWG_Office/metadata.xml", "tiles/%2E%2E/%2E%2E/%2E%2E/x.json", "%2E%2E/1/job.json"):
        assert (await client.get(f"{base}/{bad}")).status_code == 404, bad
    assert (await client.get(ready["url"].replace(f"/model/{ready['version']}/", "/model/99/"))).status_code == 404

    # The site list carries the model for the map page.
    s = next(x for x in (await client.get("/api/sites")).json() if x["id"] == site.id)
    assert s["model"]["ready"]["url"] == ready["url"]


async def test_placement_rebuild_replace_and_delete(client, session, model_dir):
    tenant, admin = await make_tenant_user(session)
    site = await make_site(session, tenant)
    await login(client, admin.email)
    ready = (await _upload(client, site.id, sample_zip()))["ready"]
    place = {"lat": 40.2666, "lng": -76.8839, "height": 1.5, "heading": 372, "scale": 1.1}
    r = await client.put(f"/api/sites/{site.id}/model/placement", json=place)
    assert r.status_code == 200 and r.json()["ready"]["placement"]["heading"] == 12
    assert (await client.put(f"/api/sites/{site.id}/model/placement", json={**place, "scale": 0})).status_code == 422

    # Rebuild with the other up axis: a new version, same placement, the old folder goes.
    r = await client.post(f"/api/sites/{site.id}/model/rebuild", json={"up": "y"})
    assert r.status_code == 200 and r.json()["job"]["status"] == "converting"
    await _drain()
    again = (await client.get(f"/api/sites/{site.id}/model")).json()["ready"]
    assert again["version"] == ready["version"] + 1 and again["up"] == "y" and again["placement"]["height"] == 1.5
    await asyncio.sleep(0.2)
    assert not (model_dir / str(site.id) / str(ready["version"])).exists()

    # A broken upload fails with a message and leaves the model on show alone.
    bad = await _upload(client, site.id, b"not a zip at all", name="broken.zip")
    assert bad["job"]["status"] == "failed" and "damaged" in bad["job"]["error"] and bad["ready"]["version"] == again["version"]
    assert (await client.delete(f"/api/sites/{site.id}/model")).status_code == 200
    assert (await client.get(f"/api/sites/{site.id}/model")).json() == {"ready": None, "job": None}
    await asyncio.sleep(0.2)
    assert not (model_dir / str(site.id) / str(again["version"])).exists()


async def test_upload_rules(client, session):
    tenant, admin = await make_tenant_user(session)
    site = await make_site(session, tenant)
    await login(client, admin.email)
    base = f"/api/sites/{site.id}/model/uploads"
    assert (await client.post(base, json={"name": "office.fbx", "size": 10})).status_code == 400
    assert (await client.post(base, json={"name": "huge.zip", "size": 10 * 1024**4})).status_code == 400
    r = await client.post(base, json={"name": "a.zip", "size": 100})
    uid = r.json()["upload_id"]
    assert (await client.post(base, json={"name": "b.zip", "size": 100})).status_code == 409      # one at a time
    r = await client.put(f"{base}/{uid}?offset=50", content=b"x" * 10)
    assert r.status_code == 409 and r.json()["detail"]["received"] == 0                          # resume point
    assert (await client.put(f"{base}/{uid}?offset=0", content=b"x" * 101)).status_code == 413
    assert (await client.delete(f"{base}/{uid}")).status_code == 200
    assert (await client.get(f"/api/sites/{site.id}/model")).json()["job"] is None
    assert (await client.put(f"{base}/{uid}?offset=0", content=b"x")).status_code == 404


async def test_operators_see_models_but_cant_change_them(client, session):
    from tests.test_users import _user
    tenant, admin = await make_tenant_user(session)
    site = await make_site(session, tenant)
    op = await _user(session, tenant, "noc@twgsecurity.com")
    await login(client, admin.email)
    ready = (await _upload(client, site.id, sample_zip()))["ready"]
    await login(client, op.email)
    assert (await client.get(ready["url"])).status_code == 200
    assert (await client.post(f"/api/sites/{site.id}/model/uploads", json={"name": "a.zip", "size": 9})).status_code == 403
    assert (await client.put(f"/api/sites/{site.id}/model/placement", json=ready["placement"])).status_code == 403
    assert (await client.delete(f"/api/sites/{site.id}/model")).status_code == 403


async def test_a_restart_resumes_conversion(client, session, model_dir):
    tenant, admin = await make_tenant_user(session)
    site = await make_site(session, tenant)
    job = model_dir / str(site.id) / "1"
    job.mkdir(parents=True)
    (job / "upload.zip").write_bytes(sample_zip())
    (job / "job.json").write_text(json.dumps({"name": "x.zip"}))
    site.model = {"site_id": site.id, "next_version": 2, "job": {"status": "converting", "version": 1, "name": "x.zip"}}
    await session.commit()
    await site_models.resume()
    await _drain()
    await login(client, admin.email)
    assert (await client.get(f"/api/sites/{site.id}/model")).json()["ready"]["version"] == 1


# ------------------------------------------------------------------------------------- the converter

def _job(tmp_path, name, data, up="auto"):
    job = tmp_path / "job"
    job.mkdir()
    (job / f"upload{Path(name).suffix}").write_bytes(data)
    (job / "job.json").write_text(json.dumps({"name": name, "up": up}))
    model_build.main(str(job))
    return job, json.loads((job / "result.json").read_text())


def test_zip_paths_outside_the_folder_are_refused(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../../evil.obj", "v 0 0 0\n")
    _, r = _job(tmp_path, "evil.zip", buf.getvalue())
    assert not r["ok"] and "Unsafe path" in r["error"]
    assert not (tmp_path / "evil.obj").exists()


def test_3d_tiles_are_kept_and_located(tmp_path):
    w, s, e, n = (math.radians(v) for v in (-76.8840, 40.2664, -76.8836, 40.2666))
    tileset = {"asset": {"version": "1.0"}, "geometricError": 10,
               "root": {"boundingVolume": {"region": [w, s, e, n, 105.0, 125.0]}, "geometricError": 0, "content": {"uri": "a.b3dm"}}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("terra_b3dms/tileset.json", json.dumps(tileset))
        z.writestr("terra_b3dms/a.b3dm", b"b3dm")
        z.writestr("terra_b3dms/readme.html", "<script>alert(1)</script>")
    _, r = _job(tmp_path, "office_b3dm.zip", buf.getvalue())
    assert r["ok"] and r["kind"] == "tiles" and r["tileset"] == "src/terra_b3dms/tileset.json"
    a = r["anchor"]
    assert abs(a["lat"] - 40.2665) < 1e-6 and abs(a["lng"] + 76.8838) < 1e-6 and a["h"] == 105.0


def test_stl_is_z_up_and_glb_is_y_up(tmp_path):
    import trimesh
    box = trimesh.creation.box(extents=(4, 2, 10))           # 10 m tall along Z
    _, r = _job(tmp_path, "tower.stl", box.export(file_type="stl"))
    assert r["ok"] and r["up"] == "z" and r["size_m"] == [4.0, 2.0, 10.0] and r["georef"] is None
    tall_y = trimesh.creation.box(extents=(4, 10, 2))        # glTF: 10 m tall along Y
    (tmp_path / "job").rename(tmp_path / "job1")
    _, r = _job(tmp_path, "tower.glb", trimesh.Scene(tall_y).export(file_type="glb"))
    assert r["ok"] and r["size_m"] == [4.0, 2.0, 10.0]


def test_huge_textures_are_scaled_down_to_jpeg(tmp_path):
    import trimesh
    mesh = trimesh.creation.box(extents=(1, 1, 1))
    uv = mesh.vertices[:, :2] + 0.5
    img = Image.new("RGB", (6000, 16), (10, 200, 10))
    mesh.visual = trimesh.visual.TextureVisuals(uv=uv, image=img)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("m.glb", trimesh.Scene(mesh).export(file_type="glb"))
    job, r = _job(tmp_path, "m.zip", buf.getvalue())
    assert r["ok"]
    data = (job / "tiles" / "p0001.glb").read_bytes()
    doc = _glb_json(data)
    view = doc["bufferViews"][doc["images"][0]["bufferView"]]
    blob_at = 20 + struct.unpack_from("<I", data, 12)[0] + 8
    img = Image.open(io.BytesIO(data[blob_at + view.get("byteOffset", 0): blob_at + view.get("byteOffset", 0) + view["byteLength"]]))
    assert img.format == "JPEG" and img.size == (4096, 11)


def test_point_clouds_are_explained(tmp_path):
    ply = b"ply\nformat ascii 1.0\nelement vertex 3\nproperty float x\nproperty float y\nproperty float z\nend_header\n0 0 0\n1 0 0\n0 1 0\n"
    _, r = _job(tmp_path, "cloud.ply", ply)
    assert not r["ok"] and "point cloud" in r["error"]
