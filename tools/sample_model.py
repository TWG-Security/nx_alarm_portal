"""A synthetic DJI Terra-style OBJ export for tests: two tiles (a 20 x 12 x 6 m textured "building" and a
40 x 40 m ground slab) in UTM 18N with a metadata.xml, zipped like Terra's output folder.

    .venv/bin/python -m tools.sample_model out.zip [lat lng]

The building's long side runs east-west and its texture has a red NORTH face, so a wrong axis or
rotation is easy to spot. No real imagery is used.
"""
import io
import sys
import zipfile

from PIL import Image, ImageDraw

DEV_SITE = (40.2665, -76.8838)      # the fake NX site tools/e2e adds


def _box(x0, y0, z0, x1, y1, z1):
    """8 corners and 12 triangles of an axis-aligned box (Z up), with simple UVs per face."""
    v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    quads = [(0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7), (4, 5, 6, 7)]   # S, E, N, W, roof
    return v, quads


def _obj(name, verts, quads, tex_rows):
    """quads get UV rows from the texture atlas (one horizontal band per face)."""
    lines = [f"mtllib {name}.mtl"] + [f"v {x:.3f} {y:.3f} {z:.3f}" for x, y, z in verts]
    vt = []
    for i, _ in enumerate(quads):
        r0, r1 = 1 - (tex_rows[i] + 1) / 5, 1 - tex_rows[i] / 5
        vt += [(0, r0), (1, r0), (1, r1), (0, r1)]
    lines += [f"vt {u:.3f} {w:.3f}" for u, w in vt] + ["usemtl m0"]
    for i, (a, b, c, d) in enumerate(quads):
        t = 4 * i + 1
        lines += [f"f {a+1}/{t} {b+1}/{t+1} {c+1}/{t+2}", f"f {a+1}/{t} {c+1}/{t+2} {d+1}/{t+3}"]
    return "\n".join(lines) + "\n"


def make(lat=DEV_SITE[0], lng=DEV_SITE[1], ground_h=110.0) -> bytes:
    from pyproj import Transformer
    e, n = Transformer.from_crs("EPSG:4326", "EPSG:32618", always_xy=True).transform(lng, lat)
    origin = (round(e), round(n), 0)                     # Terra puts a round origin near the model
    dx, dy = e - origin[0], n - origin[1]
    atlas = Image.new("RGB", (256, 320), (150, 150, 150))
    d = ImageDraw.Draw(atlas)
    for row, colour in enumerate([(90, 90, 200), (90, 160, 90), (210, 40, 40), (200, 200, 60), (60, 60, 60)]):
        d.rectangle([0, row * 64, 255, row * 64 + 63], fill=colour)                         # S E N W roof
    d.text((100, 2 * 64 + 25), "NORTH", fill=(255, 255, 255))
    buf = io.BytesIO()
    atlas.save(buf, "JPEG", quality=90)
    meta = (f'<?xml version="1.0" encoding="utf-8"?>\n<ModelMetadata version="1">\n\t<SRS>EPSG:32618</SRS>\n'
            f"\t<SRSOrigin>{origin[0]},{origin[1]},{origin[2]}</SRSOrigin>\n\t<Texture><ColorSource>Visible</ColorSource></Texture>\n"
            "</ModelMetadata>\n")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("TWG_Office/metadata.xml", meta)
        v, q = _box(dx - 10, dy - 6, ground_h, dx + 10, dy + 6, ground_h + 6)
        tiles = {"Tile_+000_+000": (v, q, [0, 1, 2, 3, 4])}
        gv, gq = _box(dx - 20, dy - 20, ground_h - 0.5, dx + 20, dy + 20, ground_h)
        tiles["Tile_+001_+000"] = (gv, gq[4:], [4])
        for t, (verts, quads, rows) in tiles.items():
            z.writestr(f"TWG_Office/Data/{t}/{t}.obj", _obj(t, verts, quads, rows))
            z.writestr(f"TWG_Office/Data/{t}/{t}.mtl", f"newmtl m0\nKa 1 1 1\nKd 1 1 1\nmap_Kd {t}.jpg\n")
            z.writestr(f"TWG_Office/Data/{t}/{t}.jpg", buf.getvalue())
    return out.getvalue()


if __name__ == "__main__":
    args = sys.argv[1:]
    lat, lng = (float(args[1]), float(args[2])) if len(args) >= 3 else DEV_SITE
    open(args[0], "wb").write(make(lat, lng))
    print(f"wrote {args[0]}")
