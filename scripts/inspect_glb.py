"""Inspect the materials written inside the GLB."""
import json, struct, sys, collections

path = sys.argv[1]
with open(path, "rb") as f:
    data = f.read()
off = 12
js = None
while js is None:
    clen, ctype = struct.unpack_from("<II", data, off)
    if ctype == 0x4E4F534A:
        js = json.loads(data[off + 8: off + 8 + clen].decode("utf-8"))
    off += 8 + clen + ((4 - clen % 4) % 4)

imgs = js.get("images", [])
texs = js.get("textures", [])
mats = js.get("materials", [])
print("images=%d textures=%d materials=%d meshes=%d nodes=%d"
      % (len(imgs), len(texs), len(mats), len(js.get("meshes", [])), len(js.get("nodes", []))))

mime = collections.Counter(i.get("mimeType") for i in imgs)
print("image formats:", dict(mime))

stats = collections.Counter()
no_base = []
for m in mats:
    pbr = m.get("pbrMetallicRoughness", {})
    has_bct = "baseColorTexture" in pbr
    has_mrt = "metallicRoughnessTexture" in pbr
    has_n = "normalTexture" in m
    has_occ = "occlusionTexture" in m
    has_em = "emissiveTexture" in m or m.get("emissiveFactor", [0, 0, 0]) != [0, 0, 0]
    stats["baseColorTexture"] += has_bct
    stats["metallicRoughnessTexture"] += has_mrt
    stats["normalTexture"] += has_n
    stats["occlusionTexture"] += has_occ
    stats["emissive"] += bool(has_em)
    stats["alphaMode:" + m.get("alphaMode", "OPAQUE")] += 1
    stats["doubleSided"] += bool(m.get("doubleSided"))
    if not has_bct:
        no_base.append((m.get("name"), pbr.get("baseColorFactor")))
print("coverage per material:")
for k, v in sorted(stats.items()):
    print("   %-28s %d / %d" % (k, v, len(mats)))
if no_base:
    print("WITHOUT baseColorTexture (%d):" % len(no_base))
    for n, f in no_base[:20]:
        print("   %-40s baseColorFactor=%s" % (n, f))

print("\nexamples:")
for m in mats[:6]:
    pbr = m.get("pbrMetallicRoughness", {})
    print("  %-34s bcf=%s mf=%s rf=%s bct=%s mrt=%s n=%s occ=%s" % (
        m.get("name"),
        [round(v, 3) for v in pbr.get("baseColorFactor", [1, 1, 1, 1])],
        pbr.get("metallicFactor"), pbr.get("roughnessFactor"),
        "baseColorTexture" in pbr, "metallicRoughnessTexture" in pbr,
        "normalTexture" in m, "occlusionTexture" in m))
