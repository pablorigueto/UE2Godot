"""
Validation pass (UE side), run after ue_export.py.

1) Dump each actor's world bounds -> bounds.json
   Used to check the Blender-side axis conversion NUMERICALLY: the Blender
   center must be (x, -y, z) * 0.01 of UE's center, and the extents must match
   on (x, y, z) (a reflection does not change size).

2) Export one mesh through UE's NATIVE glTF exporter -> ref_gltf/
   Comparing the normal texture UE writes into that glTF against the raw PNG we
   exported tells us definitively whether UE flips the green channel when
   converting to glTF (DirectX vs OpenGL convention).

3) Dump the master materials' graphs -> masters.json
"""

import unreal
import json
import os

OUT = os.environ.get("UE2G_OUT", r"D:\projects\UE2Godot\out")
LEVEL = os.environ.get("UE2G_LEVEL", "/Game/TheLightHouseOfNoReturn/Levels/L_Overview")
REF = os.path.join(OUT, "ref_gltf")
os.makedirs(REF, exist_ok=True)


def log(m):
    unreal.log("[VAL] %s" % m)


unreal.EditorLoadingAndSavingUtils.load_map(LEVEL)
actor_sys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actors = actor_sys.get_all_level_actors()

# ---------------------------------------------------------------- bounds ----
bounds = {}
for a in actors:
    try:
        origin, extent = a.get_actor_bounds(only_colliding_components=False)
    except Exception:
        continue
    for c in a.get_components_by_class(unreal.StaticMeshComponent):
        if c.static_mesh is None:
            continue
        name = "%s.%s" % (a.get_actor_label(), c.get_name())
        bounds[name] = {
            "center": [float(origin.x), float(origin.y), float(origin.z)],
            "extent": [float(extent.x), float(extent.y), float(extent.z)],
            "mesh": c.static_mesh.get_path_name().split(".")[0],
        }
with open(os.path.join(OUT, "bounds.json"), "w") as f:
    json.dump(bounds, f, indent=1)
log("bounds for %d components" % len(bounds))

# ----------------------------------------------------- UE glTF reference ----
ref_mesh = None
for a in actors:
    for c in a.get_components_by_class(unreal.StaticMeshComponent):
        sm = c.static_mesh
        if sm and "Barrel_01" in sm.get_name():
            ref_mesh = sm
            break
    if ref_mesh:
        break
if ref_mesh is None:
    for a in actors:
        for c in a.get_components_by_class(unreal.StaticMeshComponent):
            if c.static_mesh:
                ref_mesh = c.static_mesh
                break
        if ref_mesh:
            break

if ref_mesh is not None:
    log("reference mesh: %s" % ref_mesh.get_name())
    dst = os.path.join(REF, ref_mesh.get_name() + ".gltf")
    opt_cls = getattr(unreal, "GLTFExportOptions", None)
    exp_cls = getattr(unreal, "GLTFStaticMeshExporter", None) or \
        getattr(unreal, "GLTFExporter", None)
    task = unreal.AssetExportTask()
    task.set_editor_property("object", ref_mesh)
    task.set_editor_property("filename", dst)
    task.set_editor_property("automated", True)
    task.set_editor_property("prompt", False)
    task.set_editor_property("replace_identical", True)
    if opt_cls:
        o = opt_cls()
        try:
            o.set_editor_property("texture_image_format",
                                  unreal.GLTFTextureImageFormat.PNG)
            o.set_editor_property("bundle_web_viewer", False)
            o.set_editor_property("export_material_variants",
                                  unreal.GLTFMaterialVariantMode.NONE)
        except Exception as e:
            log("opts: %s" % e)
        task.set_editor_property("options", o)
    if exp_cls:
        task.set_editor_property("exporter", exp_cls())
    try:
        ok = unreal.Exporter.run_asset_export_task(task)
        log("native glTF: %s -> %s" % (ok, dst))
    except Exception as e:
        log("native glTF failed: %s" % e)

# --------------------------------------------------------- master graphs ----
masters = {}
for path in ["/Game/TheLightHouseOfNoReturn/Materials/" + n for n in
             ("MM_Props", "MM_Nature_Assets", "MM_Building_Master",
              "MM_Foliage", "MM_Glass", "MM_Landscape", "MM_Overview")]:
    m = unreal.load_asset(path)
    if not isinstance(m, unreal.Material):
        continue
    info = {"name": m.get_name(), "expressions": [], "params": {}}
    try:
        info["blend_mode"] = str(m.get_editor_property("blend_mode"))
        info["shading_model"] = str(m.get_editor_property("shading_model"))
        info["two_sided"] = bool(m.get_editor_property("two_sided"))
    except Exception:
        pass
    try:
        names = unreal.MaterialEditingLibrary.get_scalar_parameter_names(m)
        info["params"]["scalar"] = [str(n) for n in names]
        info["params"]["vector"] = [str(n) for n in
                                    unreal.MaterialEditingLibrary.get_vector_parameter_names(m)]
        info["params"]["texture"] = [str(n) for n in
                                     unreal.MaterialEditingLibrary.get_texture_parameter_names(m)]
        info["params"]["switch"] = [str(n) for n in
                                    unreal.MaterialEditingLibrary.get_static_switch_parameter_names(m)]
    except Exception as e:
        info["params_err"] = str(e)

    # list the graph nodes (type + label), enough to infer the math
    exprs = None
    for prop in ("expression_collection", "expressions"):
        try:
            exprs = m.get_editor_property(prop)
            break
        except Exception:
            continue
    if exprs is not None and not isinstance(exprs, list):
        try:
            exprs = exprs.get_editor_property("expressions")
        except Exception:
            exprs = None
    for e in (exprs or []):
        try:
            d = {"class": e.get_class().get_name()}
            for p in ("parameter_name", "const", "constant", "function",
                      "desc", "material_function"):
                try:
                    v = e.get_editor_property(p)
                    if v not in (None, "", 0.0):
                        d[p] = str(v)
                except Exception:
                    pass
            info["expressions"].append(d)
        except Exception:
            pass
    masters[m.get_name()] = info

with open(os.path.join(OUT, "masters.json"), "w") as f:
    json.dump(masters, f, indent=1)
log("masters: %s" % ", ".join(masters))
log("VALIDATE DONE")
