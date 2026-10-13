"""Turn an uploaded site model into a 3D Tiles tileset that the 3D view streams.

Runs in its own low-priority process (app/site_models.py starts it with `python -m app.services.model_build
<job dir>`), so a big conversion can never hold up alarms in the portal process. It reads <job>/upload.*,
writes progress to <job>/status.json and the outcome to <job>/result.json.

Accepted (single file or a .zip):
  - 3D Tiles: any folder with a tileset.json (DJI Terra "B3DM", Cesium, Pix4D, ...): served as uploaded.
  - OBJ + MTL + textures, including DJI Terra's tiled OBJ export (Tile_*/Tile_*.obj + metadata.xml).
  - glTF / GLB, PLY and STL meshes.
Meshes become one GLB per file ("piece"), in metres, east/north/up around the model's centre, with the
model's floor at height 0; the tileset lets Cesium skip pieces that are off screen.

Placement: Terra's metadata.xml (SRS + SRSOrigin) puts the model where it was flown, rotated for the
projection's grid north. Georeferenced 3D Tiles keep their own position. Anything else starts at the
site's pin and the admin moves it into place in the 3D view.
"""
from __future__ import annotations

import io
import json
import math
import os
import shutil
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

MESH_EXTS = {".obj", ".glb", ".gltf", ".ply", ".stl"}
MAX_UNPACKED = 8 * 1024**3          # a zip may expand to at most 8 GB...
MAX_FILES = 50_000                   # ...in at most this many files
MAX_TEXTURE = 4096                   # bigger textures are scaled down (browsers and GPUs struggle above this)
SAMPLE_PER_PIECE = 20_000            # vertices sampled per piece to find the floor height
SPLIT_OBJ_BYTES = 48 * 1024**2      # an OBJ bigger than this is cut into pieces first (bounded memory)...
FACES_PER_PIECE = 250_000           # ...of at most this many faces, one material per piece
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}


class BuildError(Exception):
    """A problem the admin can fix (shown on the upload panel as is)."""


def _status(job: Path, step: str, pct: int) -> None:
    tmp = job / "status.json.tmp"
    tmp.write_text(json.dumps({"step": step, "pct": pct, "ts": time.time()}))
    tmp.replace(job / "status.json")


# ---------------------------------------------------------------------------------------------- unpack

def unpack(upload: Path, src: Path, name: str = "") -> None:
    """Zip -> src/, refusing paths that escape it, links and zip bombs. A single file is moved in."""
    src.mkdir(parents=True, exist_ok=True)
    if upload.suffix.lower() != ".zip":
        if upload.suffix.lower() not in MESH_EXTS:
            raise BuildError(f"Unsupported file type {upload.suffix}. Upload a .zip, .obj, .glb, .gltf, .ply or .stl")
        safe = "".join(c for c in Path(name).name if c.isalnum() or c in "._- ")[:100]
        shutil.move(str(upload), src / (safe if safe.lower().endswith(upload.suffix.lower()) else f"model{upload.suffix.lower()}"))
        return
    try:
        zf = zipfile.ZipFile(upload)
    except zipfile.BadZipFile:
        raise BuildError("That .zip file is damaged or incomplete") from None
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_FILES:
            raise BuildError(f"The zip has {len(infos)} files; the limit is {MAX_FILES}")
        if sum(i.file_size for i in infos) > MAX_UNPACKED:
            raise BuildError("The zip unpacks to more than 8 GB")
        root = src.resolve()
        for i in infos:
            if (i.external_attr >> 16) & 0o170000 == 0o120000:
                continue                                         # symlink: skipped
            name = i.filename.replace("\\", "/")
            if name.startswith("__MACOSX/") or Path(name).name.startswith("._"):
                continue
            dest = (root / name).resolve()
            if not dest.is_relative_to(root):
                raise BuildError(f"Unsafe path in the zip: {i.filename}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(i) as fin, open(dest, "wb") as fout:
                shutil.copyfileobj(fin, fout, 1024 * 1024)
    upload.unlink(missing_ok=True)


def _shallowest(paths: list[Path]) -> Path | None:
    return min(paths, key=lambda p: (len(p.parts), str(p))) if paths else None


# ---------------------------------------------------------------------------------------------- geodesy

def _geodetic(x: float, y: float, z: float) -> tuple[float, float, float]:
    """ECEF metres -> (lat, lng, height above the WGS84 ellipsoid)."""
    from pyproj import Transformer
    lng, lat, h = Transformer.from_crs("EPSG:4978", "EPSG:4979", always_xy=True).transform(x, y, z)
    return lat, lng, h


def _up(lat: float, lng: float) -> tuple[float, float, float]:
    la, lo = math.radians(lat), math.radians(lng)
    return math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)


def _offset(lat: float, lng: float, east: float, north: float) -> tuple[float, float]:
    """A point east/north metres from lat/lng (local approximation; fine within a few km)."""
    return lat + north / 111_320.0, lng + east / (111_320.0 * math.cos(math.radians(lat)))


def terra_georef(meta: Path, centre: tuple[float, float, float]) -> dict:
    """DJI Terra (and ContextCapture-style) metadata.xml: <SRS> + <SRSOrigin>. `centre` is the model's
    centre in the OBJ's own coordinates. Returns {lat, lng, heading, scale, label}."""
    try:
        doc = ET.parse(meta).getroot()
    except ET.ParseError as e:
        raise BuildError(f"metadata.xml can't be read: {e}") from None
    srs = (doc.findtext("SRS") or "").strip()
    origin = [float(v) for v in (doc.findtext("SRSOrigin") or "0,0,0").replace(" ", "").split(",")[:3]]
    origin += [0.0] * (3 - len(origin))
    x, y = origin[0] + centre[0], origin[1] + centre[1]
    if srs.upper().startswith("ENU:"):
        lat0, lng0 = (float(v) for v in srs[4:].split(",")[:2])
        lat, lng = _offset(lat0, lng0, x, y)
        return {"lat": lat, "lng": lng, "heading": 0.0, "scale": 1.0, "label": f"Terra metadata.xml ({srs})"}
    from pyproj import CRS, Transformer
    from pyproj.exceptions import CRSError
    try:
        crs = CRS.from_user_input(srs)
    except CRSError:
        raise BuildError(f"metadata.xml names a coordinate system this portal doesn't know: {srs!r}") from None
    horizontal = crs.sub_crs_list[0] if crs.is_compound else crs
    if not horizontal.is_projected:
        raise BuildError(f"metadata.xml uses {srs}, which isn't a projected (metres/feet) system. "
                         "Export with a UTM or local ENU coordinate system")
    to_geo = Transformer.from_crs(horizontal, "EPSG:4326", always_xy=True)
    lng, lat = to_geo.transform(x, y)
    lng_n, lat_n = to_geo.transform(x, y + 100.0)
    # Grid north vs true north (meridian convergence): turn the model so its grid-north points there.
    heading = math.degrees(math.atan2((lng_n - lng) * math.cos(math.radians(lat)), lat_n - lat)) % 360
    unit = horizontal.axis_info[0].unit_conversion_factor or 1.0       # e.g. US survey feet -> metres
    return {"lat": lat, "lng": lng, "heading": round(heading, 3), "scale": unit, "label": f"Terra metadata.xml ({srs})"}


# ---------------------------------------------------------------------------------------------- 3D Tiles

def tiles_anchor(tileset: Path) -> dict | None:
    """Where an uploaded 3D Tiles set sits on Earth: {lat, lng, h (floor height)}, or None if it's in
    local coordinates (placed by hand)."""
    import numpy as np
    try:
        root = json.loads(tileset.read_text())["root"]
    except (ValueError, KeyError) as e:
        raise BuildError(f"{tileset.name} isn't a valid 3D Tiles file ({e})") from None
    bv = root.get("boundingVolume", {})
    if "region" in bv:
        w, s, e, n, lo, _hi = bv["region"][:6]
        return {"lat": math.degrees((s + n) / 2), "lng": math.degrees((w + e) / 2), "h": lo}
    m = np.array(root.get("transform") or np.eye(4).flatten(order="F").tolist(), dtype=float).reshape(4, 4, order="F")
    if "box" in bv:
        b = bv["box"]
        centre, axes = np.array(b[:3]), [np.array(b[3:6]), np.array(b[6:9]), np.array(b[9:12])]
    elif "sphere" in bv:
        centre, axes = np.array(bv["sphere"][:3]), [np.array([0, 0, bv["sphere"][3]])]
    else:
        return None
    p = m @ np.append(centre, 1.0)
    if np.linalg.norm(p[:3]) < 6.0e6:
        return None                                          # local coordinates, not on the Earth
    lat, lng, h = _geodetic(*p[:3])
    up = np.array(_up(lat, lng))
    half = sum(abs(float(np.dot(m[:3, :3] @ a, up))) for a in axes)
    return {"lat": lat, "lng": lng, "h": h - half}


# ---------------------------------------------------------------------------------------------- meshes

def _image_of(material):
    for attr in ("image", "baseColorTexture"):
        img = getattr(material, attr, None)
        if img is not None:
            return attr, img
    return None, None


def _fit_texture(mesh) -> bool:
    """Scale down huge textures and store opaque ones as JPEG. Returns True if the mesh is textured."""
    from PIL import Image
    import trimesh
    visual = getattr(mesh, "visual", None)
    if getattr(visual, "kind", None) != "texture" or visual.material is None:
        return False
    attr, img = _image_of(visual.material)
    uv = getattr(visual, "uv", None)
    if img is None or uv is None or len(uv) != len(mesh.vertices):
        # A texture without matching texture coordinates makes an invalid glTF (Cesium's shaders fail on it).
        mesh.visual = trimesh.visual.ColorVisuals(mesh, face_colors=[180, 180, 180, 255])
        return False
    big = max(img.size) > MAX_TEXTURE
    opaque = img.mode in ("RGB", "L", "P") or (img.mode == "RGBA" and img.getextrema()[3][0] == 255)
    if big or (opaque and img.format != "JPEG"):
        if big:
            k = MAX_TEXTURE / max(img.size)
            img = img.resize((max(1, round(img.size[0] * k)), max(1, round(img.size[1] * k))), Image.LANCZOS)
        if opaque:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=85)
            buf.seek(0)
            img = Image.open(buf)
            img.load()
        setattr(visual.material, attr, img)
    return True


# Source axes -> east/north/up. glTF is always Y-up; Terra/CAD exports (and PLY/STL) are usually Z-up.
_TO_ENU = {"y": ((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)),
           "z": ((1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1))}
_ENU_TO_GLTF = ((1, 0, 0, 0), (0, 0, 1, 0), (0, -1, 0, 0), (0, 0, 0, 1))      # (e, n, u) -> (e, u, -n)


def _meshes(path: Path):
    import trimesh
    try:
        # Not maintain_order: photogrammetry OBJ faces use separate position / texture indices (seams), and
        # trimesh only splits vertices at those seams (keeping the UVs) when it may re-order them.
        loaded = trimesh.load(str(path), process=False)
    except Exception as e:  # noqa: BLE001 - any parser error is the file's problem
        raise BuildError(f"{path.name} couldn't be read: {e}") from None
    if isinstance(loaded, trimesh.Scene):
        geoms = loaded.dump(concatenate=False) if hasattr(loaded, "dump") else list(loaded.geometry.values())
    else:
        geoms = [loaded]
    meshes = [g for g in geoms if isinstance(g, trimesh.Trimesh) and len(g.faces)]
    if not meshes:
        if any(isinstance(g, trimesh.PointCloud) for g in geoms):
            raise BuildError(f"{path.name} is a point cloud. Export a textured mesh (OBJ or 3D Tiles) instead")
        raise BuildError(f"{path.name} has no surfaces in it")
    return meshes


def _unlit(glb: bytes) -> bytes:
    """Mark every material KHR_materials_unlit: photogrammetry has the light baked into its photos,
    so the 3D view's sun would only darken it."""
    import struct
    jlen = struct.unpack_from("<I", glb, 12)[0]
    doc = json.loads(glb[20:20 + jlen])
    for mat in doc.get("materials", []):
        mat.setdefault("extensions", {})["KHR_materials_unlit"] = {}
    if doc.get("materials"):
        used = doc.setdefault("extensionsUsed", [])
        if "KHR_materials_unlit" not in used:
            used.append("KHR_materials_unlit")
    js = json.dumps(doc, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    rest = glb[20 + jlen:]
    out = struct.pack("<I", len(js)) + b"JSON" + js + rest
    return b"glTF" + struct.pack("<II", 2, 12 + len(out)) + out



# ---------------------------------------------------------------------------------------------- big inputs
# Memory stays bounded whatever the model's size: textures are scaled down one at a time before any mesh is
# loaded (JPEGs decoded straight at the smaller size), and big OBJ files are streamed into pieces of at most
# FACES_PER_PIECE faces, each converted on its own.

def shrink_textures(job: Path, src: Path) -> int:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None                       # admins' uploads; the process is memory-capped anyway
    images = [p for p in src.rglob("*") if p.suffix.lower() in IMAGE_EXTS and p.is_file()]
    done = 0
    for n, p in enumerate(images, 1):
        try:
            with Image.open(p) as im:
                if max(im.size) <= MAX_TEXTURE:
                    continue
                _status(job, f"Scaling down textures ({n} of {len(images)})", 3)
                fmt = im.format
                if fmt == "JPEG":
                    im.draft("RGB", (MAX_TEXTURE, MAX_TEXTURE))     # decode at 1/2, 1/4 or 1/8 size directly
                im.thumbnail((MAX_TEXTURE, MAX_TEXTURE), Image.LANCZOS)
                small = im.convert("RGB") if fmt == "JPEG" or im.mode not in ("RGB", "RGBA", "L") else im.copy()
            if fmt == "JPEG":
                small.save(p, "JPEG", quality=90)
            else:
                small.save(p, fmt or "PNG")
            done += 1
        except (OSError, ValueError):
            continue                                    # unreadable images: the mesh loader reports it if it matters
    return done


def _idx(token: bytes, count: int) -> int:
    i = int(token)
    return i - 1 if i > 0 else count + i                # OBJ indices are 1-based; negative = from the end


def split_obj(job: Path, path: Path) -> list[Path]:
    """Stream a big OBJ into pieces of at most FACES_PER_PIECE faces (one material each), written next to it so
    its .mtl and textures still resolve. The original is removed. Returns the piece files."""
    from array import array
    import numpy as np
    v, vt, vn = array("d"), array("d"), array("d")
    mtllibs: list[bytes] = []
    size = path.stat().st_size
    _status(job, f"Reading {path.name} ({size / 1024**2:,.0f} MB)", 4)
    with open(path, "rb") as f:                          # pass 1: every position / texture / normal
        for line in f:
            if line[:2] == b"v ":
                p = line.split()
                v.extend((float(p[1]), float(p[2]), float(p[3])))
            elif line[:3] == b"vt ":
                p = line.split()
                vt.extend((float(p[1]), float(p[2]) if len(p) > 2 else 0.0))
            elif line[:3] == b"vn ":
                p = line.split()
                vn.extend((float(p[1]), float(p[2]), float(p[3])))
            elif line.startswith(b"mtllib"):
                mtllibs.append(line.rstrip())
    V = np.frombuffer(v, dtype=np.float64).reshape(-1, 3)
    T = np.frombuffer(vt, dtype=np.float64).reshape(-1, 2) if len(vt) else None
    N = np.frombuffer(vn, dtype=np.float64).reshape(-1, 3) if len(vn) else None
    pieces: list[Path] = []
    stem = path.stem

    def flush(material: bytes, faces: list) -> None:
        if not faces:
            return
        vi = sorted({c[0] for f in faces for c in f})
        ti = sorted({c[1] for f in faces for c in f if c[1] is not None})
        ni = sorted({c[2] for f in faces for c in f if c[2] is not None})
        vm = {o: i + 1 for i, o in enumerate(vi)}
        tm = {o: i + 1 for i, o in enumerate(ti)}
        nm = {o: i + 1 for i, o in enumerate(ni)}
        out = path.with_name(f"{stem}__piece{len(pieces) + 1:04d}.obj")
        with open(out, "wb") as w:
            w.write(b"\n".join(mtllibs) + b"\n")
            np.savetxt(w, V[vi], fmt="v %.6f %.6f %.6f")
            if ti:
                np.savetxt(w, T[ti], fmt="vt %.6f %.6f")
            if ni:
                np.savetxt(w, N[ni], fmt="vn %.5f %.5f %.5f")
            if material:
                w.write(b"usemtl " + material + b"\n")
            lines = []
            for f in faces:
                lines.append(b"f " + b" ".join(
                    b"%d/%s/%s" % (vm[a], b"%d" % tm[t] if t is not None else b"", b"%d" % nm[n] if n is not None else b"")
                    for a, t, n in f))
            w.write(b"\n".join(lines) + b"\n")
        pieces.append(out)

    groups: dict[bytes, list] = {}                       # material -> faces still to write
    material, cv, ct, cn, read = b"", 0, 0, 0, 0
    with open(path, "rb") as f:                          # pass 2: faces, by material, in pieces
        for line in f:
            read += len(line)
            head = line[:2]
            if head == b"v ":
                cv += 1
            elif head == b"vt":
                ct += 1
            elif head == b"vn":
                cn += 1
            elif head == b"f ":
                corners = []
                for tok in line.split()[1:]:
                    parts = tok.split(b"/")
                    corners.append((_idx(parts[0], cv),
                                    _idx(parts[1], ct) if len(parts) > 1 and parts[1] else None,
                                    _idx(parts[2], cn) if len(parts) > 2 and parts[2] else None))
                for k in range(1, len(corners) - 1):    # polygons -> triangles
                    groups.setdefault(material, []).append((corners[0], corners[k], corners[k + 1]))
                if len(groups[material]) >= FACES_PER_PIECE:
                    flush(material, groups.pop(material))
                    _status(job, f"Cutting {path.name} into pieces ({read * 100 // size}%)", 4)
            elif line.startswith(b"usemtl"):
                material = line.split(maxsplit=1)[1].strip() if len(line.split()) > 1 else b""
    for m, faces in groups.items():
        flush(m, faces)
    del V, T, N, v, vt, vn
    path.unlink()
    return pieces


def build_meshes(job: Path, src: Path, out: Path, up: str) -> dict:
    import numpy as np
    import trimesh
    shrink_textures(job, src)
    for big in [p for p in src.rglob("*") if p.suffix.lower() == ".obj" and p.is_file() and p.stat().st_size > SPLIT_OBJ_BYTES]:
        split_obj(job, big)
    files = sorted(p for p in src.rglob("*") if p.suffix.lower() in MESH_EXTS and p.is_file())
    if not files:
        raise BuildError("No 3D model found. Upload 3D Tiles (a folder with tileset.json), OBJ, glTF/GLB, PLY or STL")
    if any(p.suffix.lower() == ".gltf" for p in files) and any(p.suffix.lower() == ".glb" for p in files):
        files = [p for p in files if p.suffix.lower() != ".gltf"]
    meta = _shallowest([p for p in src.rglob("*") if p.name.lower() == "metadata.xml"])
    if up == "auto":
        up = "z" if meta or all(p.suffix.lower() in (".ply", ".stl") for p in files) else "y"
    if up not in _TO_ENU:
        raise BuildError("Up axis must be auto, y or z")
    to_enu, to_gltf = np.array(_TO_ENU[up], float), np.array(_ENU_TO_GLTF, float)
    out.mkdir(parents=True, exist_ok=True)
    pieces, samples, faces, textured_any = [], [], 0, False
    rng = np.random.default_rng(0)
    for n, path in enumerate(files, 1):
        _status(job, f"Converting {path.name} ({n} of {len(files)})", 5 + int(85 * (n - 1) / len(files)))
        meshes = _meshes(path)
        textured = [_fit_texture(m) for m in meshes]
        if path.suffix.lower() in (".glb", ".gltf"):
            enu = to_enu if up == "z" else np.array(_TO_ENU["y"], float)    # glTF files are Y-up by definition
        else:
            enu = to_enu
        for m in meshes:
            m.apply_transform(enu)
        lo = np.min([m.bounds[0] for m in meshes], axis=0)
        hi = np.max([m.bounds[1] for m in meshes], axis=0)
        centre = (lo + hi) / 2
        shift = np.eye(4)
        shift[:3, 3] = -centre
        for m in meshes:
            v = m.vertices
            samples.append(v[rng.choice(len(v), min(len(v), SAMPLE_PER_PIECE), replace=False), 2])
            m.apply_transform(to_gltf @ shift)
            faces += len(m.faces)
        lit = not all(textured)
        if lit:
            for m in meshes:
                _ = m.vertex_normals                        # computed now so the export includes them
        glb = trimesh.Scene(meshes).export(file_type="glb")
        if not lit and path.suffix.lower() not in (".glb", ".gltf"):
            glb = _unlit(glb)
        name = f"p{n:04d}.glb"
        (out / name).write_bytes(glb)
        pieces.append({"uri": name, "centre": centre.tolist(), "half": ((hi - lo) / 2).tolist(), "bytes": len(glb)})
        textured_any |= any(textured)
        del meshes
    _status(job, "Writing the tileset", 92)
    lo = np.min([np.array(p["centre"]) - p["half"] for p in pieces], axis=0)
    hi = np.max([np.array(p["centre"]) + p["half"] for p in pieces], axis=0)
    floor = float(np.percentile(np.concatenate(samples), 2))    # the ground under the model, ignoring stray points below
    centre = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, floor])

    def box(c, h):
        return [*map(float, c), float(h[0]), 0, 0, 0, float(h[1]), 0, 0, 0, float(h[2])]

    children = []
    for p in pieces:
        t = np.array(p["centre"]) - centre
        children.append({"transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, float(t[0]), float(t[1]), float(t[2]), 1],
                         "boundingVolume": {"box": box((0, 0, 0), p["half"])}, "geometricError": 0,
                         "content": {"uri": p["uri"]}})
    size = float(np.linalg.norm(hi - lo))
    tileset = {"asset": {"version": "1.1", "generator": "TWG alarm portal"}, "geometricError": max(size, 50.0),
               "root": {"boundingVolume": {"box": box((hi + lo) / 2 - centre, (hi - lo) / 2)},
                        "geometricError": max(size, 50.0), "refine": "ADD", "children": children}}
    (out / "tileset.json").write_text(json.dumps(tileset, separators=(",", ":")))
    result = {"kind": "mesh", "pieces": len(pieces), "faces": faces, "up": up, "textured": textured_any,
              "size_m": [round(float(v), 1) for v in hi - lo], "bytes": sum(p["bytes"] for p in pieces),
              "tileset": f"{out.name}/tileset.json", "georef": None}
    if meta is not None:
        g = terra_georef(meta, tuple(centre.tolist()))
        result["georef"] = {"lat": g["lat"], "lng": g["lng"], "heading": g["heading"], "scale": g["scale"], "label": g["label"]}
    return result


def build(job: Path, up: str = "auto", name: str = "") -> dict:
    src, out = job / "src", job / "tiles"
    upload = next((p for p in job.glob("upload.*") if p.is_file() and p.suffix != ".part"), None)
    if upload is not None:
        _status(job, "Unpacking", 2)
        unpack(upload, src, name)
    if not src.is_dir():
        raise BuildError("The uploaded file is missing")
    tileset = _shallowest(list(src.rglob("tileset.json")))
    if tileset is not None:
        _status(job, "Reading the 3D Tiles set", 50)
        anchor = tiles_anchor(tileset)
        total = sum(p.stat().st_size for p in tileset.parent.rglob("*") if p.is_file())
        return {"kind": "tiles", "tileset": tileset.relative_to(job).as_posix(), "anchor": anchor,
                "georef": {"label": "3D Tiles position"} if anchor else None, "bytes": total, "pieces": None, "faces": None}
    shutil.rmtree(out, ignore_errors=True)
    return build_meshes(job, src, out, up)


def main(job_dir: str) -> int:
    job = Path(job_dir)
    opts = json.loads((job / "job.json").read_text()) if (job / "job.json").exists() else {}
    try:
        result = {"ok": True, **build(job, opts.get("up", "auto"), opts.get("name", ""))}
    except BuildError as e:
        result = {"ok": False, "error": str(e)}
    except MemoryError:
        result = {"ok": False, "error": "The model is too big to convert on this server. Export it tiled "
                                        "(DJI Terra: OBJ tiles or B3DM) or with lower detail"}
    except Exception as e:  # noqa: BLE001 - report anything else rather than dying silently
        result = {"ok": False, "error": f"Conversion failed: {type(e).__name__}: {e}"}
    (job / "result.json").write_text(json.dumps(result))
    _status(job, "Done" if result["ok"] else "Failed", 100)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    sys.exit(main(sys.argv[1]))
