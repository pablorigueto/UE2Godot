"""
UE -> Godot converter, stage 2 (Blender side).

  blender --background --python blender_build.py -- --src <out> --out scene.glb --tex 1024

Reads the scene.json + FBX + textures produced by ue_export.py, rebuilds the
scene, reconstructs the materials as PBR (metallic/roughness) and exports a
single GLB ready for Godot.

Coordinate space
----------------
UE is Z-up / left-handed (X forward, Y right); Blender is Z-up / right-handed.
The change of basis was MEASURED (scripts/verify_axes.py), not assumed: comparing
the imported FBX against the glTF that UE 5.8 itself writes for the same mesh,
the error drops to exactly zero with

    v_fbx = diag(-1, 1, 1) . v_unreal      (UE's FBX exporter mirrors on X)

That alone would already give a correct scene, but yawed 180 degrees relative to
the glTF UE itself writes. Applying Rz(180) to the mesh makes the effective basis
diag(1,-1,1) (since Rz180 . diag(-1,1,1) = diag(1,-1,1)), so:

    mesh:      v_blender = Rz(180) . v_fbx
    transform: M_blender = S . M_unreal . S      with S = diag(1,-1,1)

Final result after the glTF exporter's Z-up -> Y-up step:

    gltf = (ue_x, ue_z, ue_y)

which is exactly the convention of UE's native glTF exporter -- checked
numerically in scripts/verify_axes.py and against out/ref_level/.

UE's FBX already arrives in METERS (the importer applies the file's unit scale),
so only the translations coming from scene.json -- which are in cm -- need the
0.01 factor.
"""

import bpy
import bmesh  # noqa: F401
import json
import math
import os
import re
import sys
from mathutils import Matrix, Quaternion, Vector

# ---------------------------------------------------------------- arguments -
argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def arg(flag, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default


SRC = arg("--src", r"D:\projects\UE2Godot\out")
OUT = arg("--out", os.path.join(SRC, "L_Overview.glb"))
TEXSIZE = int(arg("--tex", "1024"))
FLIP_GREEN = arg("--flip-green", "1") == "1"
# UE compensates exposure automatically (auto-exposure + SkyAtmosphere), glTF
# does not. We export UE's physical value as-is; use --sun-scale if you want a
# stronger sun in the target engine.
SUN_SCALE = float(arg("--sun-scale", "1.0"))
EMPTY_SLOT = arg("--empty-slot", "first")   # first | keep
UE_TO_M = 0.01

TEX_OUT = os.path.join(SRC, "textures_%dk" % max(1, TEXSIZE // 1024))
os.makedirs(TEX_OUT, exist_ok=True)

S = Matrix.Diagonal(Vector((1.0, -1.0, 1.0))).to_4x4()   # UE->Blender basis change
RZ180 = Matrix.Rotation(math.pi, 4, "Z")                 # aligns the FBX to it

# Fallbacks for the two things glTF cannot take from UE as-is. Both are used only
# when the material exposes no parameter of its own (see build_material /
# texture_landscape); they are an approximation, not a measurement.
WATER_COLOR = [float(v) for v in
               (arg("--water-color", "0.012,0.055,0.062")).split(",")]
WATER_ROUGHNESS = float(arg("--water-roughness", "0.04"))
WATER_ALPHA = float(arg("--water-alpha", "0.88"))
LAND_ROUGHNESS = float(arg("--land-roughness", "0.9"))
# Fallback when the painted weights are not available. Measured mean colours of
# this pack's five layers:
#   01 T_Dirt_01            [0.499 0.423 0.334]  brown
#   02 T_Dirt_01_With_Stones[0.258 0.243 0.240]  grey
#   03 T_Sand_01            [0.667 0.604 0.526]  sand
#   04 T_Dirt_With_Plants_01[0.423 0.384 0.136]  green
#   05 T_Dirt_02            [0.213 0.165 0.123]  dark brown
# Layer 02 covers the most ground but reads as flat grey, so 01 is the better
# single-layer stand-in for the brown/green/sand mix Unreal actually paints.
LAND_LAYER = int(arg("--land-layer", "1"))          # which MM_Landscape layer
LAND_UV_TILE = float(arg("--land-uv-tile", "0"))    # metres per repeat; 0 = param
# Tiles across the terrain for the baked layer blend. 4 -> sixteen 1K textures
# over 504 m, about 8 px/m. 0 falls back to a single layer.
LAND_TILES = int(arg("--land-tiles", "4"))

# BP_Sky_Sphere's mesh is 41 m across with scale 1 in the saved level -- the
# Blueprint's construction script is what blows it up to sky size, and that never
# runs in a commandlet. Exporting it therefore drops a 41 m ball of night sky in
# the middle of the island. Godot takes its sky from a WorldEnvironment anyway.
SKY = arg("--sky", "0") == "1"
# UE's ocean is a 12.5 km plane. Kept at that size it is 25x the island, so every
# viewer frames 12.5 km of water and the island is a speck. Clamped to a multiple
# of the island's own extent instead; 0 keeps Unreal's size.
WATER_FACTOR = float(arg("--water-extent", "1.2"))
# MM_Nature_Assets blends a second set of maps onto up-facing surfaces (moss on
# the rocks, sand on MI_Rock_01_A, dirt on MI_Rock_01_Wtih_Dirt). See
# build_top_material: the faces above this angle from horizontal get it.
# OFF by default. MM_Nature_Assets projects its maps by WORLD POSITION (triplanar),
# not by UV, and glTF addresses textures by UV only. Pinning the moss to the mesh's
# UVs magnifies it into flat green slabs -- visibly worse than leaving the rock
# plain. Reproducing this properly belongs in the destination engine, which does
# have triplanar. --top-layer 1 turns the approximation back on.
TOP_LAYER = arg("--top-layer", "0") == "1"
TOP_COS = math.cos(math.radians(float(arg("--top-layer-angle", "50"))))


def log(*a):
    print("[B2G]", *a)
    sys.stdout.flush()


with open(os.path.join(SRC, "scene.json"), "r", encoding="utf-8") as f:
    SCENE = json.load(f)

MESHES = SCENE["meshes"]
MATERIALS = SCENE["materials"]
TEXTURES = SCENE["textures"]
NODES = SCENE["nodes"]

# Actors handled by ue_bake_extras.py (landscape, sky sphere, single-layer water):
# their materials cannot be rebuilt from MIC parameters, so Unreal baked them to
# textures through its own glTF exporter and we merge that file instead. Both
# sides read the same list so nothing is exported twice.
EXTRAS_DIR = os.path.join(SRC, "extras")
EXTRAS_GLTF = os.path.join(EXTRAS_DIR, "extras.gltf")
EXTRA_ACTORS = {}
if arg("--extras", "1") == "1" and os.path.exists(os.path.join(EXTRAS_DIR, "extras.json")):
    with open(os.path.join(EXTRAS_DIR, "extras.json"), "r", encoding="utf-8") as f:
        EXTRA_ACTORS = json.load(f).get("actors", {})


# -------------------------------------------------------------------- scene -
def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.unit_settings.system = "METRIC"
    sc.unit_settings.scale_length = 1.0
    sc.render.engine = "BLENDER_EEVEE_NEXT" if hasattr(
        bpy.types, "RenderEngineEeveeNext") else sc.render.engine


def ue_matrix(x):
    """{'loc','quat','scale'} (UE space, cm) -> Blender world Matrix (m)."""
    q = x["quat"]
    m = (Matrix.Translation(Vector(x["loc"])) @
         Quaternion((q[3], q[0], q[1], q[2])).to_matrix().to_4x4() @
         Matrix.Diagonal(Vector(x["scale"])).to_4x4())
    m = S @ m @ S                      # conjugation (correct rotation/scale)
    m.translation = m.translation * UE_TO_M
    return m


# ----------------------------------------------------------------- textures -
_img_cache = {}
_tex_file = {}          # UE texture key -> file written under TEX_OUT


def overrides(meta, kind, name):
    """Did the material INSTANCE set this parameter, or is it the master's default?

    ue_export merges master defaults into the parameter tables so nothing is lost,
    and records them separately under 'master_defaults'. The difference matters:
    MM_Nature_Assets defaults 'Top Layer Texture' to T_Dirt_01_B and MM_Props
    defaults 'Emiss Texture' to T_Brick_Single_1_B, so reading the merged value
    alone puts a dirt cap on every metal prop and makes the boat glow.
    """
    cur = param(meta.get(kind, {}), name)
    if cur is None:
        return False
    base = (meta.get("master_defaults") or {}).get(kind)
    if base is None:
        return True                      # older scene.json: nothing to compare to
    return param(base, name) != cur


def param(d, name, default=None):
    """Look up a parameter ignoring spaces/case (MICs have keys with extra spaces)."""
    key = name.replace(" ", "").lower()
    for k, v in d.items():
        if k.replace(" ", "").lower() == key:
            return v
    return default


JPEG_QUALITY = int(arg("--jpeg-quality", "92"))


def get_image(tex_key, non_color, needs_alpha=False, rough_mul=None):
    """Resize to TEXSIZE and return the bpy.types.Image already in its final format.

    PNG for normal maps and anything needing alpha; JPEG for the rest -- without
    that split, 140 1K textures push the GLB past 300 MB.
    """
    if tex_key is None:
        return None
    ck = (tex_key, bool(needs_alpha), rough_mul)
    if ck in _img_cache:
        return _img_cache[ck]
    meta = TEXTURES.get(tex_key)
    if not meta or not meta.get("file"):
        _img_cache[ck] = None
        return None

    src = os.path.join(SRC, meta["file"].replace("/", os.sep))
    if not os.path.exists(src):
        log("!! missing texture:", src)
        _img_cache[ck] = None
        return None

    is_normal = meta["name"].endswith("_N")
    use_png = needs_alpha or is_normal
    # UE's 'Roughness Value' is a multiplier that can exceed 1, while glTF only
    # accepts roughnessFactor <= 1 -- so it is baked into the ORM's G channel
    # (a per-material variant) instead of becoming a node the exporter would drop.
    suffix = "" if not rough_mul else "_r%03d" % int(round(rough_mul * 100))
    dst = os.path.join(TEX_OUT, meta["name"] + suffix + (".png" if use_png else ".jpg"))

    if not os.path.exists(dst):
        img = bpy.data.images.load(src)
        img.colorspace_settings.name = "Non-Color" if non_color else "sRGB"
        w, h = img.size
        cov = _alpha_coverage(img, ALPHA_CUTOFF) if needs_alpha else None
        if max(w, h) > TEXSIZE:
            r = TEXSIZE / float(max(w, h))
            img.scale(max(1, int(round(w * r))), max(1, int(round(h * r))))
        if needs_alpha and cov is not None:
            _restore_alpha_coverage(img, cov)
        if is_normal:
            if FLIP_GREEN or meta.get("flip_green"):
                _flip_green(img)
            _rebuild_normal_z(img)
        if rough_mul:
            _scale_green(img, rough_mul)
        # CAREFUL: touching filepath_raw invalidates the buffer if anything after
        # it triggers a reload -- which is why it comes last, right before save.
        img.filepath_raw = dst
        img.file_format = "PNG" if use_png else "JPEG"
        try:
            img.save(quality=JPEG_QUALITY)
        except TypeError:
            img.save()
        bpy.data.images.remove(img)

    img = bpy.data.images.load(dst)
    img.name = meta["name"]
    img.colorspace_settings.name = "Non-Color" if non_color else "sRGB"
    _img_cache[ck] = img
    _tex_file.setdefault(tex_key, os.path.basename(dst))
    return img


def _flip_green(img):
    """G = 1-G  (UE DirectX normal -> glTF OpenGL). numpy: ~100x faster."""
    import numpy as np
    buf = np.empty(len(img.pixels), dtype=np.float32)
    img.pixels.foreach_get(buf)
    buf[1::4] = 1.0 - buf[1::4]
    img.pixels.foreach_set(buf)


ALPHA_CUTOFF = 0.333


def _alpha_coverage(img, cutoff):
    """Fraction of opaque pixels at the ORIGINAL resolution (before downscaling)."""
    import numpy as np
    buf = np.empty(len(img.pixels), dtype=np.float32)
    img.pixels.foreach_get(buf)
    return float((buf[3::4] >= cutoff).mean())


def _restore_alpha_coverage(img, target_cov):
    """Restore the mask in the alpha channel after downscaling.

    Shrinking 4K->1K explodes the amount of INTERMEDIATE alpha (on the fabric
    texture: 27% -> 41%). Since alphaMode=MASK is a hard cutoff, that grey band
    turns into stippled, ragged cutouts. Here we bisect for the threshold that
    reproduces the original coverage and binarize the alpha -- same silhouette
    area as the 4K source, with no stippling.
    """
    import numpy as np
    buf = np.empty(len(img.pixels), dtype=np.float32)
    img.pixels.foreach_get(buf)
    a = buf[3::4]
    lo, hi = 0.0, 1.0
    thr = ALPHA_CUTOFF
    for _ in range(24):                      # bisect on the threshold
        thr = 0.5 * (lo + hi)
        if float((a >= thr).mean()) > target_cov:
            lo = thr
        else:
            hi = thr
    buf[3::4] = np.where(a >= thr, 1.0, 0.0)
    img.pixels.foreach_set(buf)


def _rebuild_normal_z(img):
    """Rebuild the normal map's blue channel: B = sqrt(1 - x^2 - y^2).

    UE compresses normals as BC5 (storing only RG) and rebuilds Z in the shader,
    so the blue channel of the exported PNGs is not trustworthy -- several
    textures in this pack come out with a mean B of ~0.41, i.e. a NEGATIVE Z,
    which makes the normal point into the surface and produces white speckles
    scattered across it in Godot/glTF viewers. glTF requires a full RGB normal
    map, so we derive Z here exactly as UE does in UnpackNormalMap(). Also
    normalizes alpha.
    """
    import numpy as np
    buf = np.empty(len(img.pixels), dtype=np.float32)
    img.pixels.foreach_get(buf)
    nx = buf[0::4] * 2.0 - 1.0
    ny = buf[1::4] * 2.0 - 1.0
    nz = np.sqrt(np.clip(1.0 - nx * nx - ny * ny, 0.0, 1.0))
    buf[2::4] = nz * 0.5 + 0.5
    buf[3::4] = 1.0
    img.pixels.foreach_set(buf)


def _scale_green(img, mul):
    """G = clamp(G * mul)  -- bakes UE's 'Roughness Value' into the ORM."""
    import numpy as np
    buf = np.empty(len(img.pixels), dtype=np.float32)
    img.pixels.foreach_get(buf)
    buf[1::4] = np.clip(buf[1::4] * mul, 0.0, 1.0)
    img.pixels.foreach_set(buf)


# ---------------------------------------------------------------- materials -
_mat_cache = {}


_top_cache = {}
_mat_key = {}          # built material name -> UE material key


def build_top_material(mat_key):
    """The world-space TOP LAYER of MM_Nature_Assets, as its own material.

    The rock master blends a second set of maps onto up-facing surfaces --
    'Top Layer Texture' is T_Moss_01_B on the mossy rocks, T_Sand_01_B on the
    sandy ones, T_Dirt_01_B on the dirty ones. That blend is computed from the
    WORLD normal, which a glTF material cannot express: it has one base-colour
    texture and no access to world space.

    So the layer is kept as real texture rather than a tint: it becomes a second
    material, and apply_top_layer() puts the up-facing faces on it. The boundary
    is per-face instead of the smooth gradient Unreal draws -- that is the part
    that does not survive.
    """
    if mat_key in _top_cache:
        return _top_cache[mat_key]
    _top_cache[mat_key] = None
    meta = MATERIALS.get(mat_key)
    if not meta:
        return None
    texs = meta.get("textures", {})
    scal = meta.get("scalars", {})
    vecs = meta.get("vectors", {})
    base_key = param(texs, "Top Layer Texture")
    if not base_key or base_key == param(texs, "Base Texture"):
        return None
    # only where the INSTANCE asked for a top layer -- the master's default is
    # T_Dirt_01_B, which would otherwise cap metal, food and bark with dirt
    if not overrides(meta, "textures", "Top Layer Texture"):
        return None
    base_img = get_image(base_key, non_color=False)
    if base_img is None:
        return None

    mat = bpy.data.materials.new(meta["name"] + "__top")
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (700, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (350, 0)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    tn = _tex_node(nt, base_img, -600, 300)
    src = tn.outputs["Color"]
    tint = param(vecs, "Top Layer Texture Color")
    if tint and tuple(tint[:3]) != (1.0, 1.0, 1.0):
        mix = nt.nodes.new("ShaderNodeMix")
        mix.data_type = "RGBA"
        mix.blend_type = "MULTIPLY"
        mix.location = (-250, 300)
        mix.inputs["Factor"].default_value = 1.0
        nt.links.new(src, mix.inputs[6])
        mix.inputs[7].default_value = tuple(tint)
        src = mix.outputs[2]
    nt.links.new(src, bsdf.inputs["Base Color"])

    orm_img = get_image(param(texs, "ORM Texture"), non_color=True)
    if orm_img:
        on = _tex_node(nt, orm_img, -600, -100, non_color=True)
        sep = nt.nodes.new("ShaderNodeSeparateColor")
        sep.location = (-350, -100)
        nt.links.new(on.outputs["Color"], sep.inputs["Color"])
        nt.links.new(sep.outputs["Green"], bsdf.inputs["Roughness"])
        nt.links.new(sep.outputs["Blue"], bsdf.inputs["Metallic"])
        _hook_occlusion(nt, sep.outputs["Red"])
    else:
        bsdf.inputs["Metallic"].default_value = 0.0

    n_img = get_image(param(texs, "Top Layer Normal Texture"), non_color=True)
    if n_img:
        nn = _tex_node(nt, n_img, -600, -500, non_color=True)
        nmap = nt.nodes.new("ShaderNodeNormalMap")
        nmap.location = (-250, -500)
        nmap.inputs["Strength"].default_value = abs(float(
            param(scal, "Top Layer Normal Value", 1.0) or 1.0))
        nt.links.new(nn.outputs["Color"], nmap.inputs["Color"])
        nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    bsdf.inputs["Emission Strength"].default_value = 0.0
    mat.use_backface_culling = not bool(meta.get("two_sided"))

    _top_cache[mat_key] = mat
    log("top layer: %s -> %s" % (meta["name"].split("_Materials_")[-1],
                                 base_key.split("/")[-1]))
    return mat


def apply_top_layer(ob, world, instanced=False):
    """Move up-facing faces onto the material's top layer (moss / sand / dirt).

    Skipped for instanced foliage: the split depends on each instance's rotation,
    so the mesh can no longer be shared, and doing that for the 2,237 pebbles took
    the GLB from 214 MB to 1.4 GB for something nobody can see at that size.
    """
    if not TOP_LAYER or instanced:
        return False
    # Snapshot the assigned materials FIRST. An object's material_slots follow the
    # mesh's material list, so me.materials.clear() empties the slots too -- reading
    # them afterwards yields nothing, the list ends up holding only the top layer,
    # and every face on the island gets moss.
    cur = [ms.material for ms in ob.material_slots]
    tops = {}
    for si, m in enumerate(cur):
        key = _mat_key.get(m.name) if m else None
        top = build_top_material(key) if key else None
        if top:
            tops[si] = top
    if not tops:
        return False
    # the split depends on the object's own rotation, so the mesh can no longer
    # be shared between instances
    me = ob.data.copy()
    ob.data = me
    me.materials.clear()
    for m in cur:
        me.materials.append(m)
    idx = {}
    for si, top in tops.items():
        me.materials.append(top)
        idx[si] = len(me.materials) - 1
    for ms in ob.material_slots:
        ms.link = "DATA"
    rot = world.to_3x3()
    n_top = 0
    for poly in me.polygons:
        tgt = idx.get(poly.material_index)
        if tgt is None:
            continue
        n = rot @ poly.normal
        if n.length > 0 and (n.normalized()).z >= TOP_COS:
            poly.material_index = tgt
            n_top += 1
    return n_top > 0


def _slot_name(mat):
    """FBX slot material name, minus Blender's .001 de-duplication suffix."""
    if mat is None:
        return ""
    return re.sub(r"\.\d{3}$", "", mat.name).strip().lower()


def _tex_node(nt, img, x, y, non_color=False):
    n = nt.nodes.new("ShaderNodeTexImage")
    n.image = img
    n.location = (x, y)
    n.interpolation = "Smart"
    if non_color:
        n.image.colorspace_settings.name = "Non-Color"
    return n


def build_material(mat_key):
    if mat_key in _mat_cache:
        return _mat_cache[mat_key]
    meta = MATERIALS.get(mat_key)
    if meta is None:
        m = bpy.data.materials.new("MISSING")
        _mat_cache[mat_key] = m
        return m

    mat = bpy.data.materials.new(meta["name"])
    mat.use_nodes = True
    _mat_cache[mat_key] = mat
    nt = mat.node_tree
    nt.nodes.clear()

    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (700, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (350, 0)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    texs = meta.get("textures", {})
    scal = meta.get("scalars", {})
    vecs = meta.get("vectors", {})

    blend = (meta.get("blend_mode") or "").upper()
    shading = (meta.get("shading_model") or "").upper()
    needs_alpha = ("MASKED" in blend) or ("TRANSLUCENT" in blend)

    # ---- base color -------------------------------------------------------
    base_key = (param(texs, "Base Texture") or param(texs, "Base Color Texture"))
    tint = param(vecs, "Base Texture Color") or param(vecs, "Overall Color")
    base_img = get_image(base_key, non_color=False, needs_alpha=needs_alpha)
    base_socket = None
    if base_img:
        tn = _tex_node(nt, base_img, -600, 300)
        base_socket = tn.outputs["Color"]
        alpha_socket = tn.outputs["Alpha"]
    else:
        alpha_socket = None

    if tint:
        mix = nt.nodes.new("ShaderNodeMix")
        mix.data_type = "RGBA"
        mix.blend_type = "MULTIPLY"
        mix.location = (-250, 300)
        mix.inputs["Factor"].default_value = 1.0
        if base_socket:
            nt.links.new(base_socket, mix.inputs[6])
        else:
            mix.inputs[6].default_value = (1, 1, 1, 1)
        mix.inputs[7].default_value = tuple(tint)
        base_socket = mix.outputs[2]

    if base_socket:
        nt.links.new(base_socket, bsdf.inputs["Base Color"])
    elif tint:
        bsdf.inputs["Base Color"].default_value = tuple(tint)

    # ---- ORM  (R=AO, G=Roughness, B=Metallic) -----------------------------
    rval = param(scal, "Roughness Value")
    rmul = float(rval) if (rval is not None and abs(rval - 1.0) > 1e-4) else None
    orm_img = get_image(param(texs, "ORM Texture"), non_color=True, rough_mul=rmul)
    rough_socket = None
    if orm_img:
        on = _tex_node(nt, orm_img, -600, -100, non_color=True)
        sep = nt.nodes.new("ShaderNodeSeparateColor")
        sep.location = (-350, -100)
        nt.links.new(on.outputs["Color"], sep.inputs["Color"])
        rough_socket = sep.outputs["Green"]
        nt.links.new(sep.outputs["Blue"], bsdf.inputs["Metallic"])
        _hook_occlusion(nt, sep.outputs["Red"])

    if rough_socket is not None:
        nt.links.new(rough_socket, bsdf.inputs["Roughness"])
    elif rval is not None:
        bsdf.inputs["Roughness"].default_value = float(rval)

    # ---- normal -----------------------------------------------------------
    n_img = get_image(param(texs, "Normal Texture"), non_color=True)
    if n_img:
        nn = _tex_node(nt, n_img, -600, -500, non_color=True)
        nmap = nt.nodes.new("ShaderNodeNormalMap")
        nmap.location = (-250, -500)
        nmap.inputs["Strength"].default_value = float(
            param(scal, "Normal Value", 1.0) or 1.0)
        nt.links.new(nn.outputs["Color"], nmap.inputs["Color"])
        nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])

    # ---- emissive ---------------------------------------------------------
    # Only when the INSTANCE asks for it. MM_Props declares Emiss Texture =
    # T_Brick_Single_1_B with Emiss Value = 1.0 as its master defaults, so taking
    # the merged values at face value made every prop that never touches emission
    # -- boat, pier, barrel, the lighthouse woodwork -- glow orange brick.
    dflt = meta.get("master_defaults") or {}
    e_img = get_image(param(texs, "Emiss Texture"), non_color=False)
    e_col = param(vecs, "Emiss Color")
    e_val = param(scal, "Emiss Value")
    emissive = (overrides(meta, "textures", "Emiss Texture")
                or overrides(meta, "vectors", "Emiss Color")
                or overrides(meta, "scalars", "Emiss Value"))
    if not dflt:
        # no master defaults recorded: the master's own defaults are white at
        # value 1, so anything still at that is not an emitter
        emissive = ((e_val is not None and abs(e_val - 1.0) > 1e-4)
                    or (e_col is not None and tuple(e_col[:3]) != (1.0, 1.0, 1.0)))
    if not emissive:
        e_img = e_col = None
    if e_img or e_col:
        if e_img:
            en = _tex_node(nt, e_img, -600, -900)
            src = en.outputs["Color"]
            if e_col:
                mix = nt.nodes.new("ShaderNodeMix")
                mix.data_type = "RGBA"
                mix.blend_type = "MULTIPLY"
                mix.location = (-300, -900)
                mix.inputs["Factor"].default_value = 1.0
                nt.links.new(src, mix.inputs[6])
                mix.inputs[7].default_value = tuple(e_col)
                src = mix.outputs[2]
            nt.links.new(src, bsdf.inputs["Emission Color"])
        else:
            bsdf.inputs["Emission Color"].default_value = tuple(e_col)
        bsdf.inputs["Emission Strength"].default_value = float(
            e_val if e_val is not None else 1.0)
    else:
        bsdf.inputs["Emission Strength"].default_value = 0.0

    # ---- transparency -----------------------------------------------------
    opacity = param(scal, "Opacity Value")

    def set_blend(method, render_method):
        for attr, val in (("blend_method", method),
                          ("surface_render_method", render_method)):
            try:
                setattr(mat, attr, val)
            except Exception:
                pass

    if "MASKED" in blend:
        set_blend("CLIP", "DITHERED")
        cutoff = float(meta.get("opacity_mask_clip", 0.333))
        try:
            mat.alpha_threshold = cutoff
        except Exception:
            pass
        if alpha_socket:
            # Blender 5.1's glTF exporter ignores blend_method: it infers
            # alphaMode=MASK from the NODE GRAPH, looking for 'alpha > cutoff'
            # (detect_alpha_clip in exp/material/search_node_tree.py).
            gt = nt.nodes.new("ShaderNodeMath")
            gt.operation = "GREATER_THAN"
            gt.location = (-100, 480)
            gt.inputs[1].default_value = cutoff
            nt.links.new(alpha_socket, gt.inputs[0])
            nt.links.new(gt.outputs[0], bsdf.inputs["Alpha"])
    elif "TRANSLUCENT" in blend or "ADDITIVE" in blend:
        set_blend("BLEND", "BLENDED")
        if alpha_socket and opacity is None:
            nt.links.new(alpha_socket, bsdf.inputs["Alpha"])
        else:
            bsdf.inputs["Alpha"].default_value = float(
                opacity if opacity is not None else 0.5)

    # ---- single-layer water ------------------------------------------------
    # MSM_SINGLE_LAYER_WATER has no glTF equivalent, and Unreal's own exporter
    # gives up on it ("Unsupported shading model (SingleLayerWater) ... will
    # export as Default Lit"), handing back a flat 0.85 grey -- which on this
    # level is a 12.5 km plane of white filling the whole horizon. Rebuild it as
    # the closest thing glTF does express: a smooth transmissive surface, tinted
    # by the material's own colour parameters when it exposes any.
    if "SINGLE_LAYER_WATER" in shading:
        col = (param(vecs, "Water Color") or param(vecs, "Base Color")
               or param(vecs, "Color") or WATER_COLOR)
        for sock in ("Base Color", "Alpha"):
            for lnk in list(bsdf.inputs[sock].links):
                nt.links.remove(lnk)
        bsdf.inputs["Base Color"].default_value = tuple(col[:3]) + (1.0,)
        bsdf.inputs["Metallic"].default_value = 0.0
        bsdf.inputs["Roughness"].default_value = float(
            param(scal, "Roughness", WATER_ROUGHNESS) or WATER_ROUGHNESS)
        bsdf.inputs["Emission Strength"].default_value = 0.0
        try:
            bsdf.inputs["IOR"].default_value = 1.33
        except KeyError:
            pass
        set_blend("BLEND", "BLENDED")
        bsdf.inputs["Alpha"].default_value = WATER_ALPHA
        log("water: %s -> %s alpha=%.2f rough=%.2f"
            % (meta.get("name", "?").split("_")[-1], [round(v, 3) for v in col[:3]],
               WATER_ALPHA, bsdf.inputs["Roughness"].default_value))

    mat.use_backface_culling = not bool(meta.get("two_sided"))
    _mat_key[mat.name] = mat_key
    return mat


def _hook_occlusion(nt, red_socket):
    """Wire the ORM's R channel into the 'glTF Material Output' group -> occlusionTexture."""
    grp = bpy.data.node_groups.get("glTF Material Output")
    if grp is None:
        grp = bpy.data.node_groups.new("glTF Material Output", "ShaderNodeTree")
        if hasattr(grp, "interface"):
            grp.interface.new_socket("Occlusion", in_out="INPUT",
                                     socket_type="NodeSocketFloat")
        else:
            grp.inputs.new("NodeSocketFloat", "Occlusion")
        grp.nodes.new("NodeGroupInput")
    node = nt.nodes.new("ShaderNodeGroup")
    node.node_tree = grp
    node.location = (350, -700)
    try:
        nt.links.new(red_socket, node.inputs["Occlusion"])
    except Exception:
        pass


# ------------------------------------------------------------------- meshes -
_mesh_data = {}


def import_mesh(mesh_key):
    """Import the FBX and return its list of mesh-datas (in meters, Blender space)."""
    if mesh_key in _mesh_data:
        return _mesh_data[mesh_key]
    meta = MESHES.get(mesh_key)
    _mesh_data[mesh_key] = []
    if not meta or not meta.get("file"):
        return []
    path = os.path.join(SRC, meta["file"].replace("/", os.sep))
    if not os.path.exists(path):
        log("!! missing FBX:", path)
        return []

    before = set(bpy.data.objects)
    try:
        bpy.ops.import_scene.fbx(
            filepath=path, use_manual_orientation=True,
            axis_forward="-Y", axis_up="Z", global_scale=1.0,
            use_image_search=False, use_custom_normals=True,
            use_anim=False, ignore_leaf_bones=True)
    except Exception as e:
        log("!! error importing", path, e)
        return []
    new = [o for o in bpy.data.objects if o not in before]

    datas = []
    for o in new:
        if o.type != "MESH":
            bpy.data.objects.remove(o, do_unlink=True)
            continue
        # the FBX already arrives in meters; Rz(180) aligns the X mirror UE
        # applies in the FBX with the S = diag(1,-1,1) basis used for transforms
        me = o.data
        me.transform(RZ180 @ o.matrix_world)
        me.name = meta["name"]
        datas.append(me)
        bpy.data.objects.remove(o, do_unlink=True)

    _mesh_data[mesh_key] = datas
    return datas


# ------------------------------------------------------------------- lights -
def build_light(node, coll):
    kind = node["light"]
    if kind == "sky":
        return None
    btype = {"directional": "SUN", "point": "POINT",
             "spot": "SPOT", "rect": "AREA"}.get(kind, "POINT")
    ld = bpy.data.lights.new(node["name"], btype)
    col = node.get("color", [1, 1, 1])
    ld.color = col[:3]
    inten = float(node.get("intensity", 1.0)) * SUN_SCALE

    # Units (measured empirically): Blender 5.x's glTF exporter IGNORES
    # light.energy and uses the 'Strength' of the emission node --
    #     lux(SUN) = Strength * 683
    #     cd(point/spot) = Strength / (4*pi) * 683
    # UE already gives directional lights in lux and point/spot in candela, and
    # glTF uses the same units, so this is a direct pass-through.
    if btype == "SUN":
        strength = inten / 683.0
    else:
        strength = inten * 4.0 * math.pi / 683.0
        if node.get("range"):
            ld.use_custom_distance = True
            ld.cutoff_distance = node["range"] * UE_TO_M
    ld.energy = strength
    if getattr(ld, "node_tree", None):
        em = next((n for n in ld.node_tree.nodes if n.type == "EMISSION"), None)
        if em:
            em.inputs["Strength"].default_value = strength
            em.inputs["Color"].default_value = tuple(col[:3]) + (1.0,)
    if btype == "SPOT":
        ld.spot_size = math.radians(float(node.get("outer_cone", 44.0)) * 2.0)
        inner = float(node.get("inner_cone", 0.0))
        outer = max(1e-3, float(node.get("outer_cone", 44.0)))
        ld.spot_blend = max(0.0, 1.0 - inner / outer)
    ob = bpy.data.objects.new(node["name"], ld)
    coll.objects.link(ob)
    # UE aims lights along +X; Blender aims along -Z
    ob.matrix_world = ue_matrix(node["xform"]) @ Matrix.Rotation(
        math.radians(90.0), 4, "Y")
    return ob


# -------------------------------------------------------------------- extras -
def import_extras(coll):
    """Merge the glTF Unreal baked for landscape / sky / water.

    That file already uses glTF's own convention, which is exactly what this
    scene exports to, so importing it needs no change of basis: Blender's
    importer applies Y-up -> Z-up and the exporter applies the inverse.
    Its baked textures are resized to TEXSIZE like every other one.
    """
    if not EXTRA_ACTORS or not os.path.exists(EXTRAS_GLTF):
        return 0
    before_obj = set(bpy.data.objects)
    before_img = set(bpy.data.images)
    try:
        bpy.ops.import_scene.gltf(filepath=EXTRAS_GLTF)
    except Exception as e:
        log("!! cannot import extras:", e)
        return 0
    new = [o for o in bpy.data.objects if o not in before_obj]
    if not SKY:
        for o in [x for x in new if "skysphere" in x.name.lower()]:
            bpy.data.objects.remove(o, do_unlink=True)
            new.remove(o)
    # the glTF terrain is flat (see ue_bake_extras); the real one is the FBX
    for o in [x for x in new if x.name.lower().startswith("landscapecomponent")]:
        bpy.data.objects.remove(o, do_unlink=True)
        new.remove(o)
    for o in new:
        for c in list(o.users_collection):
            c.objects.unlink(o)
        coll.objects.link(o)

    n_res = 0
    for img in [i for i in bpy.data.images if i not in before_img]:
        w, h = img.size
        if max(w, h) > TEXSIZE:
            r = TEXSIZE / float(max(w, h))
            img.scale(max(1, int(round(w * r))), max(1, int(round(h * r))))
            n_res += 1
        # the images live next to extras.gltf; re-save the resized copy under
        # TEX_OUT so the exporter embeds the small one (same trick as get_image)
        dst = os.path.join(TEX_OUT, "extra_%s.png" % bpy.path.clean_name(img.name))
        img.filepath_raw = dst
        img.file_format = "PNG"
        try:
            img.save()
        except Exception as e:
            log("!! cannot save extra texture", img.name, e)
    log("extras: %d objects, %d meshes, %d images (%d resized) from %s"
        % (len(new), len({o.data.name for o in new if o.type == "MESH"}),
           len([i for i in bpy.data.images if i not in before_img]), n_res,
           ", ".join(sorted(set(EXTRA_ACTORS.values())))))
    new += import_landscape_fbx(coll)
    texture_landscape([o for o in new if o.type == "MESH"])
    return len([o for o in new if o.type == "MESH"])


def import_landscape_fbx(coll):
    """The terrain, with its heightfield, from UE's FBX level exporter.

    Same convention as every other mesh in this converter: UE's FBX arrives in
    metres and mirrored on X, so Rz(180) puts it in the Blender basis. Here the
    geometry is already in world space, so that is the whole transform -- there is
    no per-component matrix to apply on top.
    """
    path = os.path.join(EXTRAS_DIR, "landscape.fbx")
    if not os.path.exists(path):
        log("!! no landscape.fbx -- terrain will be missing")
        return []
    before = set(bpy.data.objects)
    try:
        bpy.ops.import_scene.fbx(
            filepath=path, use_manual_orientation=True,
            axis_forward="-Y", axis_up="Z", global_scale=1.0,
            use_image_search=False, use_custom_normals=True,
            use_anim=False, ignore_leaf_bones=True)
    except Exception as e:
        log("!! cannot import landscape.fbx:", e)
        return []
    # Read every world matrix BEFORE touching the hierarchy: the FBX comes with
    # a root empty, and deleting a parent re-computes its children's matrix_world
    # (it cost a silent 1 m drop in the terrain the first time round).
    fresh = [x for x in bpy.data.objects if x not in before]
    mats = {o.name: o.matrix_world.copy() for o in fresh}
    out = []
    for o in fresh:
        if o.type != "MESH":
            bpy.data.objects.remove(o, do_unlink=True)
            continue
        o.data.transform(RZ180 @ mats[o.name])
        o.parent = None
        o.matrix_world = Matrix.Identity(4)
        o.name = "LandscapeComponent_%s" % o.name
        for c in list(o.users_collection):
            c.objects.unlink(o)
        coll.objects.link(o)
        out.append(o)
    z = [(o.matrix_world @ Vector(c)).z for o in out for c in o.bound_box]
    log("landscape FBX: %d objects, %d faces, z %.1f..%.1f m"
        % (len(out), sum(len(o.data.polygons) for o in out),
           min(z) if z else 0, max(z) if z else 0))
    return out


def bake_landscape_tiles(land):
    """Blend MM_Landscape's five layers with the painted weights and bake them.

    glTF gives a material ONE base-colour texture, so the layer blend cannot be a
    material graph -- it has to be resolved into pixels. ue_bake_extras.py samples
    the real painted weight of every layer across the terrain (the CPU query, the
    only route that survives a commandlet), and here each texel is resolved as

        colour = sum(weight_i * layer_i(world * uv_scale_i)) / sum(weight_i)

    baked into a grid of tiles so no single texture exceeds the --tex budget.
    Returns [(material, tile_min_xy_metres, tile_size_metres)] or None.
    """
    import numpy as np
    meta_path = os.path.join(EXTRAS_DIR, "landscape_weights.json")
    key = SCENE.get("landscape_material")
    meta = MATERIALS.get(key) if key else None
    if not os.path.exists(meta_path) or not meta or LAND_TILES <= 0:
        return None
    with open(meta_path, "r", encoding="utf-8") as f:
        wm = json.load(f)
    n = int(wm["size"])
    origin = [v / 100.0 for v in wm["origin_ue_cm"]]      # metres, UE space
    span = float(wm["span_cm"]) / 100.0

    texs = meta.get("textures", {})
    scal = meta.get("scalars", {})
    layers = []
    for name in wm["layers"]:
        i = int(str(name).split("_")[-1])
        img = get_image(param(texs, "Layer %02d Base Texture" % i), non_color=False)
        path = os.path.join(EXTRAS_DIR, wm["files"][name])
        if img is None or not os.path.exists(path):
            continue
        w = np.fromfile(path, dtype=np.float32).reshape(n, n)
        px = np.empty(len(img.pixels), dtype=np.float32)
        img.pixels.foreach_get(px)
        px = px.reshape(img.size[1], img.size[0], 4)[:, :, :3]
        layers.append({
            "name": str(name), "w": w, "px": px,
            "uv": float(param(scal, "Layer %02d UV Scale" % i, 0.2) or 0.2),
        })
    if not layers:
        log("!! no landscape layers to bake")
        return None

    T, RES = LAND_TILES, min(TEXSIZE, 1024)
    tile_m = span / T
    # texel centres of one tile, in metres relative to the tile
    t = (np.arange(RES) + 0.5) * (tile_m / RES)
    out = []
    for tj in range(T):
        for ti in range(T):
            x0 = origin[0] + ti * tile_m
            y0 = origin[1] + tj * tile_m
            wx = (x0 + t)[None, :].repeat(RES, 0)        # world X, metres (UE)
            wy = (y0 + t)[:, None].repeat(RES, 1)        # world Y, metres (UE)

            # BILINEAR, not nearest. The weights are sampled every couple of
            # metres; picking the closest one turns each sample into a 16x16
            # block of texels at 8 px/m, and the terrain comes out visibly
            # squared. Interpolating dissolves the grid.
            fx = np.clip((wx - origin[0]) / span * n - 0.5, 0, n - 1.001)
            fy = np.clip((wy - origin[1]) / span * n - 0.5, 0, n - 1.001)
            i0 = fx.astype(np.int32)
            j0 = fy.astype(np.int32)
            tx = (fx - i0)[:, :, None]
            ty = (fy - j0)[:, :, None]
            i1 = np.minimum(i0 + 1, n - 1)
            j1 = np.minimum(j0 + 1, n - 1)

            acc = np.zeros((RES, RES, 3), dtype=np.float32)
            tot = np.zeros((RES, RES), dtype=np.float32)
            for L in layers:
                W = L["w"]
                w = ((W[j0, i0] * (1 - tx[:, :, 0]) + W[j0, i1] * tx[:, :, 0])
                     * (1 - ty[:, :, 0])
                     + (W[j1, i0] * (1 - tx[:, :, 0]) + W[j1, i1] * tx[:, :, 0])
                     * ty[:, :, 0])
                if not w.any():
                    continue
                h, wd = L["px"].shape[0], L["px"].shape[1]
                u = np.mod(wx * L["uv"], 1.0) * wd
                v = np.mod(wy * L["uv"], 1.0) * h
                su0 = u.astype(np.int32) % wd
                sv0 = v.astype(np.int32) % h
                su1 = (su0 + 1) % wd
                sv1 = (sv0 + 1) % h
                au = (u - np.floor(u))[:, :, None]
                av = (v - np.floor(v))[:, :, None]
                tex = ((L["px"][sv0, su0] * (1 - au) + L["px"][sv0, su1] * au)
                       * (1 - av)
                       + (L["px"][sv1, su0] * (1 - au) + L["px"][sv1, su1] * au)
                       * av)
                acc += tex * w[:, :, None]
                tot += w
            safe = np.maximum(tot, 1e-6)
            col = acc / safe[:, :, None]
            col[tot < 1e-4] = layers[0]["px"].mean(axis=(0, 1))

            img = bpy.data.images.new("landscape_%d_%d" % (ti, tj), RES, RES,
                                      alpha=False)
            buf = np.ones((RES, RES, 4), dtype=np.float32)
            buf[:, :, :3] = np.clip(col, 0.0, 1.0)
            img.pixels.foreach_set(buf.ravel())
            dst = os.path.join(TEX_OUT, "landscape_bake_%d_%d.jpg" % (ti, tj))
            img.filepath_raw = dst
            img.file_format = "JPEG"
            try:
                img.save(quality=JPEG_QUALITY)
            except TypeError:
                img.save()

            mat = bpy.data.materials.new("MI_Landscape_%d_%d" % (ti, tj))
            mat.use_nodes = True
            nt = mat.node_tree
            nt.nodes.clear()
            o = nt.nodes.new("ShaderNodeOutputMaterial")
            o.location = (600, 0)
            bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
            bsdf.location = (300, 0)
            nt.links.new(bsdf.outputs["BSDF"], o.inputs["Surface"])
            tn = _tex_node(nt, img, -300, 0)
            nt.links.new(tn.outputs["Color"], bsdf.inputs["Base Color"])
            bsdf.inputs["Metallic"].default_value = 0.0
            bsdf.inputs["Roughness"].default_value = LAND_ROUGHNESS
            bsdf.inputs["Emission Strength"].default_value = 0.0
            out.append((mat, (x0, y0), tile_m))

    log("landscape bake: %d tiles of %dpx over %.0f m (%.1f px/m), %d layers: %s"
        % (len(out), RES, span, RES / tile_m, len(layers),
           ", ".join("%s x%.2f" % (L["name"], L["uv"]) for L in layers)))
    return out


def texture_landscape(objs):
    """Texture the imported terrain with the landscape material's own layer.

    Unreal's glTF exporter writes MI_Landscape_01 as baseColorFactor [0,0,0], so
    the geometry arrives black. The material itself is reachable, though: it is an
    ordinary instance of MM_Landscape and ue_export.py exports it with every
    "Layer NN Base/Normal/ORM Texture" parameter resolved, so the terrain is
    rebuilt here from the real textures.

    What cannot be reproduced is the BLEND between the five layers: it is driven
    by per-component weightmaps, and LandscapeComponent.weightmap_textures is not
    exposed to Python. One layer is used for the whole terrain -- the same
    compromise the converter already makes for the MM_Nature_Assets materials.

    UVs are planar in world XY (a landscape is a heightfield, so a top-down
    projection is its natural parameterisation), tiled every LAND_UV_TILE metres.
    """
    land = [o for o in objs if o.name.lower().startswith("landscapecomponent")]
    if not land:
        return

    tiles = bake_landscape_tiles(land)
    if tiles:
        span = tiles[0][2] * LAND_TILES
        for ob in land:
            me = ob.data
            uv = me.uv_layers.get("BakeUV") or me.uv_layers.new(name="BakeUV")
            # glTF maps TEXCOORD_0 to the FIRST uv layer, not the active one, so
            # leaving "TextureUVs" (which runs 0..504) in place made the baked
            # tiles sample at 500x. Nothing else uses it here -- drop it.
            for other in [l for l in me.uv_layers if l.name != "BakeUV"]:
                me.uv_layers.remove(other)
            uv = me.uv_layers["BakeUV"]
            me.uv_layers.active = uv
            try:
                uv.active_render = True
            except AttributeError:
                pass
            # UE-space position per vertex: the build applies
            # blender = (x, -y, z) * 0.01, so back is (x, -y, z) * 100 -> metres
            pos = [(ob.matrix_world @ v.co) for v in me.vertices]
            ue = [(p.x, -p.y) for p in pos]
            me.materials.clear()
            for mat, _, _ in tiles:
                me.materials.append(mat)
            size = tiles[0][2]
            for poly in me.polygons:
                cx = sum(ue[v][0] for v in poly.vertices) / len(poly.vertices)
                cy = sum(ue[v][1] for v in poly.vertices) / len(poly.vertices)
                ti = min(LAND_TILES - 1,
                         max(0, int((cx - tiles[0][1][0]) / size)))
                tj = min(LAND_TILES - 1,
                         max(0, int((cy - tiles[0][1][1]) / size)))
                poly.material_index = tj * LAND_TILES + ti
            # each face's UV is relative to ITS OWN tile origin, so walk faces
            for poly in me.polygons:
                ox, oy = tiles[poly.material_index][1]
                for li in poly.loop_indices:
                    x, y = ue[me.loops[li].vertex_index]
                    uv.data[li].uv = ((x - ox) / size, (y - oy) / size)
        log("landscape: %d object(s) -> %d baked tiles" % (len(land), len(tiles)))
        return

    key = SCENE.get("landscape_material")
    meta = MATERIALS.get(key) if key else None
    if not meta:
        log("!! no landscape material in scene.json -- the terrain stays black")
        return

    texs = meta.get("textures", {})
    scal = meta.get("scalars", {})
    pre = "Layer %02d " % LAND_LAYER
    base_img = get_image(param(texs, pre + "Base Texture"), non_color=False)
    orm_img = get_image(param(texs, pre + "ORM Texture"), non_color=True)
    n_img = get_image(param(texs, pre + "Normal Texture"), non_color=True)
    if base_img is None:
        log("!! landscape layer %d has no base texture (have: %s)"
            % (LAND_LAYER, sorted(texs)[:8]))
        return

    mat = bpy.data.materials.new("MI_Landscape_Layer%02d" % LAND_LAYER)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (700, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (350, 0)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    tn = _tex_node(nt, base_img, -600, 300)
    nt.links.new(tn.outputs["Color"], bsdf.inputs["Base Color"])

    rval = param(scal, pre + "Roughness Value")
    if orm_img:
        on = _tex_node(nt, orm_img, -600, -100, non_color=True)
        sep = nt.nodes.new("ShaderNodeSeparateColor")
        sep.location = (-350, -100)
        nt.links.new(on.outputs["Color"], sep.inputs["Color"])
        nt.links.new(sep.outputs["Green"], bsdf.inputs["Roughness"])
        nt.links.new(sep.outputs["Blue"], bsdf.inputs["Metallic"])
        _hook_occlusion(nt, sep.outputs["Red"])
    else:
        bsdf.inputs["Roughness"].default_value = float(
            rval if rval is not None else LAND_ROUGHNESS)
        bsdf.inputs["Metallic"].default_value = 0.0
    if n_img:
        nn = _tex_node(nt, n_img, -600, -500, non_color=True)
        nmap = nt.nodes.new("ShaderNodeNormalMap")
        nmap.location = (-250, -500)
        # "Vaue" is Unreal's own typo in MM_Landscape's parameter names
        nmap.inputs["Strength"].default_value = float(
            param(scal, pre + "Normal Vaue", param(scal, pre + "Normal Value", 1.0))
            or 1.0)
        nt.links.new(nn.outputs["Color"], nmap.inputs["Color"])
        nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    bsdf.inputs["Emission Strength"].default_value = 0.0

    # "UV Scale" is not metres per repeat: MM_Landscape feeds LandscapeLayerCoords,
    # whose Mapping Scale multiplies coordinates counted in QUADS. So one repeat
    # spans quad_size / UV Scale metres, and the quad size is measured off the mesh
    # rather than assumed -- a landscape component has (quads+1)^2 vertices, so its
    # extent divided by that count gives the quad size directly.
    # UE's FBX ships the landscape with a "TextureUVs" set that runs 0..504 --
    # the landscape's own coordinates counted in QUADS, which is exactly what
    # MM_Landscape feeds LandscapeLayerCoords. Scaling that by the layer's
    # "UV Scale" reproduces the mapping instead of re-deriving one from world XY.
    uv_scale = float(param(scal, pre + "UV Scale") or 0) or 1.0
    src = None
    for ob in land:
        src = ob.data.uv_layers.get("TextureUVs")
        if src:
            break
    for ob in land:
        me = ob.data
        lay = me.uv_layers.get("TextureUVs")
        if lay is None:
            lay = me.uv_layers.active or me.uv_layers.new(name="UVMap")
        if LAND_UV_TILE > 0:
            # explicit override: metres per repeat, projected from world XY
            co = [ob.matrix_world @ v.co for v in me.vertices]
            for loop in me.loops:
                w = co[loop.vertex_index]
                lay.data[loop.index].uv = (w.x / LAND_UV_TILE, w.y / LAND_UV_TILE)
        else:
            for d in lay.data:
                d.uv = (d.uv[0] * uv_scale, d.uv[1] * uv_scale)
        me.uv_layers.active = lay
        try:
            lay.active_render = True
        except AttributeError:
            pass
        me.materials.clear()
        me.materials.append(mat)

    log("landscape: %d object(s) -> layer %02d (%s), UV %s x %.3f"
        % (len(land), LAND_LAYER,
           (param(texs, pre + "Base Texture") or "?").split("/")[-1],
           "TextureUVs" if src else "world XY", uv_scale))


def clamp_ocean(water_objs):
    """Shrink the ocean plane to a multiple of the island's own extent.

    Unreal's sea here is /Engine/BasicShapes/Plane scaled to 12,550 m. That is
    faithful but unusable: the island is ~500 m, so the scene's bounding box is
    25x the thing you want to look at and every viewer opens on an empty sheet of
    water. The plane is scaled in X/Y only, about its own centre, so the water
    line (z) and everything else stay exactly where Unreal put them.
    """
    if not water_objs or WATER_FACTOR <= 0:
        return
    mn = Vector((1e18,) * 3)
    mx = Vector((-1e18,) * 3)
    for o in bpy.data.objects:
        if o.type != "MESH" or o in water_objs:
            continue
        for c in o.bound_box:
            w = o.matrix_world @ Vector(c)
            mn = Vector((min(mn[i], w[i]) for i in range(3)))
            mx = Vector((max(mx[i], w[i]) for i in range(3)))
    island = max(mx.x - mn.x, mx.y - mn.y)
    want = island * WATER_FACTOR
    for o in water_objs:
        cur = max((o.matrix_world @ Vector(o.bound_box[6]) -
                   o.matrix_world @ Vector(o.bound_box[0])).xy)
        if cur <= want:
            continue
        k = want / cur
        o.scale = (o.scale.x * k, o.scale.y * k, o.scale.z)
        log("ocean: %.0f m -> %.0f m (island %.0f m x %.1f)"
            % (cur, want, island, WATER_FACTOR))


# -------------------------------------------------------------------- build -
def main():
    reset_scene()
    root_coll = bpy.context.scene.collection

    n_obj = n_light = n_skipped = n_top = 0
    water_objs = []
    for node in NODES:
        if node.get("actor") in EXTRA_ACTORS:
            n_skipped += 1
            continue
        if not SKY and node.get("mesh", "").startswith("/Engine/EngineSky/"):
            n_skipped += 1
            continue
        if node["type"] == "light":
            if build_light(node, root_coll):
                n_light += 1
            continue
        if node["type"] != "mesh" or node.get("hidden"):
            continue

        datas = import_mesh(node["mesh"])
        if not datas:
            continue

        # per-slot component override; where missing, fall back to the asset's
        mesh_meta = MESHES.get(node["mesh"], {})
        over = node.get("materials") or []
        defaults = mesh_meta.get("materials") or []
        n_slots = max(len(over), len(defaults))
        slot_keys = []
        for i in range(n_slots):
            k = over[i] if i < len(over) and over[i] else None
            if k is None and i < len(defaults):
                k = defaults[i]
            slot_keys.append(k)

        # Slot left EMPTY in UE (e.g. SM_Conifer_01 slot4 = trunk+branches, 23k
        # faces): UE draws it with the default grey material. Keeping that leaves
        # the tree with white branches, so we fall back to the mesh's own first
        # valid material (--empty-slot keep reproduces UE exactly).
        if EMPTY_SLOT == "first":
            first = next((k for k in slot_keys if k), None)
            if first:
                for i, k in enumerate(slot_keys):
                    if k is None:
                        slot_keys[i] = first
                        log("empty slot: %s slot%d -> %s"
                            % (mesh_meta.get("name", "?"), i,
                               first.split("/")[-1]))
        mats = [build_material(k) if k else None for k in slot_keys]
        # Match slots by NAME, not by index. UE's FBX exporter does not always
        # write material slots in the order of the asset's static_materials:
        # SM_Fir_Tree_03 comes out [MI_Bark_02, MI_Fir_Leaves_Set_01] while UE
        # reports [MI_Fir_Leaves_Set_01, MI_Bark_02]. Assigning by index put the
        # BARK texture on the leaf cards, which is what turned a third of the
        # conifers into big orange scales.
        by_name = {}
        for k, m in zip(slot_keys, mats):
            if k and m:
                by_name[k.split("/")[-1].strip().lower()] = m
        is_water = any(
            "SINGLE_LAYER_WATER" in
            ((MATERIALS.get(k) or {}).get("shading_model") or "").upper()
            for k in slot_keys if k)

        xforms = node["instances"] if node.get("instanced") else [node["xform"]]
        for i, x in enumerate(xforms):
            mw = ue_matrix(x)
            for d in datas:
                name = node["name"] if len(xforms) == 1 else "%s_%d" % (node["name"], i)
                ob = bpy.data.objects.new(name, d)
                root_coll.objects.link(ob)
                ob.matrix_world = mw
                if is_water:
                    water_objs.append(ob)
                for si in range(len(ob.data.materials)):
                    m = by_name.get(_slot_name(ob.data.materials[si]))
                    if m is None and si < len(mats):
                        m = mats[si]          # unnamed slot: fall back to order
                    if m:
                        ob.material_slots[si].link = "OBJECT"
                        ob.material_slots[si].material = m
                if not ob.data.materials and mats and mats[0]:
                    ob.data.materials.append(mats[0])
                if apply_top_layer(ob, mw, node.get("instanced")):
                    n_top += 1
                n_obj += 1

    n_extra = import_extras(root_coll)
    clamp_ocean(water_objs)

    log("objects: %d (+%d baked) | lights: %d | unique meshes: %d | materials: %d | textures: %d"
        % (n_obj, n_extra, n_light, len(_mesh_data), len(_mat_cache),
           len([i for i in _img_cache.values() if i])))
    if n_top:
        log("top layer: %d objects split at %.0f deg from horizontal"
            % (n_top, math.degrees(math.acos(TOP_COS))))
    if n_skipped:
        log("skipped (exported by ue_bake_extras): %d components of %s"
            % (n_skipped, ", ".join(sorted(EXTRA_ACTORS))))

    log("exporting GLB ->", OUT)
    bpy.ops.export_scene.gltf(
        filepath=OUT,
        export_format="GLB",
        use_selection=False,
        export_apply=False,
        export_yup=True,
        export_texcoords=True,
        export_normals=True,
        export_tangents=True,
        export_materials="EXPORT",
        export_image_format="AUTO",
        export_cameras=False,
        export_lights=(arg("--lights", "1") == "1"),
        export_extras=False,
        export_animations=False,
    )
    write_materials_json()
    log("OK ->", OUT, "%.1f MB" % (os.path.getsize(OUT) / 1048576.0))


def write_materials_json():
    """What the GLB cannot carry, written next to it for the target engine.

    Two of this pack's masters do not survive glTF at all:

      * MM_Nature_Assets projects its maps by WORLD POSITION (triplanar) and
        blends a top layer -- moss, sand or dirt -- onto up-facing surfaces;
      * MM_Landscape blends five layers by per-component weightmaps.

    glTF addresses textures by UV and has one base-colour texture per material,
    so neither can be baked into it. An engine that does have triplanar can
    rebuild them, which is exactly how this pack was handled on the Godot side
    before -- so everything needed for that is emitted here: for each material in
    the GLB, which master it derives from, and which texture file goes into which
    parameter.
    """
    out = {
        "level": SCENE.get("level"),
        "glb": os.path.basename(OUT),
        "texture_dir": os.path.basename(TEX_OUT),
        "landscape_material": SCENE.get("landscape_material"),
        "notes": {
            "MM_Nature_Assets": "world-aligned (triplanar) projection; 'Top Layer "
                                "Texture' is blended onto up-facing surfaces",
            "MM_Landscape": "five layers blended by per-component weightmaps, "
                            "which Unreal does not expose to Python",
        },
        "materials": {},
    }

    def files(table):
        return {k: _tex_file.get(v) for k, v in (table or {}).items() if v}

    for mat_name, key in sorted(_mat_key.items()):
        meta = MATERIALS.get(key)
        if not meta:
            continue
        d = meta.get("master_defaults") or {}
        out["materials"][mat_name] = {
            "ue": key,
            "master": (meta.get("parent") or "").split("/")[-1] or None,
            "blend_mode": meta.get("blend_mode"),
            "shading_model": meta.get("shading_model"),
            "two_sided": meta.get("two_sided"),
            "opacity_mask_clip": meta.get("opacity_mask_clip"),
            "textures": files(meta.get("textures")),
            "scalars": meta.get("scalars"),
            "vectors": meta.get("vectors"),
            "master_defaults": {
                "textures": files(d.get("textures")),
                "scalars": d.get("scalars"),
                "vectors": d.get("vectors"),
            },
        }
    dst = os.path.join(SRC, "materials.json")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    log("materials.json: %d materials -> %s"
        % (len(out["materials"]), dst))


main()
