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


def log(*a):
    print("[B2G]", *a)
    sys.stdout.flush()


with open(os.path.join(SRC, "scene.json"), "r", encoding="utf-8") as f:
    SCENE = json.load(f)

MESHES = SCENE["meshes"]
MATERIALS = SCENE["materials"]
TEXTURES = SCENE["textures"]
NODES = SCENE["nodes"]


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
    e_img = get_image(param(texs, "Emiss Texture"), non_color=False)
    e_col = param(vecs, "Emiss Color")
    e_val = param(scal, "Emiss Value")
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

    mat.use_backface_culling = not bool(meta.get("two_sided"))
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


# -------------------------------------------------------------------- build -
def main():
    reset_scene()
    root_coll = bpy.context.scene.collection

    n_obj = n_light = 0
    for node in NODES:
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

        xforms = node["instances"] if node.get("instanced") else [node["xform"]]
        for i, x in enumerate(xforms):
            mw = ue_matrix(x)
            for d in datas:
                name = node["name"] if len(xforms) == 1 else "%s_%d" % (node["name"], i)
                ob = bpy.data.objects.new(name, d)
                root_coll.objects.link(ob)
                ob.matrix_world = mw
                for si in range(len(ob.data.materials)):
                    if si < len(mats) and mats[si]:
                        ob.material_slots[si].link = "OBJECT"
                        ob.material_slots[si].material = mats[si]
                if not ob.data.materials and mats and mats[0]:
                    ob.data.materials.append(mats[0])
                n_obj += 1

    log("objects: %d | lights: %d | unique meshes: %d | materials: %d | textures: %d"
        % (n_obj, n_light, len(_mesh_data), len(_mat_cache),
           len([i for i in _img_cache.values() if i])))

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
    log("OK ->", OUT, "%.1f MB" % (os.path.getsize(OUT) / 1048576.0))


main()
