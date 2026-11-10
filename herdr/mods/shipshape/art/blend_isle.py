"""Builds an island in Blender from what render_world.py exported (terrain,
ground colours, what stands where) and renders it, by day and by lamplight.

    blender -b -P blend_isle.py -- STAGE_DIR OUT_DIR NAME [NAME...]
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
from blend_ship import ball, block, obj, planked, principled, rod
from shipspec import EL, PXI, SUN


def P(x, y, z):
    """Chart axes (x east, y up, z south) to Blender."""
    return Vector((x, -z, y))


def terrain(ground, colour, R):
    n = 300
    idx = np.linspace(0, ground.shape[0] - 1, n).round().astype(int)
    g = ground[np.ix_(idx, idx)]
    col = colour[np.ix_(idx, idx)]
    ax = np.linspace(-R, R, n)
    bm = bmesh.new()
    layer = bm.loops.layers.color.new("col")
    vs = [
        [bm.verts.new(P(ax[j], max(float(g[i, j]), -0.03), ax[i])) for j in range(n)]
        for i in range(n)
    ]
    for i in range(n - 1):
        for j in range(n - 1):
            if max(g[i, j], g[i + 1, j], g[i, j + 1], g[i + 1, j + 1]) < -0.002:
                continue
            f = bm.faces.new([vs[i][j], vs[i + 1][j], vs[i + 1][j + 1], vs[i][j + 1]])
            f.smooth = True
            corners = ((i, j), (i + 1, j), (i + 1, j + 1), (i, j + 1))
            for loop, (a, b) in zip(f.loops, corners, strict=False):
                loop[layer] = (*col[a, b], 1)
    for v in [v for v in bm.verts if not v.link_faces]:
        bm.verts.remove(v)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    me = bpy.data.meshes.new("land")
    bm.to_mesh(me)
    bm.free()
    m = bpy.data.materials.new("land")
    m.use_nodes = True
    nt = m.node_tree
    b = nt.nodes["Principled BSDF"]
    b.inputs["Roughness"].default_value = 0.95
    attr = nt.nodes.new("ShaderNodeVertexColor")
    attr.layer_name = "col"
    nt.links.new(attr.outputs["Color"], b.inputs["Base Color"])
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 140
    noise.inputs["Detail"].default_value = 3
    bump = nt.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.08
    nt.links.new(noise.outputs["Fac"], bump.inputs["Height"])
    nt.links.new(bump.outputs[0], b.inputs["Normal"])
    obj("land", me, m)


def lump(centre, r, mat, squash=1.0, seed=0):
    """A rough ball: a tree's crown, a boulder."""
    rnd = random.Random(seed)
    bm = bmesh.new()
    bmesh.ops.create_icosphere(bm, subdivisions=2, radius=r)
    for v in bm.verts:
        v.co *= 1 + rnd.uniform(-0.16, 0.16)
        v.co.z *= squash
    me = bpy.data.meshes.new("lump")
    bm.to_mesh(me)
    bm.free()
    o = obj("lump", me, mat, True)
    o.location = centre
    return o


def cone(base, r, h, mat, sides=9):
    bm = bmesh.new()
    bmesh.ops.create_cone(bm, cap_ends=True, segments=sides, radius1=r, radius2=0.0, depth=h)
    me = bpy.data.meshes.new("cone")
    bm.to_mesh(me)
    bm.free()
    o = obj("cone", me, mat)
    o.location = base + Vector((0, 0, h / 2))
    return o


def roof(centre, w, d, h, mat):
    """A pitched roof: ridge along x."""
    bm = bmesh.new()
    v = [
        bm.verts.new(p)
        for p in ((-w, -d, 0), (w, -d, 0), (w, d, 0), (-w, d, 0), (-w, 0, h), (w, 0, h))
    ]
    for ids in ((0, 1, 5, 4), (2, 3, 4, 5), (1, 2, 5), (3, 0, 4), (0, 3, 2, 1)):
        bm.faces.new([v[i] for i in ids])
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    me = bpy.data.meshes.new("roof")
    bm.to_mesh(me)
    bm.free()
    o = obj("roof", me, mat)
    o.location = centre
    return o


def lamp_light(at, energy):
    light = bpy.data.lights.new("lamp", "POINT")
    light.energy = energy
    light.color = (1.0, 0.62, 0.25)
    light.shadow_soft_size = 0.03
    o = bpy.data.objects.new("lamp", light)
    o.location = at
    bpy.context.scene.collection.objects.link(o)


def build(name, stage):
    data = np.load(os.path.join(stage, name + ".npz"))
    info = json.load(open(os.path.join(stage, name + ".json")))
    R = info["R"]
    terrain(data["ground"], data["colour"], R)
    rnd = random.Random(len(name) * 7 + 3)
    M = dict(
        trunk=principled("trunk", (0.16, 0.09, 0.04), 0.9),
        leaf=[
            principled(f"leaf{i}", c, 0.85)
            for i, c in enumerate(((0.10, 0.30, 0.07), (0.15, 0.38, 0.08), (0.22, 0.44, 0.10)))
        ],
        pine=[
            principled(f"pine{i}", c, 0.85)
            for i, c in enumerate(((0.045, 0.19, 0.10), (0.065, 0.25, 0.11)))
        ],
        palm=principled("palm", (0.12, 0.36, 0.08), 0.7),
        wall=principled("wall", (0.78, 0.70, 0.55), 0.85),
        roofs=[
            principled(f"roof{i}", c, 0.7)
            for i, c in enumerate(((0.52, 0.13, 0.07), (0.60, 0.36, 0.10), (0.16, 0.22, 0.34)))
        ],
        stone=principled("stone", (0.36, 0.34, 0.31), 0.9),
        white=principled("white", (0.85, 0.83, 0.78), 0.6),
        red=principled("red", (0.62, 0.07, 0.05), 0.5),
        wood=planked("wood", (0.30, 0.17, 0.08), (0.09, 0.05, 0.02), 40, 1, 0.8),
        dark=principled("dark", (0.01, 0.008, 0.006), 0.9),
        iron=principled("iron", (0.03, 0.03, 0.035), 0.45, 0.9),
        glass=principled("glass", (1, 0.8, 0.45), 0.3, emit=(1.0, 0.66, 0.26), strength=5.0),
        lamp=principled("lamp", (1, 0.8, 0.5), 0.4, emit=(1.0, 0.62, 0.22), strength=40),
        beacon=principled("beacon", (1, 0.9, 0.6), 0.4, emit=(1.0, 0.78, 0.36), strength=110),
        lava=principled("lava", (1, 0.3, 0.05), 0.5, emit=(1.0, 0.24, 0.03), strength=30),
        rock=principled("rock", (0.20, 0.19, 0.18), 0.9),
    )
    for k, o in enumerate(info["objs"]):
        kind, x, z = o["kind"], o["x"], o["z"]
        g = o.get("g", 0.0)
        if kind == "tree":
            s = o["s"]
            rod(P(x, g - 0.01, z), P(x, g + 0.07 * s, z), 0.012 * s, 0.009 * s, M["trunk"], 6)
            for i in range(3):
                off = (
                    Vector(
                        (
                            rnd.uniform(-0.025, 0.025),
                            rnd.uniform(-0.025, 0.025),
                            rnd.uniform(0, 0.03),
                        )
                    )
                    * s
                )
                lump(
                    P(x, g + (0.085 + 0.012 * i) * s, z) + off,
                    (0.05 - 0.008 * i) * s,
                    rnd.choice(M["leaf"]),
                    0.85,
                    k * 3 + i,
                )
        elif kind == "pine":
            s = o["s"]
            rod(P(x, g - 0.01, z), P(x, g + 0.05 * s, z), 0.009 * s, 0.007 * s, M["trunk"], 6)
            mat = rnd.choice(M["pine"])
            for i in range(4):
                cone(
                    P(x, g + (0.03 + 0.045 * i) * s, z),
                    (0.05 - 0.009 * i) * s,
                    (0.085 - 0.006 * i) * s,
                    mat,
                )
        elif kind == "palm":
            s = o["s"]
            lean = Vector((rnd.uniform(-0.04, 0.04), rnd.uniform(-0.04, 0.04), 0)) * s
            base, mid, top = (
                P(x, g - 0.01, z),
                P(x, g + 0.08 * s, z) + lean * 0.6,
                P(x, g + 0.16 * s, z) + lean,
            )
            rod(base, mid, 0.011 * s, 0.009 * s, M["trunk"], 6)
            rod(mid, top, 0.009 * s, 0.007 * s, M["trunk"], 6)
            for i in range(7):
                a = i * 2 * math.pi / 7 + rnd.uniform(-0.2, 0.2)
                out = Vector((math.cos(a), math.sin(a), 0))
                p1 = top + out * 0.045 * s + Vector((0, 0, 0.012 * s))
                p2 = top + out * 0.1 * s + Vector((0, 0, -0.03 * s))
                rod(top, p1, 0.006 * s, 0.014 * s, M["palm"], 5)
                rod(p1, p2, 0.014 * s, 0.002 * s, M["palm"], 5)
        elif kind == "house":
            w, d, wall, rf = o["w"], o["d"], o["wall"], o["roof"]
            block(P(x, g + wall / 2 - 0.01, z), (w, d, wall / 2 + 0.01), M["wall"], 0.004)
            roof(P(x, g + wall, z), w + 0.012, d + 0.012, rf, M["roofs"][o["tint"] % 3])
            for i in (-1, 1):
                block(
                    P(x + i * w * 0.5, g + wall * 0.55, z + d + 0.001),
                    (w * 0.17, 0.002, wall * 0.2),
                    M["glass"],
                    0,
                )
            block(
                P(x, g + wall * 0.32, z + d + 0.001), (w * 0.13, 0.002, wall * 0.32), M["dark"], 0
            )
            rod(
                P(x + w * 0.5, g + wall + rf * 0.3, z - d * 0.3),
                P(x + w * 0.5, g + wall + rf * 1.15, z - d * 0.3),
                0.012,
                0.012,
                M["stone"],
                6,
            )
        elif kind == "block":
            w, d, h = o["w"], o["d"], o["h"]
            block(P(x, g + h / 2 - 0.02, z), (w, d, h / 2 + 0.02), M["stone"], 0.004)
            long_x = w > d
            count = max(2, int((w if long_x else d) / 0.045))
            for i in range(count):
                t = (i + 0.5) / count * 2 - 1
                if i % 2 == 0:
                    block(
                        P(
                            x + (t * w if long_x else 0),
                            g + h + 0.014,
                            z + (0 if long_x else t * d),
                        ),
                        (
                            (w / count) * 0.9 if long_x else d,
                            d if long_x else (d / count) * 0.9,
                            0.016,
                        ),
                        M["stone"],
                        0,
                    )
            if long_x and z > -0.1:
                block(P(x, g + h * 0.3, z + d + 0.001), (0.045, 0.003, h * 0.3), M["dark"], 0)
        elif kind == "tower":
            r, h = o["r"], o["h"]
            rod(P(x, g - 0.03, z), P(x, g + h, z), r, r, M["stone"], 14)
            for i in range(8):
                a = i * math.pi / 4
                block(
                    P(x + math.cos(a) * r * 0.86, g + h + 0.014, z + math.sin(a) * r * 0.86),
                    (0.014, 0.014, 0.016),
                    M["stone"],
                    0,
                )
            block(P(x, g + h * 0.6, z + r + 0.001), (0.012, 0.003, 0.022), M["glass"], 0)
        elif kind == "lighthouse":
            r, h = o["r"], o["h"]
            bands = 5
            for i in range(bands):
                r0 = r * (1 - 0.3 * i / bands)
                r1 = r * (1 - 0.3 * (i + 1) / bands)
                rod(
                    P(x, g - 0.02 + h * 0.78 * i / bands, z),
                    P(x, g + h * 0.78 * (i + 1) / bands, z),
                    r0,
                    r1,
                    M["red"] if i % 2 == 0 else M["white"],
                    20,
                )
            rod(P(x, g + h * 0.78, z), P(x, g + h * 0.8, z), r * 1.05, r * 1.05, M["iron"], 20)
            rod(P(x, g + h * 0.8, z), P(x, g + h * 0.93, z), r * 0.5, r * 0.5, M["beacon"], 12)
            cone(P(x, g + h * 0.93, z), r * 0.68, h * 0.09, M["red"], 14)
            lamp_light(P(x, g + h * 0.86, z + r), 16)
            block(P(x, g + 0.05, z + r * 0.98), (0.018, 0.004, 0.035), M["dark"], 0)
        elif kind == "jetty":
            ln = o["length"]
            block(P(x, 0.028, z + ln / 2), (0.045, ln / 2, 0.006), M["wood"], 0)
            for i in range(int(ln / 0.12) + 1):
                for side in (-1, 1):
                    zz = z + min(ln, 0.04 + i * 0.12)
                    rod(
                        P(x + side * 0.04, -0.05, zz),
                        P(x + side * 0.04, 0.05, zz),
                        0.008,
                        0.008,
                        M["trunk"],
                        6,
                    )
            end = z + ln - 0.05
            rod(P(x, 0.03, end), P(x, 0.19, end), 0.006, 0.006, M["iron"], 6)
            ball(P(x, 0.205, end), 0.016, M["lamp"])
            lamp_light(P(x, 0.2, end + 0.03), 0.6)
        elif kind == "lava":
            rod(P(x, g - 0.02, z), P(x, g + 0.004, z), o["r"], o["r"], M["lava"], 18)
            lamp_light(P(x, g + 0.08, z), 3.0)
    # A few boulders along the shore.
    g = data["ground"]
    n = g.shape[0]
    shore = np.argwhere((g > 0.004) & (g < 0.02))
    for i, j in shore[
        np.random.default_rng(len(name)).choice(len(shore), size=min(14, len(shore)), replace=False)
    ]:
        x, z = -R + 2 * R * j / (n - 1), -R + 2 * R * i / (n - 1)
        lump(P(x, float(g[i, j]), z), rnd.uniform(0.018, 0.04), M["rock"], 0.7, int(i * 31 + j))
    return info


def stage(info, lamps_only):
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = 64 if lamps_only else 128
    sc.cycles.use_denoising = True
    sc.cycles.max_bounces = 4
    sc.render.film_transparent = True
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGBA"
    sc.render.resolution_x = info["w"]
    sc.render.resolution_y = info["h"]
    sc.render.resolution_percentage = 100
    try:
        sc.view_settings.view_transform = "AgX"
        sc.view_settings.look = "AgX - Medium High Contrast"
    except TypeError:
        pass
    cam = bpy.data.cameras.new("cam")
    cam.type = "ORTHO"
    cam.ortho_scale = info["w"] / PXI
    cam.clip_end = 100
    co = bpy.data.objects.new("cam", cam)
    sc.collection.objects.link(co)
    forward = Vector((0, math.cos(EL), -math.sin(EL)))
    co.location = Vector((0, 0, info["vc"] / math.cos(EL))) - forward * 30
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
    if not lamps_only:
        sun = bpy.data.lights.new("sun", "SUN")
        sun.energy = 4.8
        sun.color = (1.0, 0.86, 0.66)
        sun.angle = math.radians(5)
        so = bpy.data.objects.new("sun", sun)
        so.rotation_mode = "QUATERNION"
        so.rotation_quaternion = Vector((SUN[0], -SUN[2], SUN[1])).to_track_quat("Z", "Y")
        sc.collection.objects.link(so)
    bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=30)
    me = bpy.data.meshes.new("sea")
    bm.to_mesh(me)
    bm.free()
    sea = obj("sea", me, principled("sea", (0.02, 0.1, 0.14), 0.4))
    sea.is_shadow_catcher = True


def main():
    args = sys.argv[sys.argv.index("--") + 1 :]
    src, out, names = args[0], args[1], args[2:]
    os.makedirs(out, exist_ok=True)
    for name in names:
        for lamps in (False, True):
            bpy.ops.wm.read_factory_settings(use_empty=True)
            info = build(name, src)
            stage(info, lamps)
            bpy.context.scene.render.filepath = os.path.join(
                out, name + ("-L" if lamps else "") + ".png"
            )
            bpy.ops.render.render(write_still=True)


if __name__ == "__main__":
    main()
