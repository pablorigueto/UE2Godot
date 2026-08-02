"""Export the whole level with UE's NATIVE glTF exporter, as a reference.

Used to cross-check, independently:
  * each actor's world transform
  * the normal map convention (does UE flip green when going to glTF?)
"""
import unreal
import json
import os

LEVEL = os.environ.get("UE2G_LEVEL", "/Game/TheLightHouseOfNoReturn/Levels/L_Overview")
OUT = os.path.join(os.environ.get("UE2G_OUT", r"D:\projects\UE2Godot\out"), "ref_level")
os.makedirs(OUT, exist_ok=True)


def log(m):
    unreal.log("[REF] %s" % m)


unreal.EditorLoadingAndSavingUtils.load_map(LEVEL)

opt_cls = getattr(unreal, "GLTFExportOptions", None)
opts = None
if opt_cls:
    opts = opt_cls()
    props = [p for p in dir(opts) if not p.startswith("_")]
    log("GLTFExportOptions: %s" % json.dumps(sorted(props)))
    for name, val in (("export_material_variants", None),
                      ("bake_material_inputs", None),
                      ("default_material_bake_size", None),
                      ("texture_image_format", None),
                      ("export_proxy_materials", True),
                      ("export_unlit_materials", True),
                      ("export_vertex_colors", True),
                      ("export_texture_coordinates", True),
                      ("export_lightmaps", False),
                      ("export_hidden_in_game", False)):
        try:
            cur = opts.get_editor_property(name)
            log("  %s = %s" % (name, cur))
            if val is not None:
                opts.set_editor_property(name, val)
        except Exception:
            pass

world = unreal.EditorLevelLibrary.get_editor_world()
dst = os.path.join(OUT, LEVEL.rstrip("/").split("/")[-1] + ".gltf")
task = unreal.AssetExportTask()
task.set_editor_property("object", world)
task.set_editor_property("filename", dst)
task.set_editor_property("automated", True)
task.set_editor_property("prompt", False)
task.set_editor_property("replace_identical", True)
if opts:
    task.set_editor_property("options", opts)
exp = getattr(unreal, "GLTFLevelExporter", None)
if exp:
    task.set_editor_property("exporter", exp())
try:
    ok = unreal.Exporter.run_asset_export_task(task)
    log("level glTF export: %s -> %s" % (ok, dst))
except Exception as e:
    log("FAILED: %s" % e)
log("REF DONE")
