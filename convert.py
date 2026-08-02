#!/usr/bin/env python3
"""
ue2godot -- Unreal Engine level to GLB (Godot) converter.

Works with UNCOOKED asset packs (.uasset/.umap with no .uexp), where the geometry
lives in editor bulk data. That is why umodel/CUE4Parse and most converters fail
on them: here Unreal Engine itself reads the assets, running headless as a
commandlet, and Blender rebuilds the scene and writes the GLB.

    python convert.py --content D:\\projects\\MyPack --level /Game/MyPack/Levels/L_X

Steps:
  1. create (or reuse) a temporary UE project with a junction to the content
  2. UnrealEditor-Cmd -run=pythonscript ue_export.py
        -> one FBX per mesh, one PNG per texture, scene.json (transforms + materials)
  3. blender --background blender_build.py
        -> rebuild the scene, PBR materials, resize textures, export the GLB
  4. (optional --verify) export a reference glTF with UE's NATIVE exporter and
     compare node by node, proving the transforms match

Requirements: Unreal Engine 5.x and Blender 4.2+ installed.
"""

import argparse
import json
import os
import subprocess
import sys
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "scripts")


def find_unreal(hint=None):
    if hint:
        return hint
    pats = [r"C:\Program Files\Epic Games\UE_*\Engine\Binaries\Win64\UnrealEditor-Cmd.exe",
            r"D:\Epic Games\UE_*\Engine\Binaries\Win64\UnrealEditor-Cmd.exe",
            r"C:\Epic Games\UE_*\Engine\Binaries\Win64\UnrealEditor-Cmd.exe"]
    hits = sorted(h for pat in pats for h in glob.glob(pat))
    return hits[-1] if hits else None


def find_blender(hint=None):
    if hint:
        return hint
    pats = [r"C:\Program Files\Blender Foundation\Blender *\blender.exe",
            r"C:\Program Files\Blender\blender.exe"]
    hits = sorted(h for pat in pats for h in glob.glob(pat))
    return hits[-1] if hits else None


def make_project(proj_dir, content_dir, plugins_extra=()):
    """Build a minimal .uproject with a junction Content/<pack> -> content_dir."""
    os.makedirs(os.path.join(proj_dir, "Config"), exist_ok=True)
    name = os.path.basename(content_dir.rstrip("\\/"))
    plugins = ["PythonScriptPlugin", "EditorScriptingUtilities", "GLTFExporter"]
    plugins += list(plugins_extra)
    uproj = os.path.join(proj_dir, "UE2GodotExport.uproject")
    with open(uproj, "w", encoding="utf-8") as f:
        json.dump({"FileVersion": 3, "EngineAssociation": "",
                   "Description": "ue2godot temporary project",
                   "Modules": [],
                   "Plugins": [{"Name": p, "Enabled": True} for p in plugins]},
                  f, indent=1)

    link = os.path.join(proj_dir, "Content", name)
    os.makedirs(os.path.join(proj_dir, "Content"), exist_ok=True)
    if not os.path.exists(link):
        # junction: needs no administrator privilege (a symlink would)
        subprocess.run(["cmd", "/c", "mklink", "/J", link,
                        os.path.abspath(content_dir)], check=True,
                       stdout=subprocess.DEVNULL)
        print("  junction: %s -> %s" % (link, content_dir))
    return uproj


def run(cmd, logfile, env=None):
    print("  $", " ".join('"%s"' % c if " " in c else c for c in cmd[:3]), "...")
    with open(logfile, "w", encoding="utf-8", errors="replace") as lf:
        proc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, env=env)
    return proc.returncode


def tail_tag(logfile, tag):
    try:
        with open(logfile, encoding="utf-8", errors="replace") as f:
            for line in f:
                if tag in line:
                    print("   ", line.split(tag, 1)[1].rstrip())
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description="Convert a UE level to GLB")
    ap.add_argument("--content", required=True,
                    help=r"asset pack folder (e.g. D:\projects\MyPack)")
    ap.add_argument("--level", required=True,
                    help="level path (e.g. /Game/MyPack/Levels/L_X)")
    ap.add_argument("--out", default=None, help="output folder (default: ./out)")
    ap.add_argument("--tex", type=int, default=1024,
                    help="longest texture side in the GLB (default 1024)")
    ap.add_argument("--jpeg-quality", type=int, default=92)
    ap.add_argument("--flip-green", choices=["0", "1"], default="0",
                    help="invert the normal map green channel (default 0)")
    ap.add_argument("--lights", choices=["0", "1"], default="1",
                    help="export level lights (default 1)")
    ap.add_argument("--sun-scale", default="1.0",
                    help="multiplier on directional light intensity (default 1.0)")
    ap.add_argument("--empty-slot", choices=["first", "keep"], default="first",
                    help="material slot left empty in UE: use the mesh's first "
                         "valid material (first) or leave it unassigned as UE does (keep)")
    ap.add_argument("--unreal", default=None, help="path to UnrealEditor-Cmd.exe")
    ap.add_argument("--blender", default=None, help="path to blender.exe")
    ap.add_argument("--skip-ue", action="store_true",
                    help="reuse an existing UE export")
    ap.add_argument("--verify", action="store_true",
                    help="export UE's native reference and compare node by node")
    args = ap.parse_args()

    out = os.path.abspath(args.out or os.path.join(HERE, "out"))
    os.makedirs(out, exist_ok=True)
    proj = os.path.join(HERE, "UEProj")

    ue = find_unreal(args.unreal)
    bl = find_blender(args.blender)
    if not args.skip_ue and not ue:
        sys.exit("Unreal Editor not found -- pass --unreal")
    if not bl:
        sys.exit("Blender not found -- pass --blender")
    print("Unreal :", ue)
    print("Blender:", bl)
    print("Output :", out)

    env = dict(os.environ, UE2G_LEVEL=args.level, UE2G_OUT=out)

    if not args.skip_ue:
        print("\n[1/3] exporting from Unreal (slow: it builds the mesh DDC)")
        uproj = make_project(proj, args.content)
        rc = run([ue, uproj, "-run=pythonscript",
                  "-script=%s" % os.path.join(SCRIPTS, "ue_export.py"),
                  "-unattended", "-nosplash", "-stdout", "-FullStdOutLogOutput"],
                 os.path.join(out, "export.log"), env)
        tail_tag(os.path.join(out, "export.log"), "[UE2G] ")
        if not os.path.exists(os.path.join(out, "scene.json")):
            sys.exit("UE export failed (rc=%s) -- see out/export.log" % rc)

    print("\n[2/3] rebuilding in Blender and writing the GLB")
    glb = os.path.join(out, os.path.basename(args.level) + ".glb")
    rc = run([bl, "--background", "--factory-startup", "--python",
              os.path.join(SCRIPTS, "blender_build.py"), "--",
              "--src", out, "--out", glb, "--tex", str(args.tex),
              "--jpeg-quality", str(args.jpeg_quality),
              "--flip-green", args.flip_green,
              "--lights", args.lights,
              "--sun-scale", str(args.sun_scale),
              "--empty-slot", args.empty_slot],
             os.path.join(out, "blender.log"))
    tail_tag(os.path.join(out, "blender.log"), "[B2G] ")
    if not os.path.exists(glb):
        sys.exit("Blender build failed (rc=%s) -- see out/blender.log" % rc)

    print("\n[3/3] summary")
    subprocess.run([sys.executable, os.path.join(SCRIPTS, "inspect_glb.py"), glb])

    if args.verify:
        print("\n[extra] UE native reference + node-by-node comparison")
        run([ue, os.path.join(proj, "UE2GodotExport.uproject"), "-run=pythonscript",
             "-script=%s" % os.path.join(SCRIPTS, "ue_ref_level.py"),
             "-unattended", "-nosplash", "-stdout"],
            os.path.join(out, "ref_level.log"), env)
        ref = os.path.join(out, "ref_level",
                           os.path.basename(args.level) + ".gltf")
        if os.path.exists(ref):
            subprocess.run([sys.executable,
                            os.path.join(SCRIPTS, "compare_to_ue.py"), glb, ref])
        else:
            print("  reference not generated -- see out/ref_level.log")

    print("\nOK ->", glb)


if __name__ == "__main__":
    main()
