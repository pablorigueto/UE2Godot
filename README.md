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

A running theme below: a headless Unreal will happily *report success* while
writing nothing. Material baking, scene captures, the base-colour g-buffer and
`render_weightmap` all return cleanly and produce empty images, and its glTF
exporter writes landscapes with zero height without a warning. Everything here is
checked against the pixels or the vertices, never against the return value.

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
| `--extras` | bake landscape / sky / water with UE's own exporters (default `1`) |
| `--bake-size` | resolution of those bakes (default `512`) |
| `--skip-ue` | reuse an existing UE export (iterate on the Blender side only) |
| `--verify` | export UE's native glTF reference and compare node by node |

Blender-side flags, passed straight through to `blender_build.py`:

| flag | effect |
|---|---|
| `--land-tiles` | tiles across the terrain for the baked layer blend (default `4` → sixteen 1K textures) |
| `--land-layer` | which `MM_Landscape` layer to fall back to when no weights were sampled (default `1`) |
| `--water-extent` | ocean plane size, as a multiple of the island's own extent (default `1.2`; `0` keeps UE's 12.5 km) |
| `--sky` | keep `BP_Sky_Sphere` (default `0`, see below) |
| `--top-layer` | approximate `MM_Nature_Assets`' world-space top layer (default `0`, see Known limitations) |

Unreal and Blender are located automatically; override with
`--unreal` / `--blender`.

## How it works

```
.uasset/.umap
    │
    │  ue_export.py         (UnrealEditor-Cmd -run=pythonscript)
    ▼
out/meshes/*.fbx            one per unique StaticMesh, LOD0, all UV sets
out/textures/*.png          source resolution (here: 4K/8K)
out/scene.json              world transform of every component + materials/params
    │
    │  ue_bake_extras.py    (… -AllowCommandletRendering)
    ▼
out/extras/landscape.fbx    the terrain, WITH its heightfield
out/extras/weight_*.f32     the painted weight of each landscape layer
out/extras/extras.gltf      what UE's own glTF exporter can bake (the sky)
    │
    │  blender_build.py     (blender --background)
    ▼
out/<Level>.glb             rebuilt scene, PBR metallic/roughness, 1K textures
out/materials.json          what glTF cannot carry, for the target engine
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

### Half of a material is in the master, not the instance

A `MaterialInstanceConstant` only stores what it **overrides**. Reading just those
values loses every parameter left at its default — which is how the lighthouse
windows went missing: `MI_Lighthouse_Glass_01` overrides nothing about opacity, so
the only alpha left was the base texture's, which is 0 across the whole image.

So `_read_master_defaults()` reads the master's declared defaults first and the
MIC chain on top. But merged values alone are a trap in the other direction, and
this pack triggers it twice:

| master | default | what merging alone produced |
|---|---|---|
| `MM_Props` | `Emiss Texture` = `T_Brick_Single_1_B`, `Emiss Value` = 1.0 | every prop — boat, pier, barrel, the lighthouse woodwork — glowing orange brick |
| `MM_Nature_Assets` | `Top Layer Texture` = `T_Dirt_01_B` | a cap of dirt on metal, food and tree bark |

The defaults are therefore also kept **separately**, under `master_defaults`, and
`overrides()` answers the question that actually matters: did the *instance* ask
for this, or is it just the master's default? Emissive and the top layer are only
built when the instance asked.

### Material slots are matched by name, not by index

UE's FBX exporter does not always write material slots in the order the asset
reports them. `SM_Fir_Tree_03` comes out `[MI_Bark_02, MI_Fir_Leaves_Set_01]`
while `static_materials` says `[MI_Fir_Leaves_Set_01, MI_Bark_02]`. Assigning by
index put the **bark** texture on the leaf cards, and a leaf card holding a bark
texture reads as a big orange scale — about a third of the conifers on this level
turned into orange cones. `SM_Fir_Tree_01`, `_05` and the props all match, so
spot-checking a few meshes does not catch it.

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

### The landscape

`LandscapeComponent` is not a `StaticMeshComponent`, so the terrain never reaches
the main export path at all. Getting it out took two separate discoveries, both
of which look like dead ends first.

**Geometry — the glTF exporter writes it flat.** UE's own glTF exporter emits
every `LandscapeComponent` as a `0.63 × 0 × 0.63 m` patch: a 63×63 grid of quads
with *zero height*. That is not an artefact of this converter — the untouched
reference export does the same. A flat plate at z = 1 m under a sea at z = 2.3 m
is why the first attempts showed "transparent ground": you were looking at the
whole terrain through the water. The **FBX level exporter** does carry the
heightfield — one 508,032-face mesh spanning z 0 → 27.9 m, matching the actor
bounds Unreal reports — so the geometry comes from there.

**Material — the weights are reachable, but only from the CPU.** `MM_Landscape`
blends five layers, and the blend lives in per-component weightmaps.
`LandscapeComponent.weightmap_textures` is not exposed to Python, and every
render-based route fails silently in a commandlet:

| route | result |
|---|---|
| glTF export, `bake_material_inputs = USE_MESH_DATA` | 0 textures, `baseColorFactor [0,0,0]` |
| same, `SIMPLE` | 0 textures |
| `SCS_BASE_COLOR` g-buffer via SceneCapture2D | constant |
| `unlit_viewmode = CAPTURE` | identical to lit |
| orthographic capture of the isolated terrain | black, even under a 100× sun |
| `ALandscape.render_weightmap` | returns `True`, writes an all-zero image |

What does work is the CPU query
`LandscapeComponent.editor_get_paint_layer_weight_by_name_at_location`. It is
answered **per component**, and only inside that component's own 63 m patch —
asking component 0 about the whole landscape returns 0 everywhere except its own
corner, which is exactly what made this look impossible at first. Walking the
grid component by component gives the real painted weights (here 252×252 per
layer, `Layer_01` covering 99.3% with the path, beach and grass painted on top).

glTF allows one base-colour texture per material, so the blend cannot stay a
material graph — it is resolved into pixels:

```
colour = Σ(weight_i · layer_i(world · uv_scale_i)) / Σ(weight_i)
```

baked into `--land-tiles`² textures (16 × 1024 px over 504 m ≈ 8 px/m). Both the
weight lookup and the layer sampling are **bilinear**: nearest-neighbour turns
each 2 m weight sample into a 16×16 block of texels and the terrain comes out
visibly squared.

`UV Scale` is not metres per repeat — `MM_Landscape` feeds `LandscapeLayerCoords`,
whose mapping scale multiplies coordinates counted in *quads*. The quad size is
measured from the mesh (a component has (quads+1)² vertices) rather than assumed:
1.00 m here, so `UV Scale` 0.1 means one repeat every 10 m.

### Sky and ocean

`BP_Sky_Sphere` is **excluded** (`--sky 1` keeps it). Its `SM_SkySphere` is 41 m
across with scale 1 in the saved level — the Blueprint's construction script is
what blows it up to sky size, and that never runs in a commandlet. Exporting it
drops a 41 m ball of night sky in the middle of the island. Godot takes its sky
from a `WorldEnvironment` anyway.

The ocean is `/Engine/BasicShapes/Plane` scaled to **12,550 m**. Faithful, but
unusable: the island is ~500 m, so every viewer opens on an empty sheet of water
with the level a speck in the middle. It is scaled in X/Y only, about its own
centre, so the water line stays exactly where Unreal put it (`--water-extent 0`
keeps UE's size).

Its material uses `MSM_SINGLE_LAYER_WATER`, which UE's own glTF exporter refuses
— *"Unsupported shading model (SingleLayerWater) … will export as Default Lit"* —
handing back a flat 0.85 grey, i.e. 12.5 km of white. It is rebuilt from the
shading model instead: a smooth transmissive surface tinted by the material's own
colour parameters where it exposes any.

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

`L_Showcase` is 6× the level: 721 nodes, 631 plain meshes, 9,917 foliage
instances, 54 lights, 94 unique meshes. Comparing it by *name* is misleading —
the reference names every foliage instance individually while this converter
groups them under one component, and it collapses `Actor.Component` to `Actor`,
so a Blueprint's ten components all match its single reference node. Compared by
**position** instead, which also covers the instances that no name reaches:

```
mine 10,548 mesh nodes -> reference : 10,548 within 1 mm  (100.00%)
reference 10,612        -> mine     : 10,548 within 1 mm  ( 99.40%)
lights: mine 52 = reference 52  (26 spot / 2 directional / 24 point)
```

The 64 reference nodes with no match are the `LandscapeComponent`s, which the
reference exports flat and this converter takes from the FBX instead.

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
| `ue_bake_extras.py` | landscape geometry + layer weights + whatever UE can bake |

## What glTF cannot carry — `out/materials.json`

One family of materials does not survive the format at all. `MM_Nature_Assets`
projects its maps by **world position** (triplanar) and blends a top layer — moss
on the rocks, sand on `MI_Rock_01_A`, dirt on `MI_Rock_01_Wtih_Dirt` — onto
up-facing surfaces. glTF addresses textures by UV and gives a material one
base-colour texture, so there is no honest place to put either.

Pinning the top layer to the mesh's UVs was tried and rejected: the moss magnifies
into flat green slabs, visibly worse than leaving the rock plain (`--top-layer 1`
turns the approximation back on). The landscape's five layers *can* be baked
because the terrain has a sane planar parameterisation; a rock's does not.

So the data goes out beside the GLB. `materials.json` lists, per material in the
file, which master it derives from and which texture file feeds which parameter —
and, separately, which values are only the master's defaults. An engine that has
triplanar can rebuild these properly from it.

## Known limitations

* **`MM_Nature_Assets` is triplanar.** Its world-space projection and top layer are
  emitted to `materials.json` rather than baked (see above). The rocks therefore
  carry their base texture only: no moss, no sand cap.
* **Landscape layer blend is baked, not live.** The five layers are resolved into
  `--land-tiles`² textures at ~8 px/m. Painting the terrain in Unreal afterwards
  means re-running the export.
* **No glTF equivalent:** SkyAtmosphere, VolumetricCloud, ExponentialHeightFog,
  Runtime Virtual Textures and the foliage wind.
* Foliage **subsurface** (`Subssurface Value`) is not exported.
* `SM_Conifer_01` has a material slot that is **empty in UE** (23k triangles: the
  trunk and branches). UE draws it with the default grey material; by default this
  converter falls back to the mesh's first valid material instead, which is a
  repair rather than a faithful reproduction — use `--empty-slot keep` to match UE.
* LOD0 only (the goal is maximum fidelity; LODs can be generated in Godot).
