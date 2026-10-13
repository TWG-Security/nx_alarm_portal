"""Levels of detail for textured site models (app/services/model_lod.py): the tile tree, simplification and,
above all, that every tile's texture atlas still puts the right picture on every face."""
import io
import json
import struct
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image

from app.services import model_build, model_lod
from tools.sample_model import make as sample_zip

# tools/sample_model.py paints each side of the building one colour; glTF axes are x = east, y = up, z = south.
COLOURS = {"south": (90, 90, 200), "east": (90, 160, 90), "north": (210, 40, 40), "west": (200, 200, 60), "roof": (60, 60, 60)}


def _build(tmp_path, data: bytes, name="m.zip", **job):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / f"upload{Path(name).suffix}").write_bytes(data)
    (job_dir / "job.json").write_text(json.dumps({"name": name, **job}))
    model_build.main(str(job_dir))
    return job_dir, json.loads((job_dir / "result.json").read_text())


def _read_glb(data: bytes):
    n = struct.unpack_from("<I", data, 12)[0]
    doc = json.loads(data[20:20 + n])
    blob = data[20 + n + 8:]

    def view(i):
        v = doc["bufferViews"][i]
        return blob[v.get("byteOffset", 0): v.get("byteOffset", 0) + v["byteLength"]]

    prim = doc["meshes"][0]["primitives"][0]
    acc = doc["accessors"]
    pos = np.frombuffer(view(acc[prim["attributes"]["POSITION"]]["bufferView"]), dtype=np.float32).reshape(-1, 3)
    uv = np.frombuffer(view(acc[prim["attributes"]["TEXCOORD_0"]]["bufferView"]), dtype=np.float32).reshape(-1, 2)
    ia = acc[prim["indices"]]
    idx = np.frombuffer(view(ia["bufferView"]), dtype=np.uint16 if ia["componentType"] == 5123 else np.uint32).reshape(-1, 3)
    img = Image.open(io.BytesIO(view(doc["images"][0]["bufferView"]))).convert("RGB")
    return doc, pos, uv, idx, img


def _tiles(ts: dict):
    yield ts
    for c in ts.get("children", []):
        yield from _tiles(c)


def test_every_tile_at_every_level_paints_each_face_its_own_colour(tmp_path, monkeypatch):
    monkeypatch.setattr(model_lod, "LEAF_FACES", 3)            # 12 faces -> a tree several levels deep
    job, r = _build(tmp_path, sample_zip())
    assert r["ok"], r
    assert r["lod"]["tiles"] > 4 and r["lod"]["levels"] >= 3
    tileset = json.loads((job / "tiles" / "tileset.json").read_text())
    tiles = list(_tiles(tileset["root"]))
    leaf_faces = 0
    for t in tiles:
        doc, pos, uv, idx, img = _read_glb((job / "tiles" / t["content"]["uri"]).read_bytes())
        assert "KHR_materials_unlit" in doc["materials"][0]["extensions"]
        assert ((uv >= 0) & (uv <= 1)).all()
        # Every vertex lies inside the tile's bounding box (tileset: east, north, up).
        c, h = np.array(t["boundingVolume"]["box"][:3]), np.array(t["boundingVolume"]["box"][3:12]).reshape(3, 3).diagonal()
        enu = np.c_[pos[:, 0], -pos[:, 2], pos[:, 1]]
        assert (np.abs(enu - c) <= h + 1e-3).all(), t["content"]["uri"]
        building = np.zeros(3)                                  # the building stands at the tileset's origin
        for f in idx:
            p = enu[f]
            normal = np.cross(p[1] - p[0], p[2] - p[0])
            normal /= np.linalg.norm(normal)
            centre = p.mean(axis=0)
            if abs(normal[2]) > 0.9:
                side = "roof"
            else:
                d = centre - building
                side = ("east" if d[0] > 0 else "west") if abs(d[0]) / 10 > abs(d[1]) / 6 else ("north" if d[1] > 0 else "south")
            u, v = uv[f].mean(axis=0)
            got = img.getpixel((min(img.width - 1, int(u * img.width)), min(img.height - 1, int(v * img.height))))
            assert max(abs(a - b) for a, b in zip(got, COLOURS[side])) < 40, (t["content"]["uri"], side, got)
        if "children" not in t:
            leaf_faces += len(idx)
            assert t["geometricError"] == 0
        else:
            assert t["refine"] == "REPLACE"
            assert all(t["geometricError"] > c["geometricError"] for c in t["children"])
    assert leaf_faces == 12                                     # the leaves hold every face exactly once
    assert tileset["geometricError"] > tiles[0]["geometricError"]


def test_simplification_keeps_tile_borders_and_texture_seams():
    """A textured grid split in two tiles: the parent is simplified a lot, but the outer border and the seam
    between the two texture charts stay, so neighbouring tiles meet and textures don't stretch across charts."""
    n = 40
    xs, ys = np.meshgrid(np.linspace(0, 20, n + 1), np.linspace(0, 10, n + 1))
    V = np.c_[xs.ravel(), ys.ravel(), np.zeros(xs.size)]
    quads = [(j * (n + 1) + i, j * (n + 1) + i + 1, (j + 1) * (n + 1) + i + 1, (j + 1) * (n + 1) + i)
             for j in range(n) for i in range(n)]
    F = np.array([f for a, b, c, d in quads for f in ((a, b, c), (a, c, d))])
    m = model_lod.Textured()
    # Two charts: the west half and the east half have separate vertices (and UVs) along x = 10.
    west = V[:, 0] <= 10
    east = V[:, 0] >= 10
    remap_e = -np.ones(len(V), dtype=np.int64)
    remap_e[east] = np.arange(east.sum()) + len(V)
    Fw = F[west[F].all(axis=1)]
    Fe = remap_e[F[east[F].all(axis=1)]]
    Vall = np.r_[V, V[east]]
    m.V = Vall
    m.F = np.r_[Fw, Fe]
    m.T = np.zeros(len(Vall), dtype=np.int32)
    m.size = [(256, 256)]
    m.PX = np.r_[V[:, :2] / [20, 10] * [128, 256], V[east][:, :2] / [20, 10] * [128, 256] + [128, 0]]
    faces, err = model_lod._simplify(m, m.F, 10, 1.0)
    assert len(faces) < len(m.F) / 4 and err < 1e-3            # flat: collapses for free
    used = np.unique(faces)
    pos = m.V[used]
    for corner in ((0, 0), (20, 0), (20, 10), (0, 10)):
        assert (np.abs(pos[:, :2] - corner).sum(axis=1) < 1e-6).any(), corner
    # No face mixes the two charts' vertices.
    side = used >= len(V)
    lookup = dict(zip(used.tolist(), side.tolist()))
    assert all(len({lookup[v] for v in f}) == 1 for f in faces.tolist())


def test_repeating_textures_are_converted_without_levels_of_detail(tmp_path):
    import trimesh
    mesh = trimesh.creation.box(extents=(2, 2, 2))
    mesh.visual = trimesh.visual.TextureVisuals(uv=(mesh.vertices[:, :2] + 1) * 2, image=Image.new("RGB", (8, 8), (1, 2, 3)))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("m.glb", trimesh.Scene(mesh).export(file_type="glb"))
    job, r = _build(tmp_path, buf.getvalue())
    assert r["ok"] and "lod" not in r and (job / "tiles" / "p0001.glb").exists()
    assert not (job / "lodtex").exists()
