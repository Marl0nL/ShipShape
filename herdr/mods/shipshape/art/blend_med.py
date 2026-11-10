"""Builds the Mediterranean in Blender from what medmap.py exported and renders
it, by day and by lamplight.

    blender -b -P blend_med.py -- STAGE_DIR OUT_DIR [scale]
"""

import json
import math
import os
import random
import sys

import bmesh
import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from blend_isle import P, cone, lamp_light, roof
from blend_ship import block, obj, principled, rod
from shipspec import EL, SUN


def terrain(g, col, wu, hu):
    nz, nx = g.shape
    xs = np.linspace(-wu / 2, wu / 2, nx, dtype=np.float32)
    zs = np.linspace(-hu / 2, hu / 2, nz, dtype=np.float32)
    X, Z = np.meshgrid(xs, zs)
    co = np.stack([X, -Z, np.maximum(g, -0.02)], axis=-1).reshape(-1, 3)
    idx = np.arange(nz * nx, dtype=np.int32).reshape(nz, nx)
    land = g > -0.003
    keep = land[:-1, :-1] | land[1:, :-1] | land[:-1, 1:] | land[1:, 1:]
    quads = np.stack([idx[:-1, :-1], idx[1:, :-1], idx[1:, 1:], idx[:-1, 1:]], axis=-1)[keep]
    me = bpy.data.meshes.new("land")
    me.vertices.add(len(co))
    me.vertices.foreach_set("co", co.ravel())
    me.loops.add(quads.size)
    me.loops.foreach_set("vertex_index", quads.ravel())
    me.polygons.add(len(quads))
    me.polygons.foreach_set("loop_start", np.arange(0, quads.size, 4, dtype=np.int32))
    me.polygons.foreach_set("loop_total", np.full(len(quads), 4, dtype=np.int32))
    me.polygons.foreach_set("use_smooth", np.ones(len(quads), dtype=bool))
    me.update()
    me.validate()
    attr = me.color_attributes.new("col", "FLOAT_COLOR", "POINT")
    rgba = np.concatenate([col.reshape(-1, 3), np.ones((nz * nx, 1), np.float32)], axis=1)
    attr.data.foreach_set("color", rgba.ravel())
    m = bpy.data.materials.new("land")
    m.use_nodes = True
    nt = m.node_tree
    b = nt.nodes["Principled BSDF"]
    b.inputs["Roughness"].default_value = 0.95
    a = nt.nodes.new("ShaderNodeVertexColor")
    a.layer_name = "col"
    nt.links.new(a.outputs["Color"], b.inputs["Base Color"])
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 26
    noise.inputs["Detail"].default_value = 6
    bump = nt.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.3
    bump.inputs["Distance"].default_value = 0.05
    nt.links.new(noise.outputs["Fac"], bump.inputs["Height"])
    nt.links.new(bump.outputs[0], b.inputs["Normal"])
    obj("land", me, m)


def town(M, x, z, g, rnd, houses, light):
    """A harbour town: a huddle of roofs and a light on the shore."""
    s = 0.62
    for _ in range(houses):
        a = rnd.uniform(0, 2 * math.pi)
        r = rnd.uniform(0.03, 0.05 + houses * 0.012)
        hx, hz = x + math.cos(a) * r * 1.5, z + math.sin(a) * r
        w, d, wall, rf = 0.05 * s * rnd.uniform(0.8, 1.2), 0.038 * s, 0.045 * s, 0.035 * s
        block(P(hx, g + wall / 2 - 0.01, hz), (w, d, wall / 2 + 0.01), M["wall"], 0.002)
        roof(P(hx, g + wall, hz), w + 0.006, d + 0.006, rf, rnd.choice(M["roofs"]))
        block(P(hx, g + wall * 0.55, hz + d + 0.001), (w * 0.3, 0.002, wall * 0.22), M["glass"], 0)
    h = 0.3 if light else 0.13
    r = 0.03 if light else 0.016
    for i in range(4):
        r0, r1 = r * (1 - 0.28 * i / 4), r * (1 - 0.28 * (i + 1) / 4)
        rod(
            P(x, g - 0.02 + h * 0.8 * i / 4, z),
            P(x, g + h * 0.8 * (i + 1) / 4, z),
            r0,
            r1,
            M["red"] if i % 2 == 0 and light else M["white"],
            14,
        )
    rod(
        P(x, g + h * 0.8, z),
        P(x, g + h * 0.93, z),
        r * 0.55,
        r * 0.55,
        M["beacon"] if light else M["lamp"],
        10,
    )
    cone(P(x, g + h * 0.93, z), r * 0.75, h * 0.1, M["red"], 12)
    lamp_light(P(x, g + h * 0.88, z + r * 1.2), 9 if light else 1.2)


def build(stage):
    info = json.load(open(os.path.join(stage, "med.json")))
    data = np.load(os.path.join(stage, "med.npz"))
    g, col = data["ground"], data["colour"]
    terrain(g, col, info["wu"], info["hu"])
    rnd = random.Random(11)
    M = dict(
        wall=principled("wall", (0.80, 0.74, 0.60), 0.85),
        roofs=[
            principled(f"roof{i}", c, 0.7)
            for i, c in enumerate(((0.55, 0.16, 0.08), (0.62, 0.38, 0.12), (0.50, 0.22, 0.12)))
        ],
        white=principled("white", (0.85, 0.83, 0.78), 0.6),
        red=principled("red", (0.62, 0.07, 0.05), 0.5),
        glass=principled("glass", (1, 0.8, 0.45), 0.3, emit=(1.0, 0.66, 0.26), strength=6.0),
        lamp=principled("lamp", (1, 0.8, 0.5), 0.4, emit=(1.0, 0.62, 0.22), strength=40),
        beacon=principled("beacon", (1, 0.9, 0.6), 0.4, emit=(1.0, 0.78, 0.36), strength=110),
        leaf=[
            principled(f"leaf{i}", c, 0.9)
            for i, c in enumerate(((0.07, 0.22, 0.06), (0.10, 0.27, 0.07), (0.13, 0.30, 0.08)))
        ],
    )
    h = info["home"]
    town(M, h["at"][0], h["at"][1], h["g"], rnd, 9, True)
    for t in info["towns"]:
        town(M, t["at"][0], t["at"][1], t["g"], rnd, 4, False)
    # Woods: dark clumps wherever it's green and low.
    arid, d_land = data["arid"], data["d_land"]
    nz, nx = g.shape
    ok = np.argwhere((arid < 0.35) & (g > 0.03) & (g < 0.16) & (d_land > 4))
    pick = ok[np.random.default_rng(2).choice(len(ok), size=min(2600, len(ok)), replace=False)]
    bm = bmesh.new()
    for i, j in pick:
        x = -info["wu"] / 2 + info["wu"] * j / (nx - 1)
        z = -info["hu"] / 2 + info["hu"] * i / (nz - 1)
        r = rnd.uniform(0.022, 0.05)
        made = bmesh.ops.create_icosphere(bm, subdivisions=1, radius=r)
        for v in made["verts"]:
            v.co *= 1 + rnd.uniform(-0.2, 0.2)
            v.co.z *= 0.55
            v.co += P(x, float(g[i, j]) + r * 0.25, z)
    me = bpy.data.meshes.new("woods")
    bm.to_mesh(me)
    bm.free()
    obj("woods", me, M["leaf"][1], True)
    return info


def stage_scene(info, lamps_only, scale):
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = 40 if lamps_only else 96
    sc.cycles.use_denoising = True
    sc.cycles.max_bounces = 3
    sc.render.film_transparent = True
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGBA"
    sc.render.resolution_x = info["w"]
    sc.render.resolution_y = info["h"]
    sc.render.resolution_percentage = scale
    try:
        sc.view_settings.view_transform = "AgX"
        sc.view_settings.look = "AgX - Medium High Contrast"
    except TypeError:
        pass
    cam = bpy.data.cameras.new("cam")
    cam.type = "ORTHO"
    cam.ortho_scale = info["wu"]
    cam.clip_end = 200
    co = bpy.data.objects.new("cam", cam)
    sc.collection.objects.link(co)
    forward = Vector((0, math.cos(EL), -math.sin(EL)))
    co.location = Vector((0, 0, info["vc"] / math.cos(EL))) - forward * 60
    co.rotation_euler = (math.pi / 2 - EL, 0, 0)
    sc.camera = co
    world = bpy.data.worlds.new("sky")
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    bg.inputs[0].default_value = (0.30, 0.46, 0.78, 1)
    bg.inputs[1].default_value = 0.0 if lamps_only else 0.85
    sc.world = world
    if lamps_only:
        sc.view_settings.exposure = 2.2
    else:
        sun = bpy.data.lights.new("sun", "SUN")
        sun.energy = 4.8
        sun.color = (1.0, 0.86, 0.66)
        sun.angle = math.radians(4)
        so = bpy.data.objects.new("sun", sun)
        so.rotation_mode = "QUATERNION"
        so.rotation_quaternion = Vector((SUN[0], -SUN[2], SUN[1])).to_track_quat("Z", "Y")
        sc.collection.objects.link(so)
    bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=60)
    me = bpy.data.meshes.new("sea")
    bm.to_mesh(me)
    bm.free()
    sea = obj("sea", me, principled("sea", (0.02, 0.1, 0.14), 0.4))
    sea.is_shadow_catcher = True


def main():
    args = sys.argv[sys.argv.index("--") + 1 :]
    src, out = args[0], args[1]
    scale = int(args[2]) if len(args) > 2 else 100
    os.makedirs(out, exist_ok=True)
    for lamps in (False, True):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        info = build(src)
        stage_scene(info, lamps, scale)
        bpy.context.scene.render.filepath = os.path.join(
            out, "med" + ("-L" if lamps else "") + ".png"
        )
        bpy.ops.render.render(write_still=True)
        if scale < 100:
            break


if __name__ == "__main__":
    main()
