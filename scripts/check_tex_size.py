"""Read the real dimensions of every image EMBEDDED in the GLB (not from logs).

PNG: width/height from the IHDR.  JPEG: scans the SOF markers.
"""
import json, struct, sys, collections

path = sys.argv[1] if len(sys.argv) > 1 else r"D:\projects\UE2Godot\out\L_Overview.glb"
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 1024   # expected --tex value
raw = open(path, "rb").read()

off, js, binc = 12, None, b""
while off < len(raw):
    clen, ctype = struct.unpack_from("<II", raw, off)
    chunk = raw[off + 8: off + 8 + clen]
    if ctype == 0x4E4F534A:
        js = json.loads(chunk.decode("utf-8"))
    elif ctype == 0x004E4942:
        binc = chunk
    off += 8 + clen + ((4 - clen % 4) % 4)


def png_size(b):
    return struct.unpack(">II", b[16:24])


def jpeg_size(b):
    i = 2
    while i < len(b) - 9:
        if b[i] != 0xFF:
            i += 1
            continue
        m = b[i + 1]
        if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        seg = struct.unpack(">H", b[i + 2:i + 4])[0]
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", b[i + 5:i + 9])
            return w, h
        i += 2 + seg
    return (0, 0)


bvs = js["bufferViews"]
sizes = collections.Counter()
fmts = collections.Counter()
largest = []
for i, im in enumerate(js.get("images", [])):
    bv = bvs[im["bufferView"]]
    o = bv.get("byteOffset", 0)
    blob = binc[o: o + bv["byteLength"]]
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = png_size(blob)
        fmts["PNG"] += 1
    else:
        w, h = jpeg_size(blob)
        fmts["JPEG"] += 1
    sizes["%dx%d" % (w, h)] += 1
    largest.append((max(w, h), w, h, im.get("name", "img%d" % i), len(blob)))

print("images in the GLB: %d   formats: %s"
      % (len(js.get("images", [])), dict(fmts)))
print("resolutions found:")
for s, n in sizes.most_common():
    print("   %-12s %d" % (s, n))
largest.sort(reverse=True)
big = [m for m in largest if m[0] > LIMIT]
print("above %d px: %d" % (LIMIT, len(big)))
for m in big[:10]:
    print("   %dx%d  %s" % (m[1], m[2], m[3]))
print("largest side present: %d px" % (largest[0][0] if largest else 0))
print("5 biggest files:")
for m in sorted(largest, key=lambda x: -x[4])[:5]:
    print("   %-46s %dx%d  %.2f MB" % (m[3][:46], m[1], m[2], m[4] / 1048576.0))
