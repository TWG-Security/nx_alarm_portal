"""Level of detail for textured site models (photogrammetry), used by model_build.

A drone model is millions of faces with gigabytes of texture. Drawn whole, the 3D view stutters, and the
browser keeps throwing pieces out of its cache and loading them again as the camera moves. So the model
becomes a quadtree of tiles:
  - a leaf holds the full mesh of its patch of ground, with a texture made of only the texture patches
    ("charts") its faces use, so no tile carries a whole source texture;
  - a parent holds a simplified copy of its children's faces (meshoptimizer, keeping the charts intact so the
    texture still fits) with a smaller texture, down to the root, which covers the whole model;
  - each tile's geometric error is the size of its texels and of its simplification error, so Cesium draws the
    root from afar and swaps in finer tiles (REPLACE) only where the camera gets close.
Tile borders are locked during simplification, so neighbouring tiles always meet, whatever level each is at.

Memory stays bounded: the textures are painted into the tile atlases in passes of at most ATLAS_BATCH bytes,
each opening every source texture once.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import struct
from pathlib import Path

LEAF_FACES = 100_000         # faces in a leaf tile at most
LEAF_ATLAS = 2048            # texture side of a leaf tile at most (bigger needs are split, then scaled down)
MIN_TEXEL = 0.01             # metres: sharper source textures are scaled down to 1 cm per pixel (plenty up close)
NODE_ATLAS = 2048            # texture side of a simplified tile at most
MAX_DEPTH = 7
PAD = 2                      # texture pixels kept around each chart, so mipmaps don't bleed in neighbours...
PAD_COARSE = 1               # ...1 in simplified tiles, where most charts are only a few pixels
TINY = 4.0                   # a chart smaller than this (pixels in its tile) is painted as one flat 2x2 colour
TEXEL_GE = 16                # geometric error = texel size x this: at Cesium's screen-space error of 16 px a tile is
                             # refined once one of its texels would cover more than a screen pixel...
SHAPE_GE = 4                 # ...or once its simplification error would cover more than 4 screen pixels
ATLAS_BATCH = 384 * 1024**2  # atlas bytes painted per pass over the source textures
UV_SLACK = 0.01              # UVs this far outside 0-1 are allowed (rounding); beyond that, textures repeat: no LOD


def _jpeg_bytes(img) -> bytes | None:
    """The original JPEG file behind a not-yet-decoded PIL image (trimesh opens textures lazily)."""
    fp = getattr(img, "fp", None)
    if img.format != "JPEG" or fp is None or not hasattr(fp, "getvalue"):
        return None
    try:
        return fp.getvalue()
    except ValueError:
        return None


def _save_texture(img, dest: Path, max_side: int) -> tuple[int, int]:
    raw = _jpeg_bytes(img)
    if raw is not None and max(img.size) <= max_side:
        dest.write_bytes(raw)
        return img.size
    from PIL import Image
    im = img.convert("RGB")
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    im.save(dest, "JPEG", quality=90)
    return im.size


class Textured:
    """Every mesh of a model in one set of arrays: positions (east/north/up, metres), texture coordinates,
    the texture of each vertex, and faces indexing them."""

    def __init__(self):
        self.parts: list[tuple] = []              # (V, UV, texture id, F) per mesh, until join()
        self.tex: list[Path] = []
        self.size: list[tuple[int, int]] = []
        self._keys: dict = {}

    def add(self, mesh, tex_dir: Path, max_side: int) -> bool:
        """Add one trimesh mesh (already east/north/up). False if it can't take part (no texture, no UVs,
        repeating UVs): then the whole model is converted without levels of detail."""
        import numpy as np
        from app.services.model_build import _image_of
        visual = getattr(mesh, "visual", None)
        if getattr(visual, "kind", None) != "texture" or visual.material is None:
            return False
        _, img = _image_of(visual.material)
        uv = getattr(visual, "uv", None)
        if img is None or uv is None or len(uv) != len(mesh.vertices) or not len(uv):
            return False
        uv = np.asarray(uv, dtype=np.float64)
        if uv.min() < -UV_SLACK or uv.max() > 1 + UV_SLACK:
            return False
        raw = _jpeg_bytes(img)
        key = hashlib.sha1(raw).hexdigest() if raw is not None else id(img)
        if key not in self._keys:
            tex_dir.mkdir(parents=True, exist_ok=True)
            path = tex_dir / f"t{len(self.tex):04d}.jpg"
            self.size.append(_save_texture(img, path, max_side))
            self.tex.append(path)
            self._keys[key] = len(self.tex) - 1
        t = self._keys[key]
        self.parts.append((np.asarray(mesh.vertices, dtype=np.float64), np.clip(uv, 0.0, 1.0),
                           t, np.asarray(mesh.faces, dtype=np.int64)))
        return True

    def join(self) -> None:
        import numpy as np
        off, faces = 0, []
        for V, _, _, F in self.parts:
            faces.append(F + off)
            off += len(V)
        self.V = np.concatenate([p[0] for p in self.parts])
        self.UV = np.concatenate([p[1] for p in self.parts])
        self.T = np.concatenate([np.full(len(p[0]), p[2], dtype=np.int32) for p in self.parts])
        self.F = np.concatenate(faces)
        self.parts = []
        size = np.array(self.size, dtype=np.float64)
        # Source texture pixel coordinates of every vertex (image rows go down; OBJ/trimesh v goes up).
        self.PX = np.c_[self.UV[:, 0] * size[self.T, 0], (1.0 - self.UV[:, 1]) * size[self.T, 1]]
        del self.UV


# ---------------------------------------------------------------------------------------------- tree

class Node:
    __slots__ = ("id", "depth", "ids", "children", "faces", "err", "scale", "atlas", "plan", "uv", "verts",
                 "lo", "hi", "texel", "ge", "bytes")

    def __init__(self, depth: int, ids):
        self.depth, self.ids, self.children = depth, ids, []
        self.faces = self.plan = self.uv = self.verts = None
        self.err = self.texel = self.ge = 0.0
        self.scale = 1.0
        self.bytes = 0


def _areas(m: Textured, F):
    """3D area (m²) and source-texture area (px²) of each face."""
    import numpy as np
    p = m.V[F]
    a3 = 0.5 * np.linalg.norm(np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), axis=1)
    t = m.PX[F]
    d1, d2 = t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]
    apx = 0.5 * np.abs(d1[:, 0] * d2[:, 1] - d1[:, 1] * d2[:, 0])
    return a3, apx


def build_tree(m: Textured) -> Node:
    """Split the faces (by centre, at the medians) until each leaf has at most LEAF_FACES faces and texture that
    fits in LEAF_ATLAS."""
    import numpy as np
    cen = m.V[m.F][:, :, :2].mean(axis=1)
    a3, apx = _areas(m, m.F)
    apx = np.minimum(apx, a3 / MIN_TEXEL**2)    # texture pixels each face will get
    budget = LEAF_ATLAS * LEAF_ATLAS * 0.4      # charts' boxes and padding take about twice their faces' area

    def split(ids, depth):
        node = Node(depth, ids)
        if depth >= MAX_DEPTH or (len(ids) <= LEAF_FACES and apx[ids].sum() <= budget):
            return node
        mx, my = np.median(cen[ids], axis=0)
        east, north = cen[ids, 0] >= mx, cen[ids, 1] >= my
        parts = [ids[east & north], ids[east & ~north], ids[~east & north], ids[~east & ~north]]
        parts = [p for p in parts if len(p)]
        if len(parts) > 1:
            node.children = [split(p, depth + 1) for p in parts]
        return node

    return split(np.arange(len(m.F)), 0)


def walk(node: Node):
    """Children before parents."""
    for c in node.children:
        yield from walk(c)
    yield node


def _simplify(m: Textured, F, target: int, max_err: float):
    import meshoptimizer as mo
    import numpy as np
    used, inv = np.unique(F.ravel(), return_inverse=True)
    pos = np.ascontiguousarray(m.V[used], dtype=np.float32)
    idx = np.ascontiguousarray(inv, dtype=np.uint32)
    dst = np.empty_like(idx)
    err = np.zeros(1, dtype=np.float32)
    n = mo.simplify(dst, idx, pos, target_index_count=max(3, target * 3), target_error=max_err,
                    options=mo.SIMPLIFY_LOCK_BORDER | mo.SIMPLIFY_ERROR_ABSOLUTE, result_error=err)
    return used[dst[:n]].reshape(-1, 3), float(err[0])


def simplify_tree(m: Textured, root: Node, status=lambda pct: None) -> None:
    """Leaves keep every face; each parent is its children's faces, simplified to about a quarter, as far as
    its texture's resolution can show (the error allowed is a few of its texels)."""
    import numpy as np
    nodes = list(walk(root))
    for k, node in enumerate(nodes):
        if not node.children:
            node.faces = m.F[node.ids]
            continue
        faces = np.concatenate([c.faces for c in node.children])
        a3, _ = _areas(m, faces)
        texel = math.sqrt(a3.sum() / (NODE_ATLAS * NODE_ATLAS * 0.5))
        node.faces, node.err = _simplify(m, faces, len(faces) // 4, SHAPE_GE * texel)
        node.err = max(node.err, *(c.err for c in node.children))
        status(int(100 * (k + 1) / len(nodes)))


# ---------------------------------------------------------------------------------------------- atlases

def _charts(F, n):
    """Connected groups of faces in texture space (they share texture coordinates, not just positions)."""
    import numpy as np
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    a = np.concatenate([F[:, 0], F[:, 1]])
    b = np.concatenate([F[:, 1], F[:, 2]])
    _, label = connected_components(coo_matrix((np.ones(len(a), dtype=np.int8), (a, b)), shape=(n, n)), directed=False)
    return label


def _pack(w, h, side: int):
    """Shelf-pack rectangles (largest first) into a strip at most `side` wide. Returns (x, y, width, height)."""
    import numpy as np
    order = np.lexsort((-w, -h))
    area = float((w * h).sum())
    width = int(min(side, max(int(w.max()), math.ceil(math.sqrt(area * 1.08)))))
    width += -width % 4
    x = np.zeros(len(w), dtype=np.int64)
    y = np.zeros(len(w), dtype=np.int64)
    cx = cy = shelf = 0
    for i in order:
        if cx + w[i] > width:
            cy, cx, shelf = cy + shelf, 0, 0
        x[i], y[i] = cx, cy
        cx += w[i]
        shelf = max(shelf, h[i])
    return x, y, width, cy + shelf


def plan_atlas(m: Textured, node: Node, side: int, max_scale: float) -> None:
    """Lay out the node's texture: each chart's piece of its source texture, scaled by node.scale, packed into
    one image; tiny charts become a flat colour. Sets node.verts, node.uv (texture coordinates in the atlas),
    node.plan (what to paint) and node.atlas (width, height)."""
    import numpy as np
    used, inv = np.unique(node.faces.ravel(), return_inverse=True)
    node.verts, local = used, inv.reshape(-1, 3)
    label = _charts(local, len(used))
    k = int(label.max()) + 1
    px, tex = m.PX[used], m.T[used]
    lo = np.full((k, 2), np.inf)
    hi = np.full((k, 2), -np.inf)
    np.minimum.at(lo, label, px)
    np.maximum.at(hi, label, px)
    ctex = np.zeros(k, dtype=np.int32)
    ctex[label] = tex
    ext = np.maximum(hi - lo, 1.0)
    size = np.array(m.size, dtype=np.float64)[ctex]
    a3, apx = _areas(m, node.faces)

    s = min(max_scale, math.sqrt(side * side * 0.8 / float(((ext[:, 0] + 1) * (ext[:, 1] + 1)).sum())))
    for _ in range(30):
        pad = PAD if s >= 0.5 else PAD_COARSE
        big = ext.max(axis=1) * s >= TINY
        x0 = np.clip(np.floor(lo - pad / s), 0, size)
        x1 = np.clip(np.ceil(hi + pad / s), 0, size)
        x1 = np.maximum(x1, x0 + 1)
        dw = np.maximum(1, np.ceil((x1 - x0) * s)).astype(np.int64)
        tiny = np.flatnonzero(~big)
        rects = np.flatnonzero(big)
        if len(rects):
            rx, ry, width, height = _pack(dw[rects, 0], dw[rects, 1], side)
        else:
            rx = ry = np.zeros(0, dtype=np.int64)
            width, height = 4, 0
        per_row = width // 2
        pal_y0 = height + (-height % 2)
        total = pal_y0 + 2 * math.ceil(len(tiny) / per_row) if len(tiny) else height
        total = max(4, total + (-total % 4))
        if total <= side:
            break
        s *= 0.92 * math.sqrt(side / total)
    else:
        raise RuntimeError("texture atlas doesn't fit")
    node.scale, node.atlas = s, (int(width), int(total))

    # Where each chart lands, then every vertex's texture coordinate in the atlas.
    dest = np.zeros((k, 2))
    factor = np.zeros((k, 2))
    dest[rects] = np.c_[rx, ry]
    factor[rects] = dw[rects] / (x1[rects] - x0[rects])
    pal = np.zeros((len(tiny), 2), dtype=np.int64)
    if len(tiny):
        j = np.arange(len(tiny))
        pal = np.c_[2 * (j % per_row), pal_y0 + 2 * (j // per_row)]
    uvpx = np.zeros((len(used), 2))
    is_rect = np.zeros(k, dtype=bool)
    is_rect[rects] = True
    rv = is_rect[label]
    lab = label[rv]
    uvpx[rv] = dest[lab] + (px[rv] - x0[lab]) * factor[lab]
    if len(tiny):
        slot = np.full(k, -1, dtype=np.int64)
        slot[tiny] = np.arange(len(tiny))
        tv = ~rv
        uvpx[tv] = pal[slot[label[tv]]] + 1.0          # the centre of the 2x2 block
    node.uv = uvpx / np.array(node.atlas, dtype=np.float64)
    node.plan = {"tex": ctex, "rects": rects, "x0": x0, "x1": x1, "dw": dw, "dest": dest.astype(np.int64),
                 "tiny": tiny, "centre": (lo + hi) / 2, "pal": pal}
    node.texel = math.sqrt(a3.sum() / max(apx.sum() * s * s, 1e-9))


def paint(m: Textured, nodes: list[Node], status=lambda done: None) -> dict[int, bytes]:
    """Paint the planned atlases, ATLAS_BATCH bytes at a time. Returns {node id: JPEG}."""
    from PIL import Image
    out: dict[int, bytes] = {}
    batches, cur, used = [], [], 0
    for n in sorted(nodes, key=lambda n: n.id):
        b = n.atlas[0] * n.atlas[1] * 3
        if cur and used + b > ATLAS_BATCH:
            batches.append(cur)
            cur, used = [], 0
        cur.append(n)
        used += b
    if cur:
        batches.append(cur)
    for batch in batches:
        canvas = {n.id: Image.new("RGB", n.atlas, (128, 128, 128)) for n in batch}
        for t, path in enumerate(m.tex):
            todo = [n for n in batch if (n.plan["tex"] == t).any()]
            if not todo:
                continue
            with Image.open(path) as f:
                im = f.convert("RGB")
            small = im.reduce(4) if min(im.size) >= 16 else im
            sk = im.size[0] / small.size[0]
            for n in todo:
                p, c = n.plan, canvas[n.id]
                for i in p["rects"][p["tex"][p["rects"]] == t]:
                    box = tuple(int(v) for v in (*p["x0"][i], *p["x1"][i]))
                    w, h = int(p["dw"][i, 0]), int(p["dw"][i, 1])
                    piece = im.crop(box) if (w, h) == (box[2] - box[0], box[3] - box[1]) else \
                        im.resize((w, h), Image.BILINEAR, box=box)
                    c.paste(piece, tuple(int(v) for v in p["dest"][i]))
                tiny = p["tiny"]
                if len(tiny):
                    sel = p["tex"][tiny] == t
                    for j, (cx, cy) in zip(tiny[sel].tolist(), p["pal"][sel].tolist()):
                        x, y = p["centre"][j]
                        colour = small.getpixel((min(small.size[0] - 1, int(x / sk)), min(small.size[1] - 1, int(y / sk))))
                        c.paste(colour, (cx, cy, cx + 2, cy + 2))
            del im, small
        for n in batch:
            buf = io.BytesIO()
            canvas.pop(n.id).save(buf, "JPEG", quality=85)
            out[n.id] = buf.getvalue()
            status(len(out))
    return out


# ---------------------------------------------------------------------------------------------- output

def glb(pos, uv, idx, jpeg: bytes) -> bytes:
    """One textured, unlit triangle mesh as a GLB. pos: glTF (Y-up) metres; uv: atlas coordinates."""
    import numpy as np
    pos = np.ascontiguousarray(pos, dtype=np.float32)
    uv = np.ascontiguousarray(uv, dtype=np.float32)
    small = len(pos) < 65536
    idx = np.ascontiguousarray(idx.ravel(), dtype=np.uint16 if small else np.uint32)
    chunks, views, off = [], [], 0
    for data, target in ((pos.tobytes(), 34962), (uv.tobytes(), 34962), (idx.tobytes(), 34963), (jpeg, None)):
        view = {"buffer": 0, "byteOffset": off, "byteLength": len(data)}
        if target:
            view["target"] = target
        views.append(view)
        data += b"\0" * (-len(data) % 4)
        chunks.append(data)
        off += len(data)
    doc = {
        "asset": {"version": "2.0", "generator": "TWG alarm portal"},
        "extensionsUsed": ["KHR_materials_unlit"],
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "TEXCOORD_0": 1}, "indices": 2, "material": 0}]}],
        "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}, "metallicFactor": 0, "roughnessFactor": 1},
                       "extensions": {"KHR_materials_unlit": {}}}],
        "textures": [{"source": 0, "sampler": 0}],
        "samplers": [{"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071}],
        "images": [{"bufferView": 3, "mimeType": "image/jpeg"}],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(pos), "type": "VEC3",
             "min": pos.min(axis=0).tolist(), "max": pos.max(axis=0).tolist()},
            {"bufferView": 1, "componentType": 5126, "count": len(uv), "type": "VEC2"},
            {"bufferView": 2, "componentType": 5123 if small else 5125, "count": len(idx), "type": "SCALAR"}],
        "bufferViews": views,
        "buffers": [{"byteLength": off}],
    }
    js = json.dumps(doc, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    body = b"".join(chunks)
    out = struct.pack("<I", len(js)) + b"JSON" + js + struct.pack("<I", len(body)) + b"BIN\0" + body
    return b"glTF" + struct.pack("<II", 2, 12 + len(out)) + out


def build(job: Path, m: Textured, out: Path, status) -> dict:
    """Tree, simplification, atlases, GLBs and tileset.json in `out`. m.V must already be in the tileset's frame
    (east/north/up metres around the model's floor). Returns {tiles, levels, bytes, faces}."""
    import numpy as np
    status("Dividing the model into tiles", 40)
    root = build_tree(m)
    nodes = list(walk(root))
    for i, n in enumerate(nodes):
        n.id = i
    status(f"Simplifying {len(nodes)} tiles", 42)
    simplify_tree(m, root, lambda pct: status(f"Simplifying {len(nodes)} tiles", 42 + pct * 18 // 100))
    for n in nodes:                                       # children first, so a parent knows their scale
        leaf = not n.children
        if leaf:
            a3, apx = _areas(m, n.faces)
            cap = min(1.0, math.sqrt(a3.sum() / max(apx.sum(), 1e-9)) / MIN_TEXEL)
        else:
            cap = min(1.0, 0.5 * max(c.scale for c in n.children))
        plan_atlas(m, n, LEAF_ATLAS if leaf else NODE_ATLAS, cap)
        p = m.V[n.verts]
        n.lo, n.hi = p.min(axis=0), p.max(axis=0)
        if n.children:
            n.lo = np.minimum(n.lo, np.min([c.lo for c in n.children], axis=0))
            n.hi = np.maximum(n.hi, np.max([c.hi for c in n.children], axis=0))
            n.ge = max(TEXEL_GE * n.texel, SHAPE_GE * n.err, *(1.05 * c.ge for c in n.children), 0.01)
    status("Painting tile textures", 62)
    images = paint(m, nodes, lambda done: status(f"Painting tile textures ({done} of {len(nodes)})", 62 + 26 * done // len(nodes)))
    out.mkdir(parents=True, exist_ok=True)
    enu_to_gltf = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], dtype=np.float64)
    total = 0
    for n in nodes:
        used = n.verts
        lookup = np.searchsorted(used, n.faces)
        data = glb(m.V[used] @ enu_to_gltf.T, n.uv, lookup, images.pop(n.id))
        (out / f"t{n.id:04d}.glb").write_bytes(data)
        n.bytes = len(data)
        total += len(data)

    def box(n):
        c, h = (n.lo + n.hi) / 2, np.maximum((n.hi - n.lo) / 2, 0.01)
        return [*map(float, c), float(h[0]), 0, 0, 0, float(h[1]), 0, 0, 0, float(h[2])]

    def tile(n):
        t = {"boundingVolume": {"box": box(n)}, "geometricError": round(n.ge, 4), "content": {"uri": f"t{n.id:04d}.glb"}}
        if n.children:
            t["refine"] = "REPLACE"
            t["children"] = [tile(c) for c in n.children]
        return t

    size = float(np.linalg.norm(root.hi - root.lo))
    tileset = {"asset": {"version": "1.1", "generator": "TWG alarm portal"},
               "geometricError": round(max(4 * root.ge, size, 50.0), 4), "root": tile(root)}
    (out / "tileset.json").write_text(json.dumps(tileset, separators=(",", ":")))
    return {"tiles": len(nodes), "levels": max(n.depth for n in nodes) + 1, "bytes": total,
            "root_faces": int(len(root.faces)), "texel_cm": [round(100 * min(n.texel for n in nodes if not n.children), 2),
                                                             round(100 * root.texel, 2)]}
