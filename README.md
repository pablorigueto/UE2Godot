# ue2godot — Unreal → GLB (Godot) converter

Converts an **entire Unreal Engine level** into a single `.glb` ready for Godot:
geometry, transforms, PBR materials and textures.

Built for **uncooked** asset packs (`.uasset`/`.umap` with no `.uexp`), which is
the case for `TheLightHouseOfNoReturn`. In those packs the mesh is not in render
format — it lives in editor *bulk data* (`MeshDescription`). That is exactly why
umodel/CUE4Parse and most `.uasset` converters cannot extract anything useful
from them.

The approach here is to not reimplement the `.uasset` parser: **Unreal Engine
itself** reads the assets, running headless as a Python commandlet, and
**Blender** rebuilds the scene and writes the GLB.

## Usage

```bash
python convert.py --content D:\projects\TheLightHouseOfNoReturn \
                  --level   /Game/TheLightHouseOfNoReturn/Levels/L_Overview \
                  --tex 1024 --verify
```

| flag | effect |
|---|---|
| `--content` | asset pack folder |
| `--level` | level path under `/Game/...` |
| `--out` | output folder (default `./out`) |
| `--tex` | longest texture side in the GLB (default `1024`) |
| `--jpeg-quality` | JPEG quality (default `92`) |
| `--flip-green` | invert the normal map green channel (default `0`) |
| `--lights` | export level lights (default `1`) |
| `--sun-scale` | multiplier on directional light intensity (default `1.0`) |
| `--empty-slot` | empty UE material slot: `first` (use the mesh's first valid material) or `keep` (leave unassigned, as UE does) |
| `--skip-ue` | reuse an existing UE export (iterate on the Blender side only) |
| `--verify` | export UE's native glTF reference and compare node by node |

Unreal and Blender are located automatically; override with
`--unreal` / `--blender`.

## How it works

```
.uasset/.umap
    │
    │  ue_export.py   (UnrealEditor-Cmd -run=pythonscript)
    ▼
out/meshes/*.fbx      one per unique StaticMesh, LOD0, all UV sets
out/textures/*.png    source resolution (here: 4K/8K)
out/scene.json        world transform of every component + materials/params
    │
    │  blender_build.py   (blender --background)
    ▼
out/<Level>.glb       rebuilt scene, PBR metallic/roughness, 1K textures
```

### Axis conversion

This is where homemade converters usually go wrong. Here the change of basis was
**measured, not assumed** (`scripts/verify_axes.py`): comparing the imported FBX
against the glTF that UE 5.8 itself writes for the same mesh, the error drops to
exactly zero with

```
v_fbx = diag(-1, 1, 1) · v_unreal     (UE's FBX exporter mirrors on X)
```

Applying `Rz(180°)` to the mesh makes the effective basis `S = diag(1,-1,1)`, so
each object's world matrix is the conjugation

```
M_blender = S · M_unreal · S
```

After the glTF exporter's `Z-up → Y-up` step the result is
`gltf = (ue_x, ue_z, ue_y)` — exactly the convention of Unreal's own glTF
exporter.

UE's FBX already arrives in **meters**; only the `scene.json` translations (which
are in cm) get the `0.01` factor.

### Materials

The pack's `MI_*` instances follow the `B` / `N` / `ORM` (+ `E`) convention, which
maps directly onto glTF:

| Unreal | glTF |
|---|---|
| `Base Texture` / `Base Color Texture` | `baseColorTexture` |
| `Base Texture Color` / `Overall Color` | `baseColorFactor` |
| `ORM Texture` R / G / B | `occlusionTexture` / roughness / metallic |
| `Normal Texture` (+ `Normal Value`) | `normalTexture` |
| `Emiss Texture/Color/Value` | `emissiveTexture` / `emissiveFactor` |
| `BLEND_MASKED` (+ `opacity_mask_clip`) | `alphaMode: MASK` + `alphaCutoff` |
| `BLEND_TRANSLUCENT` (+ `Opacity Value`) | `alphaMode: BLEND` |
| `two_sided` | `doubleSided` |

Two Blender 4.2+/5.x quirks the script works around:

* the glTF exporter **ignores `blend_method`** — it infers `alphaMode: MASK` from
  the *node graph*, looking for `alpha > cutoff`. Without a `Math > GREATER_THAN`
  node feeding Alpha, every masked material is exported as `BLEND` (expensive and
  wrong in Godot);
* `occlusionTexture` is only exported through the `glTF Material Output` node
  group, fed by the ORM's R channel.

`Roughness Value` in UE is a multiplier that can exceed 1, while glTF's
`roughnessFactor` is capped at 1 — so it is baked into a per-material copy of the
ORM's G channel instead of becoming a node the exporter would drop.

### Normal maps: the blue channel must be rebuilt

This was the most destructive problem in the conversion, and it is not obvious.
Unreal compresses normal maps as **BC5**, which stores only R and G — Z is
rebuilt in the shader (`UnpackNormalMap`). The blue channel of the exported PNGs
is therefore **not trustworthy**:

```
T_Concreate_Plaster_01_N  (lighthouse tower)   B: mean = 0.41
T_Brick_Single_1_N                             B: mean = 0.47
(a valid normal map has B ≈ 0.9–1.0)
```

`B < 0.5` decodes to a **negative Z**: the normal points into the surface. In
Godot and glTF viewers that shows up as noise — white speckles scattered over the
surface. glTF requires a full RGB normal map, so the script derives Z the same
way UE does:

```
nx = R*2-1 ;  ny = G*2-1
B  = sqrt(clamp(1 - nx² - ny², 0, 1)) * 0.5 + 0.5
```

Applied to every normal map: it repairs the invalid ones and normalizes the rest.

### Alpha masks and downscaling

Shrinking 4K → 1K keeps overall coverage but explodes the amount of
**intermediate** alpha (on the fabric texture: 27% → 41% of pixels between 0.05
and 0.95). Since `alphaMode: MASK` is a hard cutoff, that grey band turns into
stippled, ragged cutouts.

So for every mask texture the script measures opaque coverage at the **source**
resolution, then after the downscale bisects for the threshold that reproduces
that same coverage and binarizes the alpha. Result: the same silhouette area as
the 4K original, with no stippling.

Measured on this pack (`intermediate alpha` before → after):

```
T_Fabrics_01_B          0.2661 → 0.0000    coverage 0.5670 → 0.5682
T_Fir_Tree_Leaves_01_B  0.0762 → 0.0000    coverage 0.2668 → 0.2668
T_Pines_Leaves_01_B     0.2379 → 0.0000    coverage 0.2481 → 0.2483
```

### Lights

Measured, not guessed: Blender 5.x's glTF exporter **ignores `light.energy`** and
uses the *Strength* of the light's emission node — `lux = Strength × 683`.
Without setting that node, every directional light was exported at **683 lux**
regardless of its value (68× this scene's real 10 lux), blowing out the scene in
any viewer.

UE gives directional lights in lux and point/spot in candela; glTF uses the same
units, so it is a direct pass-through. Use `--sun-scale` to boost it and
`--lights 0` to export no lights at all (UE compensates with auto-exposure +
SkyAtmosphere, which glTF does not carry — in Godot you will normally want your
own `WorldEnvironment`).

### Textures

Resized to `--tex` (1024 by default). PNG for normal maps and anything needing
alpha, JPEG for the rest — without that split, 140 1K textures would push the GLB
past 300 MB.

## Validation

`--verify` exports the same level with Unreal's **native** glTF exporter and
compares node by node (`scripts/compare_to_ue.py`). On `L_Overview`:

```
nodes mine=104  ref=114  comparable=104
position error (meters):  mean=0.000000  median=0.000000  max=0.000000
nodes with error < 1mm: 104/104 (100.0%)
meshes: mine=102 ref=102 | materials: mine=54 ref=54
```

The 10 nodes present only in the reference are SkyAtmosphere, VolumetricCloud,
ExponentialHeightFog, RuntimeVirtualTextureVolume, the camera, TextRender and the
Brush — none of them has geometry or a glTF equivalent.

## Helper tools (`scripts/`)

| script | purpose |
|---|---|
| `verify_axes.py` | measures the change of basis against UE's native glTF |
| `compare_to_ue.py` | compares the GLB with UE's reference, node by node |
| `inspect_glb.py` | texture/alphaMode coverage per material in the GLB |
| `check_tex_size.py` | reads the real resolution of every image embedded in the GLB |
| `render_check.py` | reimports the GLB and renders it (`--target`, `--only`) |
| `ue_validate.py` | dumps per-actor bounds and the master material graphs |
| `ue_ref_level.py` | exports the reference level with UE's native glTF exporter |

## Known limitations

* **RGB detail mask.** 5 materials (`MI_Bark_02`, `MI_Brick_01`, `MI_Bricks_01`, …)
  use the `MM_Nature_Assets` master, which blends tinted layers driven by an RGB
  mask texture, plus a world-space moss top layer on the rocks. glTF cannot
  express that; the base texture is used instead. Matching it 1:1 would require
  *baking* those materials.
* **No glTF equivalent:** SkyAtmosphere, VolumetricCloud, ExponentialHeightFog,
  Runtime Virtual Textures and the foliage wind.
* Foliage **subsurface** (`Subssurface Value`) is not exported.
* `SM_Conifer_01` has a material slot that is **empty in UE** (23k triangles: the
  trunk and branches). UE draws it with the default grey material; by default this
  converter falls back to the mesh's first valid material instead, which is a
  repair rather than a faithful reproduction — use `--empty-slot keep` to match UE.
* LOD0 only (the goal is maximum fidelity; LODs can be generated in Godot).
