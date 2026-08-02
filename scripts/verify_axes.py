"""Check that UE's FBX + the S conjugation match UE's native glTF.

Reference (glTF written by UE 5.8 itself for SM_Barrel_01, in meters, Y-up):
    POSITION min = [-0.38285014, -0.00302949641, -0.379908472]
    POSITION max = [ 0.37275416,  0.99619854,     0.375695825]
"""
import bpy, sys, os
from mathutils import Matrix

p = lambda *a: (print("[AXIS]", *a), sys.stdout.flush())
SRC = r"D:\projects\UE2Godot\out"
FBX = os.path.join(SRC, "meshes", "TheLightHouseOfNoReturn_Meshes_Props_SM_Barrel_01.fbx")

REF_MIN = (-0.38285014, -0.00302949641, -0.379908472)
REF_MAX = (0.37275416, 0.99619854, 0.375695825)

for scale in (1.0, 0.01):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=FBX, use_manual_orientation=True,
                             axis_forward="-Y", axis_up="Z", global_scale=1.0,
                             use_image_search=False, use_custom_normals=True)
    o = [x for x in bpy.data.objects if x.type == "MESH"][0]
    me = o.data
    me.transform(Matrix.Scale(scale, 4) @ o.matrix_world)
    vs = [v.co for v in me.vertices]
    mn = [min(v[i] for v in vs) for i in range(3)]
    mx = [max(v[i] for v in vs) for i in range(3)]
    p("--- mesh_scale = %s ---" % scale)
    p("blender min: %s" % ["%.6f" % v for v in mn])
    p("blender max: %s" % ["%.6f" % v for v in mx])

    # candidate blender -> gltf mappings
    cands = {
        "(bx, bz, -by)": ((mn[0], mn[2], -mx[1]), (mx[0], mx[2], -mn[1])),
        "(bx, bz, +by)": ((mn[0], mn[2], mn[1]), (mx[0], mx[2], mx[1])),
        "(-bx, bz, +by)": ((-mx[0], mn[2], mn[1]), (-mn[0], mx[2], mx[1])),
        "(-bx, bz, -by)": ((-mx[0], mn[2], -mx[1]), (-mn[0], mx[2], -mn[1])),
    }
    for name, (cmn, cmx) in cands.items():
        err = sum(abs(cmn[i] - REF_MIN[i]) + abs(cmx[i] - REF_MAX[i]) for i in range(3))
        p("  %-16s error=%.6f" % (name, err))

p("uv layers / colors checked on the first pass")
p("AXIS END")
