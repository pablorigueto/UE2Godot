"""
Final validation: compares the generated GLB against the glTF that UE 5.8 ITSELF
exported from the same level, node by node. Pure Python (no dependencies).

  python compare_to_ue.py <mine.glb> <reference.gltf>
"""
import json
import struct
import sys
import math


def load_gltf(path):
    if path.lower().endswith(".glb"):
        with open(path, "rb") as f:
            data = f.read()
        magic, ver, total = struct.unpack_from("<III", data, 0)
        assert magic == 0x46546C67, "not a GLB"
        off, js = 12, None
        while off < total:
            clen, ctype = struct.unpack_from("<II", data, off)
            chunk = data[off + 8: off + 8 + clen]
            if ctype == 0x4E4F534A:
                js = json.loads(chunk.decode("utf-8"))
                break
            off += 8 + clen + ((4 - clen % 4) % 4)
        return js
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def mat_of(node):
    """Local 4x4 matrix (row-major) of a glTF node."""
    if "matrix" in node:                       # glTF: column-major
        m = node["matrix"]
        return [[m[0], m[4], m[8], m[12]],
                [m[1], m[5], m[9], m[13]],
                [m[2], m[6], m[10], m[14]],
                [m[3], m[7], m[11], m[15]]]
    t = node.get("translation", [0, 0, 0])
    r = node.get("rotation", [0, 0, 0, 1])     # x,y,z,w
    s = node.get("scale", [1, 1, 1])
    x, y, z, w = r
    rot = [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
           [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
           [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]
    out = [[rot[i][j] * s[j] for j in range(3)] + [t[i]] for i in range(3)]
    return out + [[0, 0, 0, 1]]


def mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)]
            for i in range(4)]


def world_transforms(g):
    """name -> (world matrix, mesh index)."""
    nodes = g.get("nodes", [])
    out = {}

    def walk(i, parent):
        n = nodes[i]
        m = mul(parent, mat_of(n))
        name = n.get("name", "node%d" % i)
        out.setdefault(name, []).append((m, n.get("mesh")))
        for c in n.get("children", []):
            walk(c, m)

    ident = [[1 if i == j else 0 for j in range(4)] for i in range(4)]
    for sc in g.get("scenes", []):
        for r in sc.get("nodes", []):
            walk(r, ident)
    return out


def key(name):
    """'Actor.Component' and 'Actor' collapse to the same key."""
    return name.split(".")[0].strip().lower()


def main():
    mine = load_gltf(sys.argv[1])
    ref = load_gltf(sys.argv[2])

    wm, wr = world_transforms(mine), world_transforms(ref)
    km = {}
    for n, lst in wm.items():
        km.setdefault(key(n), []).extend(lst)
    kr = {}
    for n, lst in wr.items():
        kr.setdefault(key(n), []).extend(lst)

    common = sorted(set(km) & set(kr))
    print("nodes mine=%d  ref=%d  comparable=%d" % (len(km), len(kr), len(common)))
    only_ref = sorted(set(kr) - set(km))
    if only_ref:
        print("only in the reference (%d): %s"
              % (len(only_ref), ", ".join(only_ref[:12])))

    errs = []
    worst = []
    for k in common:
        a = km[k][0][0]
        b = kr[k][0][0]
        d = math.sqrt(sum((a[i][3] - b[i][3]) ** 2 for i in range(3)))
        errs.append(d)
        worst.append((d, k, [round(a[i][3], 3) for i in range(3)],
                      [round(b[i][3], 3) for i in range(3)]))
    if not errs:
        print("!! no comparable nodes")
        return
    errs.sort()
    worst.sort(reverse=True)
    print("position error (meters):  mean=%.6f  median=%.6f  max=%.6f"
          % (sum(errs) / len(errs), errs[len(errs) // 2], errs[-1]))
    print("worst:")
    for d, k, a, b in worst[:8]:
        print("   %-34s d=%.5f  mine=%s  ue=%s" % (k, d, a, b))

    ok = sum(1 for e in errs if e < 1e-3)
    print("nodes with error < 1mm: %d/%d (%.1f%%)"
          % (ok, len(errs), 100.0 * ok / len(errs)))

    print("meshes: mine=%d ref=%d | materials: mine=%d ref=%d"
          % (len(mine.get("meshes", [])), len(ref.get("meshes", [])),
             len(mine.get("materials", [])), len(ref.get("materials", []))))
    print("images in my GLB: %d | textures: %d"
          % (len(mine.get("images", [])), len(mine.get("textures", []))))


main()
