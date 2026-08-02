"""
UE -> Godot converter, stage 1 (Unreal side).

Runs inside UnrealEditor-Cmd in commandlet mode:

  UnrealEditor-Cmd.exe <proj>.uproject -run=pythonscript -script=ue_export.py

Extracts from a level:
  * every unique StaticMesh -> FBX (LOD0, all UV sets, vertex colors)
  * every Texture2D used    -> PNG/TGA at source resolution
  * every Material/MIC      -> parameters (textures, scalars, vectors) + master flags
  * the scene graph         -> scene.json with each component's world matrix,
                               including every ISM/HISM (foliage) instance
  * lights                  -> type, color, intensity, attenuation

The axis conversion is NOT done here: everything is exported in UE's native
space (cm, Z-up, left-handed) and the Blender script applies the change of basis.
"""

import unreal
import json
import math
import os
import re
import traceback

# ----------------------------------------------------------------- config ---
LEVEL = os.environ.get("UE2G_LEVEL", "/Game/TheLightHouseOfNoReturn/Levels/L_Overview")
OUT = os.environ.get("UE2G_OUT", r"D:\projects\UE2Godot\out")

MESH_DIR = os.path.join(OUT, "meshes")
TEX_DIR = os.path.join(OUT, "textures")
for d in (OUT, MESH_DIR, TEX_DIR):
    os.makedirs(d, exist_ok=True)

DEG2RAD = math.pi / 180.0


def log(msg):
    unreal.log("[UE2G] %s" % msg)


def warn(msg):
    unreal.log_warning("[UE2G] %s" % msg)


def safe_name(path_name):
    """/Game/A/B.B  ->  A_B  (unique and valid on disk)."""
    obj = path_name.split(".")[0]
    obj = obj.replace("/Game/", "").replace("/Engine/", "Engine/")
    return re.sub(r"[^A-Za-z0-9_]+", "_", obj).strip("_")


# ------------------------------------------------------------- transforms ---
def rotator_to_quat(pitch, yaw, roll):
    """UE's FRotator::Quaternion(), verbatim (source: UnrealMath.cpp)."""
    sp = math.sin(pitch * 0.5 * DEG2RAD)
    cp = math.cos(pitch * 0.5 * DEG2RAD)
    sy = math.sin(yaw * 0.5 * DEG2RAD)
    cy = math.cos(yaw * 0.5 * DEG2RAD)
    sr = math.sin(roll * 0.5 * DEG2RAD)
    cr = math.cos(roll * 0.5 * DEG2RAD)
    x = cr * sp * sy - sr * cp * cy
    y = -cr * sp * cy - sr * cp * sy
    z = cr * cp * sy - sr * sp * cy
    w = cr * cp * cy + sr * sp * sy
    return [x, y, z, w]


def transform_to_dict(t):
    """unreal.Transform -> {loc, quat, scale} in UE's native space."""
    loc = t.translation
    rot = t.rotation           # FQuat in UE5 Python
    scl = t.scale3d
    try:
        q = [float(rot.x), float(rot.y), float(rot.z), float(rot.w)]
    except AttributeError:     # came through as a Rotator
        q = rotator_to_quat(float(rot.pitch), float(rot.yaw), float(rot.roll))
    return {"loc": [float(loc.x), float(loc.y), float(loc.z)],
            "quat": q,
            "scale": [float(scl.x), float(scl.y), float(scl.z)]}


def component_world_transform(comp):
    """World transform of a SceneComponent, with cross-version fallbacks."""
    for getter in ("get_world_transform", "k2_get_component_to_world"):
        fn = getattr(comp, getter, None)
        if fn:
            try:
                return transform_to_dict(fn())
            except Exception:
                pass
    loc = comp.get_world_location()
    rot = comp.get_world_rotation()
    scl = comp.get_world_scale()
    return {"loc": [float(loc.x), float(loc.y), float(loc.z)],
            "quat": rotator_to_quat(float(rot.pitch), float(rot.yaw), float(rot.roll)),
            "scale": [float(scl.x), float(scl.y), float(scl.z)]}


# ---------------------------------------------------------------- export ----
_exported_meshes = {}
_exported_textures = {}
_exported_materials = {}


def export_static_mesh(mesh):
    key = mesh.get_path_name().split(".")[0]
    if key in _exported_meshes:
        return key
    name = safe_name(key)
    fbx = os.path.join(MESH_DIR, name + ".fbx")

    entry = {"asset": key, "name": name, "file": "meshes/%s.fbx" % name,
             "materials": [], "ok": False}
    _exported_meshes[key] = entry           # register first to avoid recursion

    slots = []
    try:
        for slot in mesh.get_editor_property("static_materials"):
            slots.append(slot.get_editor_property("material_interface"))
    except Exception:
        try:
            for i in range(mesh.get_num_sections(0)):
                slots.append(mesh.get_material(i))
        except Exception as e:
            warn("slots of %s: %s" % (name, e))
    entry["materials"] = [export_material(m) if m else None for m in slots]

    if not os.path.exists(fbx):
        opts = unreal.FbxExportOption()
        opts.set_editor_property("ascii", False)
        opts.set_editor_property("collision", False)
        opts.set_editor_property("level_of_detail", False)   # LOD0 only
        opts.set_editor_property("vertex_color", True)
        task = unreal.AssetExportTask()
        task.set_editor_property("object", mesh)
        task.set_editor_property("filename", fbx)
        task.set_editor_property("automated", True)
        task.set_editor_property("prompt", False)
        task.set_editor_property("replace_identical", True)
        task.set_editor_property("options", opts)
        exp_cls = getattr(unreal, "StaticMeshExporterFBX", None)
        if exp_cls is not None:
            task.set_editor_property("exporter", exp_cls())
        try:
            unreal.Exporter.run_asset_export_task(task)
        except Exception as e:
            warn("FBX %s: %s" % (name, e))

    entry["ok"] = os.path.exists(fbx)
    if not entry["ok"]:
        warn("FBX NOT generated: %s" % name)
    else:
        entry["bytes"] = os.path.getsize(fbx)
    return key


TEX_EXPORTERS = ("TextureExporterPNG", "TextureExporterTGA", "TextureExporterBMP")


def export_texture(tex):
    if tex is None:
        return None
    # Only a plain Texture2D. UTextureExporterPNG::SupportsTexture asserts (a hard
    # engine crash, not a catchable exception) on textures whose editor Source is
    # not a plain 2D image -- CurveLinearColorAtlas, which subclasses Texture2D
    # and is what the Water plugin's master materials reference, is exactly that
    # case. isinstance() would let those through, so match the class exactly.
    if type(tex).__name__ != "Texture2D":
        warn("skipping %s: unsupported texture class %s"
             % (tex.get_name(), type(tex).__name__))
        return None
    key = tex.get_path_name().split(".")[0]
    if key in _exported_textures:
        return key
    name = safe_name(key)

    entry = {"asset": key, "name": name, "file": None, "srgb": True,
             "compression": None, "width": None, "height": None,
             "flip_green": False}
    _exported_textures[key] = entry

    try:
        entry["srgb"] = bool(tex.get_editor_property("srgb"))
        cs = tex.get_editor_property("compression_settings")
        entry["compression"] = str(cs)
        entry["flip_green"] = bool(tex.get_editor_property("flip_green_channel"))
    except Exception:
        pass
    try:
        entry["width"] = int(tex.blueprint_get_size_x())
        entry["height"] = int(tex.blueprint_get_size_y())
    except Exception:
        pass

    # Float source formats are the other thing UTextureExporterPNG::SupportsTexture
    # asserts on. /Water/Textures/Caustics/Caustics_Tiling_01_HDR (TC_HDR) reaches
    # here through the water master's parameter defaults and takes the whole
    # editor down with it -- an assertion, so there is nothing to catch.
    cs = str(entry.get("compression") or "")
    if "HDR" in cs or "Float" in cs:
        warn("skipping %s: %s cannot be exported as PNG" % (name, cs))
        entry["file"] = None
        return key

    # named BEFORE the exporter runs: if the engine asserts inside it the process
    # dies without a Python traceback, and this line is what identifies the asset
    log("  texture %s (%s)" % (name, entry["compression"]))

    for exp_cls in TEX_EXPORTERS:
        cls = getattr(unreal, exp_cls, None)
        if cls is None:
            continue
        ext = {"TextureExporterPNG": ".png", "TextureExporterTGA": ".tga",
               "TextureExporterBMP": ".bmp"}[exp_cls]
        dst = os.path.join(TEX_DIR, name + ext)
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            entry["file"] = "textures/%s%s" % (name, ext)
            return key
        task = unreal.AssetExportTask()
        task.set_editor_property("object", tex)
        task.set_editor_property("filename", dst)
        task.set_editor_property("automated", True)
        task.set_editor_property("prompt", False)
        task.set_editor_property("replace_identical", True)
        task.set_editor_property("exporter", cls())
        try:
            unreal.Exporter.run_asset_export_task(task)
        except Exception:
            pass
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            entry["file"] = "textures/%s%s" % (name, ext)
            return key

    warn("texture NOT exported: %s" % name)
    return key


def _read_params(mic, entry):
    for tp in mic.get_editor_property("texture_parameter_values"):
        pname = str(tp.get_editor_property("parameter_info").get_editor_property("name"))
        val = tp.get_editor_property("parameter_value")
        entry["textures"][pname] = export_texture(val) if val else None
    for sp in mic.get_editor_property("scalar_parameter_values"):
        pname = str(sp.get_editor_property("parameter_info").get_editor_property("name"))
        entry["scalars"][pname] = float(sp.get_editor_property("parameter_value"))
    for vp in mic.get_editor_property("vector_parameter_values"):
        pname = str(vp.get_editor_property("parameter_info").get_editor_property("name"))
        v = vp.get_editor_property("parameter_value")
        entry["vectors"][pname] = [float(v.r), float(v.g), float(v.b), float(v.a)]


def _read_master_defaults(master, entry):
    """Parameter DEFAULTS declared by the master material.

    _read_params only sees what a MIC actually overrides. Anything left at its
    default is absent from the instance, so without this the value is lost --
    e.g. MM_Glass declares the opacity of MI_Lighthouse_Glass_01, which overrides
    nothing: the glass then falls back to the base texture's alpha, which is 0
    everywhere, and the lighthouse windows disappear.

    Read first, so any override from the MIC chain still wins.
    """
    mel = getattr(unreal, "MaterialEditingLibrary", None)
    if mel is None:
        return
    # Kept SEPARATELY as well as merged: knowing a value is only the master's
    # default is what tells the Blender side that a material is not really
    # emissive. MM_Props defaults Emiss Texture to T_Brick_Single_1_B with
    # Emiss Value 1.0, so merging alone made every prop -- boat, pier, barrel,
    # the lighthouse woodwork -- glow orange.
    dflt = entry.setdefault("master_defaults",
                            {"scalars": {}, "vectors": {}, "textures": {}})
    try:
        for n in mel.get_scalar_parameter_names(master):
            try:
                v = float(mel.get_material_default_scalar_parameter_value(master, n))
                entry["scalars"][str(n)] = v
                dflt["scalars"][str(n)] = v
            except Exception:
                pass
        for n in mel.get_vector_parameter_names(master):
            try:
                c = mel.get_material_default_vector_parameter_value(master, n)
                v = [float(c.r), float(c.g), float(c.b), float(c.a)]
                entry["vectors"][str(n)] = v
                dflt["vectors"][str(n)] = v
            except Exception:
                pass
        for n in mel.get_texture_parameter_names(master):
            try:
                t = mel.get_material_default_texture_parameter_value(master, n)
                if isinstance(t, unreal.Texture2D):
                    k = export_texture(t)
                    entry["textures"][str(n)] = k
                    dflt["textures"][str(n)] = k
            except Exception:
                pass
    except Exception as e:
        warn("master defaults %s: %s" % (master.get_name(), e))


def export_material(mat):
    if mat is None:
        return None
    key = mat.get_path_name().split(".")[0]
    if key in _exported_materials:
        return key
    entry = {"asset": key, "name": safe_name(key), "parent": None,
             "textures": {}, "scalars": {}, "vectors": {},
             "blend_mode": None, "shading_model": None, "two_sided": False,
             "opacity_mask_clip": 0.3333}
    _exported_materials[key] = entry

    # inheritance chain: MIC -> ... -> Material (the master)
    chain = []
    cur = mat
    while isinstance(cur, unreal.MaterialInstance):
        chain.append(cur)
        cur = cur.get_editor_property("parent")
        if cur is None:
            break
    master = cur if isinstance(cur, unreal.Material) else None
    entry["parent"] = master.get_path_name().split(".")[0] if master else None

    # master defaults first, then master-most MIC, then leaf: most specific wins
    if master is not None:
        _read_master_defaults(master, entry)
    for mic in reversed(chain):
        try:
            _read_params(mic, entry)
        except Exception as e:
            warn("params %s: %s" % (entry["name"], e))

    if master is not None:
        try:
            entry["blend_mode"] = str(master.get_editor_property("blend_mode"))
            entry["shading_model"] = str(master.get_editor_property("shading_model"))
            entry["two_sided"] = bool(master.get_editor_property("two_sided"))
            entry["opacity_mask_clip"] = float(
                master.get_editor_property("opacity_mask_clip_value"))
        except Exception:
            pass
        # textures used by the master but never overridden by a parameter
        try:
            for t in master.get_editor_property("referenced_textures") or []:
                if isinstance(t, unreal.Texture2D):
                    entry["textures"].setdefault("__master__%s" % t.get_name(),
                                                 export_texture(t))
        except Exception:
            pass

    # overrides on the leaf MIC itself (two-sided etc.)
    if isinstance(mat, unreal.MaterialInstance):
        try:
            bo = mat.get_editor_property("base_property_overrides")
            if bo.get_editor_property("override_two_sided"):
                entry["two_sided"] = bool(bo.get_editor_property("two_sided"))
            if bo.get_editor_property("override_blend_mode"):
                entry["blend_mode"] = str(bo.get_editor_property("blend_mode"))
            if bo.get_editor_property("override_opacity_mask_clip_value"):
                entry["opacity_mask_clip"] = float(
                    bo.get_editor_property("opacity_mask_clip_value"))
        except Exception:
            pass
    return key


# ------------------------------------------------------------------ scene ---
def collect_light(comp, actor, nodes):
    kind = None
    if isinstance(comp, unreal.DirectionalLightComponent):
        kind = "directional"
    elif isinstance(comp, unreal.SpotLightComponent):
        kind = "spot"
    elif isinstance(comp, unreal.RectLightComponent):
        kind = "rect"
    elif isinstance(comp, unreal.PointLightComponent):
        kind = "point"
    elif isinstance(comp, unreal.SkyLightComponent):
        kind = "sky"
    if kind is None:
        return
    n = {"type": "light", "light": kind,
         "name": "%s.%s" % (actor.get_actor_label(), comp.get_name()),
         "xform": component_world_transform(comp)}
    try:
        n["intensity"] = float(comp.get_editor_property("intensity"))
        c = comp.get_editor_property("light_color")
        n["color"] = [c.r / 255.0, c.g / 255.0, c.b / 255.0]
    except Exception:
        pass
    for prop, out in (("attenuation_radius", "range"),
                      ("outer_cone_angle", "outer_cone"),
                      ("inner_cone_angle", "inner_cone"),
                      ("source_radius", "source_radius"),
                      ("intensity_units", "intensity_units")):
        try:
            v = comp.get_editor_property(prop)
            n[out] = float(v) if not isinstance(v, unreal.EnumBase) else str(v)
        except Exception:
            pass
    nodes.append(n)


def collect_mesh_component(comp, actor, nodes):
    mesh = comp.static_mesh
    if mesh is None:
        return
    mesh_key = export_static_mesh(mesh)

    overrides = []
    try:
        for i in range(comp.get_num_materials()):
            m = comp.get_material(i)
            overrides.append(export_material(m) if m else None)
    except Exception as e:
        warn("materials of %s: %s" % (comp.get_name(), e))

    hidden = False
    try:
        hidden = bool(comp.get_editor_property("hidden_in_game")) or \
                 bool(actor.is_temporarily_hidden_in_editor())
    except Exception:
        pass

    base = {"type": "mesh", "mesh": mesh_key, "materials": overrides,
            "actor": actor.get_actor_label(), "component": comp.get_name(),
            "actor_class": actor.get_class().get_name(), "hidden": hidden}

    if isinstance(comp, unreal.InstancedStaticMeshComponent):
        count = comp.get_instance_count()
        insts = []
        for i in range(count):
            try:
                t = comp.get_instance_transform(i, world_space=True)
                insts.append(transform_to_dict(t))
            except Exception:
                pass
        base["name"] = "%s.%s" % (actor.get_actor_label(), comp.get_name())
        base["instances"] = insts
        base["instanced"] = True
        nodes.append(base)
    else:
        base["name"] = "%s.%s" % (actor.get_actor_label(), comp.get_name())
        base["xform"] = component_world_transform(comp)
        base["instanced"] = False
        nodes.append(base)


def main():
    log("loading level %s" % LEVEL)
    unreal.EditorLoadingAndSavingUtils.load_map(LEVEL)

    actor_sys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actors = actor_sys.get_all_level_actors()
    log("%d actors" % len(actors))

    nodes = []
    skipped = []
    for idx, a in enumerate(actors):
        try:
            comps = a.get_components_by_class(unreal.SceneComponent)
            touched = False
            for c in comps:
                if isinstance(c, unreal.StaticMeshComponent):
                    collect_mesh_component(c, a, nodes)
                    touched = True
                elif isinstance(c, unreal.LightComponentBase):
                    collect_light(c, a, nodes)
                    touched = True
            if not touched:
                skipped.append("%s (%s)" % (a.get_actor_label(),
                                            a.get_class().get_name()))
        except Exception as e:
            warn("actor %s: %s" % (a.get_actor_label(), e))
            traceback.print_exc()
        if idx % 20 == 0:
            log("  actor %d/%d" % (idx, len(actors)))

    # Landscape: its components are LandscapeComponents, not StaticMeshComponents,
    # so the actor never reaches collect_mesh_component and the terrain is absent
    # from this export -- the geometry comes from ue_bake_extras.py, through UE's
    # own glTF exporter. Its MATERIAL, though, is an ordinary material instance
    # (MI_Landscape_01 of MM_Landscape), so it is exported here like any other and
    # blender_build.py textures the imported terrain with it.
    land_mat = None
    for a in actors:
        if not a.get_class().get_name().startswith("Landscape"):
            continue
        try:
            m = a.get_editor_property("landscape_material")
        except Exception:
            continue
        if m is not None:
            land_mat = export_material(m)
            log("landscape material: %s" % land_mat)
            break

    scene = {
        "level": LEVEL,
        "space": "unreal",           # cm, Z-up, left-handed
        "engine": "UE5.8",
        "landscape_material": land_mat,
        "nodes": nodes,
        "meshes": _exported_meshes,
        "materials": _exported_materials,
        "textures": _exported_textures,
        "skipped_actors": skipped,
    }
    with open(os.path.join(OUT, "scene.json"), "w", encoding="utf-8") as f:
        json.dump(scene, f, indent=1)

    n_inst = sum(len(n.get("instances", [])) for n in nodes if n.get("instanced"))
    n_mesh_nodes = sum(1 for n in nodes if n["type"] == "mesh" and not n.get("instanced"))
    log("=" * 60)
    log("nodes          : %d" % len(nodes))
    log("  plain meshes : %d" % n_mesh_nodes)
    log("  instances    : %d" % n_inst)
    log("  lights       : %d" % sum(1 for n in nodes if n["type"] == "light"))
    log("unique meshes  : %d (%d FBX ok)"
        % (len(_exported_meshes), sum(1 for m in _exported_meshes.values() if m["ok"])))
    log("materials      : %d" % len(_exported_materials))
    log("textures       : %d (%d files)"
        % (len(_exported_textures),
           sum(1 for t in _exported_textures.values() if t["file"])))
    log("actors with no geometry/light: %d" % len(skipped))
    for s in skipped:
        log("    - %s" % s)
    log("OK -> %s" % OUT)


main()
