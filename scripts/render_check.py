"""Import the generated GLB back and render it -- end-to-end visual proof.

  blender -b -P render_check.py -- --glb scene.glb --out check.png [--cam iso|top|closeup]
"""
import bpy, sys, os, math
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
arg = lambda f, d=None: argv[argv.index(f) + 1] if f in argv else d
GLB = arg("--glb")
OUT = arg("--out", "check.png")
CAM = arg("--cam", "iso")
RES = int(arg("--res", "1600"))
p = lambda *a: (print("[RENDER]", *a), sys.stdout.flush())

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=GLB)

objs = [o for o in bpy.data.objects if o.type == "MESH"]
p("meshes imported:", len(objs))

ONLY = arg("--only")
if ONLY:
    keep = [o for o in objs if ONLY.lower() in o.name.lower()]
    p("isolating:", [o.name for o in keep])
    for o in list(objs):
        if o not in keep:
            bpy.data.objects.remove(o, do_unlink=True)
    objs = keep
p("materials:", len(bpy.data.materials), "| images:", len(bpy.data.images))

# global bbox
mn = Vector((1e18,) * 3)
mx = Vector((-1e18,) * 3)
for o in objs:
    for c in o.bound_box:
        w = o.matrix_world @ Vector(c)
        mn = Vector((min(mn[i], w[i]) for i in range(3)))
        mx = Vector((max(mx[i], w[i]) for i in range(3)))
ctr = (mn + mx) / 2
size = (mx - mn)
p("bbox min", ["%.2f" % v for v in mn], "max", ["%.2f" % v for v in mx])
p("dimensions (m):", ["%.2f" % v for v in size])

# light + sky
p("lights coming from the GLB:", [(o.name, o.data.type, round(o.data.energy, 4))
                                  for o in bpy.data.objects if o.type == "LIGHT"])
sun_d = bpy.data.lights.new("sun", "SUN")
sun_d.energy = float(arg("--sun", "3.0"))
sun_d.angle = math.radians(2)
sun = bpy.data.objects.new("sun", sun_d)
bpy.context.scene.collection.objects.link(sun)
sun.rotation_euler = (math.radians(50), 0, math.radians(35))

world = bpy.data.worlds.new("w")
bpy.context.scene.world = world
world.use_nodes = True
BG = arg("--bg", "0.30,0.42,0.62")
world.node_tree.nodes["Background"].inputs[0].default_value = \
    tuple(float(v) for v in BG.split(",")) + (1,)
world.node_tree.nodes["Background"].inputs[1].default_value = float(arg("--bg-strength", "1.0"))

cam_d = bpy.data.cameras.new("cam")
cam = bpy.data.objects.new("cam", cam_d)
bpy.context.scene.collection.objects.link(cam)
bpy.context.scene.camera = cam

TARGET = arg("--target")
if TARGET:
    sel = [o for o in objs if TARGET.lower() in o.name.lower()]
    if not sel:
        p("!! target not found:", TARGET, "| examples:", [o.name for o in objs[:10]])
    else:
        p("target:", [o.name for o in sel][:5])
        mn = Vector((1e18,) * 3)
        mx = Vector((-1e18,) * 3)
        for o in sel:
            for c in o.bound_box:
                w = o.matrix_world @ Vector(c)
                mn = Vector((min(mn[i], w[i]) for i in range(3)))
                mx = Vector((max(mx[i], w[i]) for i in range(3)))
        ctr = (mn + mx) / 2
        size = mx - mn
        p("target bbox (m):", ["%.2f" % v for v in size])

radius = max(size) * 0.65
if CAM == "top":
    cam.location = ctr + Vector((0, 0, max(size)))
    cam.rotation_euler = (0, 0, 0)
elif CAM == "closeup":
    cam_d.lens = 50
    d = max(max(size) * 1.15, 0.4)
    cam.location = ctr + Vector((d * 0.75, -d * 0.9, d * 0.5))
    cam.rotation_euler = (math.radians(66), 0, math.radians(40))
else:
    cam.location = ctr + Vector((radius * 0.9, -radius * 1.1, radius * 0.55))
    cam.rotation_euler = (math.radians(66), 0, math.radians(40))

sc = bpy.context.scene
sc.render.resolution_x = RES
sc.render.resolution_y = int(RES * 9 / 16)
sc.render.film_transparent = False
sc.render.filepath = OUT
sc.render.image_settings.file_format = "PNG"
ENGINE = arg("--engine", "EEVEE")
if ENGINE.upper() == "CYCLES":
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = int(arg("--samples", "24"))
    sc.cycles.use_denoising = True
else:
    try:
        sc.render.engine = "BLENDER_EEVEE_NEXT"
    except Exception:
        sc.render.engine = "BLENDER_EEVEE"
    try:
        sc.eevee.taa_render_samples = int(arg("--samples", "32"))
    except Exception:
        pass
try:
    sc.view_settings.view_transform = arg("--view", "Standard")
    sc.view_settings.exposure = float(arg("--exposure", "0"))
except Exception as e:
    p("view transform:", e)

bpy.ops.render.render(write_still=True)
p("render ->", OUT)
p("RENDER END")
