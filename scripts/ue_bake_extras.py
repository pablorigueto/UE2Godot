"""
UE -> Godot converter, stage 1b (Unreal side): the actors the main path cannot express.

`ue_export.py` walks StaticMeshComponents and rebuilds each material from its
MIC parameters. Three kinds of actor fall outside that:

  * Landscape          -- LandscapeComponents are not StaticMeshComponents, so the
                          terrain is simply absent from the export.
  * sky sphere         -- /Engine/EngineSky/SM_SkySphere driven by a dynamic
                          instance of M_Sky_Panning_Clouds2 (MSM_UNLIT gradient):
                          it has no texture parameters, so it lands in the GLB as
                          a flat 0.8 grey shell around the whole scene.
  * single-layer water -- a plane with MSM_SINGLE_LAYER_WATER, likewise
                          parameterless, likewise flat grey (and 12.5 km wide).

For these, Unreal's OWN glTF exporter is used with material BAKING enabled: it
renders each material's inputs to textures using the mesh's UVs, which is the
only way to reproduce a layer-blended landscape or a procedural sky gradient.
The result is merged by blender_build.py, which skips the same actors on its side.

    out/extras/extras.gltf   baked geometry + materials
    out/extras/extras.json   actors baked here (so the main path skips them)
"""

import unreal
import array
import json
import os
import time

LEVEL = os.environ.get("UE2G_LEVEL", "/Game/TheLightHouseOfNoReturn/Levels/L_Showcase")
OUT = os.path.join(os.environ.get("UE2G_OUT", r"D:\projects\UE2Godot\out"), "extras")
BAKE_SIZE = os.environ.get("UE2G_BAKE_SIZE", "512")
os.makedirs(OUT, exist_ok=True)


def log(m):
    unreal.log("[XTRA] %s" % m)


def warn(m):
    unreal.log_warning("[XTRA] %s" % m)


# --------------------------------------------------------------- selection ---
SKY_MESHES = ("/Engine/EngineSky/",)
WATER_SHADING = "SINGLE_LAYER_WATER"


def is_landscape(a):
    for cls in ("Landscape", "LandscapeProxy", "LandscapeStreamingProxy"):
        t = getattr(unreal, cls, None)
        if t and isinstance(a, t):
            return True
    return a.get_class().get_name().startswith("Landscape")


def mesh_path(comp):
    sm = getattr(comp, "static_mesh", None)
    return sm.get_path_name().split(".")[0] if sm else ""


def is_sky(a):
    for c in a.get_components_by_class(unreal.StaticMeshComponent):
        if any(k in mesh_path(c) for k in SKY_MESHES):
            return True
    return False


unreal.EditorLoadingAndSavingUtils.load_map(LEVEL)
actor_sys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actors = actor_sys.get_all_level_actors()
log("%d actors in %s" % (len(actors), LEVEL))

picked, why = [], {}
for a in actors:
    tag = None
    if is_landscape(a):
        tag = "landscape"
    elif is_sky(a):
        tag = "sky"
    # NOTE: single-layer water is deliberately NOT baked here. Unreal's own glTF
    # exporter refuses it -- "Unsupported shading model (SingleLayerWater) in
    # material WaterCausticsPreview_Texture, will export as Default Lit" -- and
    # hands back a flat 0.85 grey, which on this level is a 12.5 km plane of
    # white. blender_build.py rebuilds it from the shading model instead.
    if tag:
        picked.append(a)
        why[a.get_actor_label()] = tag

if not picked:
    log("nothing to bake")
else:
    for lbl, tag in sorted(why.items()):
        log("  %-10s %s" % (tag, lbl))

    # Isolating by SELECTION does not work in a commandlet (set_selected_level_actors
    # is a no-op with no editor viewport, and AssetExportTask.bSelected then exports
    # the whole level). The world here is a throwaway that is never saved, so the
    # reliable way to export only these actors is to destroy every other one.
    keep = set(picked)
    killed = 0
    for a in actors:
        if a in keep:
            continue
        try:
            actor_sys.destroy_actor(a)
            killed += 1
        except Exception:
            pass
    log("isolated: %d actors destroyed, %d kept" % (killed, len(picked)))

    # ------------------------------------------------------------- options ---
    opts = None
    cls = getattr(unreal, "GLTFExportOptions", None)
    if cls:
        opts = cls()
        log("options available: %s"
            % json.dumps(sorted(p for p in dir(opts) if not p.startswith("_"))))

        def try_set(name, value, note=""):
            try:
                opts.set_editor_property(name, value)
                log("  set %s = %s %s" % (name, value, note))
                return True
            except Exception as e:
                warn("  cannot set %s: %s" % (name, e))
                return False

        # USE_MESH_DATA bakes through the mesh's own UVs -- required for a
        # layer-blended landscape, whose look depends on per-component weightmaps.
        mode = getattr(unreal, "GLTFMaterialBakeMode", None)
        if mode:
            try_set("bake_material_inputs", mode.USE_MESH_DATA)

        # In UE 5.8 the bake size is a struct {x, y, auto_detect}, not an enum.
        # auto_detect sizes each bake after the source textures, which on a
        # landscape means one 1K set PER COMPONENT (64 here) -- hundreds of MB in
        # the GLB. A fixed size keeps it bounded; 512 over a ~63 m component is
        # about 8 px/m, and the Blender side caps everything at --tex anyway.
        try:
            cur = opts.get_editor_property("default_material_bake_size")
            log("  default_material_bake_size = %s (%s)" % (cur, type(cur).__name__))
            if BAKE_SIZE and BAKE_SIZE != "auto":
                size = type(cur)()
                size.set_editor_property("auto_detect", False)
                size.set_editor_property("x", int(BAKE_SIZE))
                size.set_editor_property("y", int(BAKE_SIZE))
                try_set("default_material_bake_size", size)
        except Exception as e:
            warn("  bake size: %s" % e)
        for n, v in (("export_unlit_materials", True),   # sky sphere is MSM_UNLIT
                     ("adjust_normalmaps", True)):
            try_set(n, v)
        fmt = getattr(unreal, "GLTFTextureImageFormat", None)
        if fmt:
            try_set("texture_image_format", fmt.PNG)
        var = getattr(unreal, "GLTFMaterialVariantMode", None)
        if var:
            try_set("export_material_variants", var.NONE)
        for n, v in (("bundle_web_viewer", False),
                     ("export_preview_mesh", False),
                     ("export_hidden_in_game", False),
                     ("export_lights", False),
                     ("export_cameras", False),
                     ("export_vertex_colors", True),
                     ("export_texture_coordinates", True)):
            try_set(n, v)

    # -------------------------------------------------------------- export ---
    world = unreal.EditorLevelLibrary.get_editor_world()
    dst = os.path.join(OUT, "extras.gltf")
    task = unreal.AssetExportTask()
    task.set_editor_property("object", world)
    task.set_editor_property("filename", dst)
    task.set_editor_property("automated", True)
    task.set_editor_property("prompt", False)
    task.set_editor_property("replace_identical", True)
    task.set_editor_property("selected", True)      # only the actors selected above
    if opts:
        task.set_editor_property("options", opts)
    exp = getattr(unreal, "GLTFLevelExporter", None)
    if exp:
        task.set_editor_property("exporter", exp())
    try:
        ok = unreal.Exporter.run_asset_export_task(task)
        log("export: %s -> %s" % (ok, dst))
    except Exception as e:
        warn("export FAILED: %s" % e)

    with open(os.path.join(OUT, "extras.json"), "w", encoding="utf-8") as f:
        json.dump({"level": LEVEL, "actors": why}, f, indent=1)

    # ------------------------------------------- landscape GEOMETRY ----------
    # UE's glTF exporter writes the terrain FLAT: every LandscapeComponent comes
    # out as a 0.63 x 0 x 0.63 m patch, a 63x63 grid of quads with zero height
    # (verified against the untouched reference export too, so it is the exporter,
    # not the isolation done above). A flat plate at z=1 m under a sea at z=2.3 m
    # is exactly the "transparent ground" the conversion showed.
    #
    # The FBX LEVEL exporter does carry the heightfield -- one 508k-face mesh
    # spanning z 0.9..27.9 m -- so the terrain geometry comes from there instead.
    # The sky is dropped first so the FBX holds nothing but the landscape.
    land = [a for a in picked if why.get(a.get_actor_label()) == "landscape"]
    if land:
        for a in picked:
            if a not in land:
                try:
                    actor_sys.destroy_actor(a)
                except Exception:
                    pass
        fbx = os.path.join(OUT, "landscape.fbx")
        task = unreal.AssetExportTask()
        task.set_editor_property("object", unreal.EditorLevelLibrary.get_editor_world())
        task.set_editor_property("filename", fbx)
        task.set_editor_property("automated", True)
        task.set_editor_property("prompt", False)
        task.set_editor_property("replace_identical", True)
        exp_fbx = getattr(unreal, "LevelExporterFBX", None)
        if exp_fbx:
            task.set_editor_property("exporter", exp_fbx())
        opt = getattr(unreal, "FbxExportOption", None)
        if opt:
            o = opt()
            for k, v in (("ascii", False), ("collision", False),
                         ("level_of_detail", False), ("vertex_color", True)):
                try:
                    o.set_editor_property(k, v)
                except Exception:
                    pass
            task.set_editor_property("options", o)
        try:
            ok = unreal.Exporter.run_asset_export_task(task)
            log("landscape FBX: %s -> %s (%.1f MB)"
                % (ok, fbx,
                   os.path.getsize(fbx) / 1048576.0 if os.path.exists(fbx) else 0))
        except Exception as e:
            warn("landscape FBX FAILED: %s" % e)

    # -------------------------------------------- landscape layer weights ----
    # The painted weights ARE reachable, just not the way it first looks:
    # LandscapeComponent.weightmap_textures is not exposed, and the renderer-based
    # routes all come back empty in a commandlet (render_weightmap returns True and
    # writes nothing; so does a SceneCapture, and material baking). What does work
    # is the CPU query editor_get_paint_layer_weight_by_name_at_location.
    #
    # It is answered PER COMPONENT and only inside that component's own 63 m patch
    # -- asking component 0 about the whole landscape returns 0 everywhere except
    # its own corner, which is what made this look like a dead end at first. So the
    # grid is walked component by component.
    #
    # Written as raw float32 next to a small JSON: blender_build.py blends the five
    # layer textures with it and bakes the terrain's real colour.
    if land:
        a = land[0]
        names = [str(n) for n in a.get_target_layer_names()
                 if not str(n).startswith("__")]
        comps = [c for c in a.get_components_by_class(unreal.SceneComponent)
                 if "LandscapeComponent" in c.get_class().get_name()]
        o, e = a.get_actor_bounds(only_colliding_components=False)
        xf = a.get_actor_transform()
        quad_cm = float(xf.scale3d.x)                  # cm per landscape quad
        lo_x, lo_y = o.x - e.x, o.y - e.y
        span = 2.0 * e.x
        quads = int(round(span / quad_cm))             # 504 here
        per = int(round(quads / (len(comps) ** 0.5)))  # 63 quads per component
        log("weights: %d layers, %d components, %d quads (%.0f cm each), "
            "%d per component" % (len(names), len(comps), quads, quad_cm, per))

        # index the components by their section origin, in quads
        by_section = {}
        for c in comps:
            try:
                sx = int(c.get_editor_property("section_base_x"))
                sy = int(c.get_editor_property("section_base_y"))
            except Exception:
                continue
            by_section[(sx // per, sy // per)] = c

        STEP = int(os.environ.get("UE2G_WEIGHT_STEP", "2"))   # quads per sample
        n = quads // STEP
        data = {}
        t0 = time.time()
        for nm in names:
            buf = array.array("f", [0.0]) * 0
            buf = array.array("f", bytes(4 * n * n))
            name_obj = unreal.Name(nm)
            for j in range(n):
                y = lo_y + (j * STEP + 0.5) * quad_cm
                cj = min(len(by_section) and (j * STEP) // per, quads // per - 1)
                for i in range(n):
                    x = lo_x + (i * STEP + 0.5) * quad_cm
                    ci = (i * STEP) // per
                    c = by_section.get((ci, cj))
                    if c is None:
                        continue
                    try:
                        buf[j * n + i] = float(
                            c.editor_get_paint_layer_weight_by_name_at_location(
                                unreal.Vector(x, y, o.z), name_obj))
                    except Exception:
                        pass
            nz = sum(1 for v in buf if v > 0.01)
            log("  %-10s %d x %d  painted %.1f%%" % (nm, n, n, 100.0 * nz / (n * n)))
            with open(os.path.join(OUT, "weight_%s.f32" % nm), "wb") as f:
                buf.tofile(f)
            data[nm] = "weight_%s.f32" % nm
        log("  sampled in %.1fs" % (time.time() - t0))
        with open(os.path.join(OUT, "landscape_weights.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"layers": names, "files": data, "size": n,
                       "step_quads": STEP, "quad_cm": quad_cm,
                       "origin_ue_cm": [lo_x, lo_y], "span_cm": span}, f, indent=1)

    # ---------------------------------------------- landscape material -------
    # Only the GEOMETRY is taken from the file above: UE's glTF exporter writes
    # MI_Landscape_01 as baseColorFactor [0,0,0] -- a black terrain -- in every
    # bake mode (USE_MESH_DATA and SIMPLE both produce zero textures; the baker is
    # implemented for static meshes, which is why the sky sphere DOES come out
    # baked, but not for LandscapeComponents).
    #
    # Reading the albedo out of the renderer instead does not work either: in a
    # commandlet the SCS_BASE_COLOR g-buffer comes back constant, unlit_viewmode
    # has no effect, and once the other actors are removed the landscape stops
    # rendering altogether (black even under a 100x sun).
    #
    # What IS reachable is the material: MI_Landscape_01 is an ordinary instance
    # of MM_Landscape exposing "Layer NN Base/Normal/ORM Texture", so ue_export.py
    # exports it like any other material and blender_build.py rebuilds the terrain
    # from the real layer textures. The per-pixel blend between the five layers is
    # the one thing out of reach: LandscapeComponent.weightmap_textures is not
    # exposed to Python.

log("XTRA DONE")
