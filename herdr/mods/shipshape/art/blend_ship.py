"""Builds a ship in Blender and renders it: every class, sail count and heading.

    blender -b -P blend_ship.py -- OUT_DIR JOB [JOB...]

A JOB is class-sails-heading (frigate-4-1), or class-L-heading for the
lamps-only pass (the ship in the dark, lit by its own lantern and windows).
"""

import math
import os
import sys

import bmesh
import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import shipspec
from shipspec import EL, HEADINGS, SHIPS, SUN, deck, frame, helm, plan, roll, trim
from shipspec import job as parse

PX = int(os.environ.get("SHIP_PX", shipspec.PX))  # the close-ups are rendered larger


def V(x, y, z):
    """Ship frame (x forward, y up, z to starboard) to Blender (z up)."""
    return Vector((x, -z, y))


# ------------------------------------------------------------ materials


def principled(name, color, rough=0.7, metallic=0.0, emit=None, strength=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*color, 1)
    b.inputs["Roughness"].default_value = rough
    b.inputs["Metallic"].default_value = metallic
    if emit:
        b.inputs["Emission Color"].default_value = (*emit, 1)
        b.inputs["Emission Strength"].default_value = strength
    return m


def planked(name, light, dark, scale, axis, rough=0.75):
    """Wood laid in planks: bands across one object axis, with a little grain."""
    m = principled(name, light, rough)
    nt = m.node_tree
    b = nt.nodes["Principled BSDF"]
    coord = nt.nodes.new("ShaderNodeTexCoord")
    sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    nt.links.new(coord.outputs["Object"], sep.inputs[0])
    mul = nt.nodes.new("ShaderNodeMath")
    mul.operation = "MULTIPLY"
    mul.inputs[1].default_value = scale
    nt.links.new(sep.outputs[axis], mul.inputs[0])
    fr = nt.nodes.new("ShaderNodeMath")
    fr.operation = "FRACT"
    nt.links.new(mul.outputs[0], fr.inputs[0])
    seam = nt.nodes.new("ShaderNodeMath")
    seam.operation = "LESS_THAN"
    seam.inputs[1].default_value = 0.1
    nt.links.new(fr.outputs[0], seam.inputs[0])
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 9
    noise.inputs["Detail"].default_value = 4
    nt.links.new(coord.outputs["Object"], noise.inputs["Vector"])
    grain = nt.nodes.new("ShaderNodeMixRGB")
    grain.inputs[1].default_value = (*light, 1)
    grain.inputs[2].default_value = (*[v * 0.72 for v in light], 1)
    nt.links.new(noise.outputs["Fac"], grain.inputs[0])
    mix = nt.nodes.new("ShaderNodeMixRGB")
    mix.inputs[2].default_value = (*dark, 1)
    nt.links.new(seam.outputs[0], mix.inputs[0])
    nt.links.new(grain.outputs[0], mix.inputs[1])
    nt.links.new(mix.outputs[0], b.inputs["Base Color"])
    bump = nt.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.35
    nt.links.new(seam.outputs[0], bump.inputs["Height"])
    nt.links.new(bump.outputs[0], b.inputs["Normal"])
    return m


def canvas(name, color):
    """Sailcloth: matte, seamed, and letting a little sun through."""
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    out = nt.nodes["Material Output"]
    b = nt.nodes["Principled BSDF"]
    b.inputs["Roughness"].default_value = 0.92
    uv = nt.nodes.new("ShaderNodeTexCoord")
    sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    nt.links.new(uv.outputs["UV"], sep.inputs[0])
    mul = nt.nodes.new("ShaderNodeMath")
    mul.operation = "MULTIPLY"
    mul.inputs[1].default_value = 5.0
    nt.links.new(sep.outputs[0], mul.inputs[0])
    fr = nt.nodes.new("ShaderNodeMath")
    fr.operation = "FRACT"
    nt.links.new(mul.outputs[0], fr.inputs[0])
    seam = nt.nodes.new("ShaderNodeMath")
    seam.operation = "LESS_THAN"
    seam.inputs[1].default_value = 0.05
    nt.links.new(fr.outputs[0], seam.inputs[0])
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 3.5
    stain = nt.nodes.new("ShaderNodeMixRGB")
    stain.inputs[1].default_value = (*color, 1)
    stain.inputs[2].default_value = (*[v * 0.8 for v in color], 1)
    nt.links.new(noise.outputs["Fac"], stain.inputs[0])
    mix = nt.nodes.new("ShaderNodeMixRGB")
    mix.inputs[2].default_value = (*[v * 0.6 for v in color], 1)
    nt.links.new(seam.outputs[0], mix.inputs[0])
    nt.links.new(stain.outputs[0], mix.inputs[1])
    nt.links.new(mix.outputs[0], b.inputs["Base Color"])
    tr = nt.nodes.new("ShaderNodeBsdfTranslucent")
    nt.links.new(mix.outputs[0], tr.inputs["Color"])
    sh = nt.nodes.new("ShaderNodeMixShader")
    sh.inputs[0].default_value = 0.28
    nt.links.new(b.outputs[0], sh.inputs[1])
    nt.links.new(tr.outputs[0], sh.inputs[2])
    nt.links.new(sh.outputs[0], out.inputs["Surface"])
    return m


def materials(warship):
    M = {}
    M["bottom"] = principled("bottom", (0.045, 0.03, 0.022), 0.85)
    if warship:
        M["side"] = planked("side", (0.035, 0.024, 0.018), (0.01, 0.008, 0.006), 13, 2, 0.6)
        M["band"] = planked("band", (0.62, 0.42, 0.13), (0.22, 0.14, 0.05), 13, 2, 0.6)
        M["trim"] = planked("trim", (0.035, 0.024, 0.018), (0.01, 0.008, 0.006), 13, 2, 0.6)
    else:
        M["side"] = planked("side", (0.30, 0.13, 0.05), (0.07, 0.03, 0.015), 13, 2, 0.6)
        M["band"] = planked("band", (0.30, 0.13, 0.05), (0.07, 0.03, 0.015), 13, 2, 0.6)
        M["trim"] = principled("trim", (0.80, 0.74, 0.60), 0.55)
    M["deck"] = planked("deck", (0.50, 0.33, 0.17), (0.14, 0.08, 0.04), 16, 1, 0.8)
    M["spar"] = planked("spar", (0.22, 0.11, 0.05), (0.10, 0.05, 0.02), 3, 2, 0.6)
    M["cabin"] = planked("cabin", (0.42, 0.22, 0.09), (0.12, 0.06, 0.03), 14, 2, 0.7)
    M["roof"] = principled("roof", (0.07, 0.045, 0.03), 0.8)
    M["sail"] = canvas("sail", (0.93, 0.88, 0.75))
    M["furl"] = principled("furl", (0.70, 0.63, 0.48), 0.95)
    M["rope"] = principled("rope", (0.045, 0.03, 0.02), 0.9)
    M["dark"] = principled("dark", (0.006, 0.005, 0.004), 0.9)
    M["brass"] = principled("brass", (0.75, 0.52, 0.16), 0.3, 1.0)
    M["lamp"] = principled("lamp", (1, 0.8, 0.5), 0.4, emit=(1.0, 0.62, 0.22), strength=42)
    M["glass"] = principled("glass", (1, 0.8, 0.45), 0.3, emit=(1.0, 0.66, 0.26), strength=5.5)
    M["iron"] = principled("iron", (0.03, 0.03, 0.035), 0.45, 0.9)
    for i, col in enumerate(
        ((0.55, 0.10, 0.08), (0.12, 0.22, 0.42), (0.65, 0.58, 0.42), (0.16, 0.32, 0.18))
    ):
        M[f"shirt{i}"] = principled(f"shirt{i}", col, 0.9)
    M["skin"] = principled("skin", (0.62, 0.38, 0.26), 0.8)
    return M


# ---------------------------------------------------------------- parts


def obj(name, mesh, mat=None, smooth=False):
    o = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(o)
    if mat:
        mesh.materials.append(mat)
    if smooth:
        for p in mesh.polygons:
            p.use_smooth = True
    return o


def rod(a, b, r0, r1=None, mat=None, sides=10):
    """A tapered cylinder between two points."""
    a, b = Vector(a), Vector(b)
    r1 = r0 if r1 is None else r1
    bm = bmesh.new()
    bmesh.ops.create_cone(
        bm, cap_ends=True, segments=sides, radius1=r0, radius2=r1, depth=(b - a).length
    )
    me = bpy.data.meshes.new("rod")
    bm.to_mesh(me)
    bm.free()
    o = obj("rod", me, mat, True)
    o.location = (a + b) / 2
    o.rotation_mode = "QUATERNION"
    o.rotation_quaternion = (b - a).to_track_quat("Z", "Y")
    return o


def block(centre, half, mat, bevel=0.008):
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=2)
    for v in bm.verts:
        v.co = Vector((v.co.x * half[0], v.co.y * half[1], v.co.z * half[2]))
    if bevel:
        bmesh.ops.bevel(bm, geom=bm.edges[:], offset=bevel, segments=2, affect="EDGES")
    me = bpy.data.meshes.new("block")
    bm.to_mesh(me)
    bm.free()
    o = obj("block", me, mat)
    o.location = centre
    return o


def ball(centre, r, mat):
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=14, v_segments=8, radius=r)
    me = bpy.data.meshes.new("ball")
    bm.to_mesh(me)
    bm.free()
    o = obj("ball", me, mat, True)
    o.location = centre
    return o


def hull(c, M):
    """The hull, lofted from sections: bottom, sides, a band, a rail, bulwarks and the deck."""
    L, draft = c["L"], c["draft"]
    ts = np.linspace(-1, 1, 57)
    rail, wall = 0.085, 0.035
    ks = np.linspace(0, 1, 9)
    rings = []
    for t in ts:
        b = float(plan(c, t))
        d = float(deck(c, t))
        top = d + rail
        pts = [(0.0, -draft)]
        for k in ks:
            pts.append((b * (0.5 + 0.5 * k**0.55), -draft + (top + draft) * k))
        inner = max(b - wall, 0.0)
        pts += [(inner, top), (inner, d), (0.0, d)]
        rings.append([(L * t, y, z) for z, y in pts])
    names = ["bottom", "side", "band", "trim", "deck"]
    slot = {n: i for i, n in enumerate(names)}
    bm = bmesh.new()
    grid = [
        [(bm.verts.new(V(x, y, z)), bm.verts.new(V(x, y, -z))) for x, y, z in ring]
        for ring in rings
    ]
    n = len(rings[0])
    free = c["free"]

    def which(j, y):
        if j >= n - 3:
            return "deck" if j == n - 2 else "trim"
        if y < 0.07:
            return "bottom"
        if y > free - 0.02:
            return "trim"
        if y > free - 0.2:
            return "band"
        return "side"

    for i in range(len(rings) - 1):
        for j in range(n - 1):
            y = (rings[i][j][1] + rings[i][j + 1][1]) / 2
            for side in (0, 1):
                quad = [
                    grid[i][j][side],
                    grid[i + 1][j][side],
                    grid[i + 1][j + 1][side],
                    grid[i][j + 1][side],
                ]
                if len(set(quad)) < 3:
                    continue
                try:
                    f = bm.faces.new(quad if side == 0 else quad[::-1])
                    f.material_index = slot[which(j, y)]
                    f.smooth = j < n - 3
                except ValueError:
                    pass
    # The transom.
    for j in range(n - 4):
        try:
            f = bm.faces.new([grid[0][j][0], grid[0][j + 1][0], grid[0][j + 1][1], grid[0][j][1]])
            f.material_index = slot["band" if rings[0][j][1] > free - 0.25 else "side"]
        except ValueError:
            pass
    bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=1e-5)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    me = bpy.data.meshes.new("hull")
    bm.to_mesh(me)
    bm.free()
    o = obj("hull", me)
    for name in names:
        me.materials.append(M[name])
    return o


def sheet(points, nu, nv, mat):
    """A cloth surface from an nu x nv grid of points (rows of nu)."""
    bm = bmesh.new()
    uv = bm.loops.layers.uv.new("uv")
    vs = [bm.verts.new(p) for p in points]
    for j in range(nv - 1):
        for i in range(nu - 1):
            ids = [j * nu + i, j * nu + i + 1, (j + 1) * nu + i + 1, (j + 1) * nu + i]
            if len({tuple(vs[k].co) for k in ids}) < 3:
                continue
            f = bm.faces.new([vs[k] for k in ids])
            f.smooth = True
            for loop, k in zip(f.loops, ids, strict=False):
                loop[uv].uv = ((k % nu) / (nu - 1), (k // nu) / (nv - 1))
    me = bpy.data.meshes.new("sail")
    bm.to_mesh(me)
    bm.free()
    return obj("sail", me, mat)


def sailor(x, y, z, M, i):
    rod(V(x, y, z), V(x, y + 0.09, z), 0.022, 0.026, M[f"shirt{i % 4}"], 8)
    ball(V(x, y + 0.115, z), 0.022, M["skin"])


def build(name, n, k):
    """The ship on heading k: her sails trimmed to the chart's wind (shipspec.trim)."""
    c = SHIPS[name]
    tr = trim(k)
    L, free = c["L"], c["free"]
    M = materials(c["ports"] > 0)
    hull(c, M)

    def dk(x):
        return float(deck(c, x / L))

    def hb(x):
        return float(plan(c, x / L))

    # The stern lantern, and windows across the transom.
    sy = dk(-L)
    lamp = V(-L - 0.04, sy + 0.3, 0)
    rod(V(-L + 0.01, sy + 0.05, 0), V(-L - 0.04, sy + 0.25, 0), 0.012, 0.012, M["iron"], 6)
    ball(lamp, 0.045, M["lamp"])
    light = bpy.data.lights.new("lantern", "POINT")
    light.energy = 5.0
    light.color = (1.0, 0.62, 0.25)
    light.shadow_soft_size = 0.04
    lo = bpy.data.objects.new("lantern", light)
    lo.location = lamp + Vector((0.08, 0, 0.02))
    bpy.context.scene.collection.objects.link(lo)
    rows = 2 if c["castle"] > 0.2 else 1
    cols = 5 if c["castle"] > 0.2 else 3 if c["masts"] else 0
    for r in range(rows):
        for k in range(cols):
            z = (k - (cols - 1) / 2) * hb(-L) * 1.25 / cols
            block(
                V(-L - 0.004, sy - 0.1 - r * 0.13, z),
                (0.006, hb(-L) * 0.42 / cols, 0.04),
                M["glass"],
                0,
            )

    # Gunports along the band.
    for k in range(c["ports"]):
        x = -L * 0.72 + (L * 1.42) * k / max(1, c["ports"] - 1)
        for side in (-1, 1):
            w = hb(x) * (
                0.5 + 0.5 * ((free - 0.11 + c["draft"]) / (free + 0.085 + c["draft"])) ** 0.55
            )
            block(V(x, free - 0.11, side * (w + 0.002)), (0.035, 0.006, 0.032), M["dark"], 0)

    # Deck furniture.
    houses = {
        "sloop": [(-0.5, 0.2, 0.07, 0.17)],
        "brig": [(-0.92, 0.2, 0.08, 0.22)],
        "frigate": [(-1.28, 0.2, 0.08, 0.24)],
        "galleon": [(1.42, 0.14, 0.05, 0.16)],
    }.get(name, [])
    for hx, hl, hh, hw in houses:
        y = dk(hx)
        block(V(hx, y + hh, 0), (hl, hw, hh), M["cabin"])
        block(V(hx, y + hh * 2 + 0.012, 0), (hl + 0.025, hw + 0.025, 0.014), M["roof"])
        for side in (-1, 1):
            for k in (-1, 1):
                block(
                    V(hx + k * hl * 0.45, y + hh * 1.15, side * (hw + 0.002)),
                    (hl * 0.22, 0.004, hh * 0.38),
                    M["glass"],
                    0,
                )
    hatches = {
        "sloop": [0.45],
        "brig": [0.06, 0.92],
        "frigate": [-0.45, 0.32, 1.15],
        "galleon": [-0.58, 0.38],
    }.get(name, [])
    for hx in hatches:
        block(V(hx, dk(hx) + 0.02, 0), (0.12, min(0.13, hb(hx) * 0.4), 0.02), M["roof"])
    if c["masts"]:
        for bx, bz in ((-0.12, 0.6), (0.3, -0.55), (-0.3, -0.5), (0.62, 0.5))[: 2 + c["crew"] // 2]:
            x = bx * L
            z = bz * hb(x)
            rod(V(x, dk(x), z), V(x, dk(x) + 0.085, z), 0.04, 0.034, M["cabin"], 10)
        for k in range(c["crew"]):
            x = L * (-0.78 + 1.5 * ((k * 0.37 + 0.11) % 1.0))
            z = hb(x) * (0.55 if k % 2 else -0.5)
            sailor(x, dk(x), z, M, k)
    else:
        for z in (-0.1, 0.1):
            rod(V(-0.2, free + 0.035, z), V(0.32, free + 0.06, z * 2.3), 0.012, 0.012, M["spar"], 6)
        block(V(-0.05, free - 0.02, 0), (0.03, 0.16, 0.01), M["cabin"])
        sailor(-0.12, free - 0.03, 0, M, 1)
        return c
    # The wheel, on its post.
    hx, hy, hz = helm(name)
    rod(V(hx, hy - 0.14, 0), V(hx, hy - 0.02, 0), 0.016, 0.012, M["spar"], 8)
    for i in range(12):
        a0, a1 = i * math.pi / 6, (i + 1) * math.pi / 6
        rod(
            V(hx, hy + 0.07 * math.cos(a0), 0.07 * math.sin(a0)),
            V(hx, hy + 0.07 * math.cos(a1), 0.07 * math.sin(a1)),
            0.008,
            0.008,
            M["brass"],
            6,
        )
    for i in range(4):
        a = i * math.pi / 4
        rod(
            V(hx, hy - 0.085 * math.cos(a), -0.085 * math.sin(a)),
            V(hx, hy + 0.085 * math.cos(a), 0.085 * math.sin(a)),
            0.006,
            0.006,
            M["spar"],
            5,
        )
    sailor(hx - 0.09, hy - 0.14, 0, M, 0)
    # Spars.
    bow_y = dk(L)
    tip = (L + 0.5, bow_y + 0.32, 0)
    rod(V(L * 0.8, bow_y + 0.03, 0), V(*tip), 0.034, 0.02, M["spar"])
    u = np.array([-math.sin(tr["nu"]), 0, math.cos(tr["nu"])])
    nrm = np.array([math.cos(tr["nu"]), 0, math.sin(tr["nu"])])
    tops = []
    for mx, mh, _ in c["masts"]:
        top = (mx, free + mh, 0)
        tops.append(top)
        rod(V(mx, dk(mx) - 0.05, 0), V(*top), 0.042, 0.022, M["spar"], 12)
    line = [tip] + [tops[i] for i in reversed(range(len(tops)))] + [(-L, dk(-L) + 0.08, 0)]
    for a, b in zip(line, line[1:], strict=False):
        rod(V(*a), V(*b), 0.006, 0.006, M["rope"], 5)
    for mx, mh, _ in c["masts"]:
        for side in (-1, 1):
            for k in (0, 1):
                x = mx - 0.06 - k * 0.12
                rod(
                    V(mx, free + mh * 0.8, 0),
                    V(x, dk(x) + 0.07, side * hb(x) * 0.98),
                    0.005,
                    0.005,
                    M["rope"],
                    5,
                )

    if c.get("rig") == "sloop":
        mx, mh, _ = c["masts"][0]
        # The boom eased out to leeward, the sail bellying to leeward of it.
        g, lee = (tr["boom"], tr["lee"]) if n else (math.radians(4), 1)
        aft = np.array([-math.cos(g), 0, lee * math.sin(g)])
        side = np.array([math.sin(g), 0, lee * math.cos(g)])
        boom_y, boom_l = free + 0.34, 1.05
        base = np.array([mx, 0, 0])
        rod(
            V(mx, boom_y, 0),
            V(*(base + aft * boom_l + np.array([0, boom_y + 0.03, 0]))),
            0.026,
            0.02,
            M["spar"],
        )
        if n >= 1:
            pts, nu, nv = [], 12, 12
            for j in range(nv):
                b = j / (nv - 1)
                for i in range(nu):
                    a = i / (nu - 1)
                    reach = 0.03 + a * (boom_l - 0.07) * (1 - b)
                    belly = 0.17 * abs(tr["fill"]) * math.sin(math.pi * a) * (1 - b) ** 0.7
                    pts.append(
                        V(
                            *(
                                base
                                + aft * reach
                                + side * belly
                                + np.array([0, boom_y + 0.05 + b * (mh - 0.5), 0])
                            )
                        )
                    )
            sheet(pts, nu, nv, M["sail"])
        else:
            rod(
                V(*(base + aft * 0.06 + np.array([0, boom_y + 0.07, 0]))),
                V(*(base + aft * (boom_l - 0.1) + np.array([0, boom_y + 0.1, 0]))),
                0.05,
                0.04,
                M["furl"],
            )
        if n >= 2:
            g2 = g * 0.4
            fwd = np.array([math.cos(g2), 0, lee * math.sin(g2)])
            side2 = np.array([-math.sin(g2), 0, lee * math.cos(g2)])
            reach_max = tip[0] - mx - 0.08
            pts, nu, nv = [], 10, 10
            for j in range(nv):
                b = j / (nv - 1)
                for i in range(nu):
                    a = i / (nu - 1)
                    reach = 0.08 + a * reach_max * (1 - b)
                    y = (free + 0.5) + b * (mh * 0.86 - 0.5) + a * (1 - b) * (tip[1] - free - 0.42)
                    belly = 0.13 * abs(tr["fill"]) * math.sin(math.pi * a) * (1 - b) ** 0.7
                    pts.append(V(*(base + fwd * reach + side2 * belly + np.array([0, y, 0]))))
            sheet(pts, nu, nv, M["sail"])
        return c

    tiers = c["tiers"]
    order = sorted(range(len(c["masts"])), key=lambda m: (m != c["main"], -m))
    slots = [(m, t) for t in range(tiers) for m in order]
    set_ = set(slots[:n])
    for m, (mx, mh, hw0) in enumerate(c["masts"]):
        th = (mh - 0.3) / tiers
        for t in range(tiers):
            hw = hw0 * (1 - 0.2 * t)
            y0 = free + 0.3 + th * t
            y1 = y0 + th - 0.08
            centre = np.array([mx, 0, 0]) + nrm * 0.07
            up = np.array([0, y1, 0])
            rod(
                V(*(centre - u * (hw + 0.06) + up)),
                V(*(centre + u * (hw + 0.06) + up)),
                0.022,
                0.022,
                M["spar"],
            )
            if (m, t) in set_:
                pts, nu, nv = [], 14, 10
                for j in range(nv):
                    v = -1 + 2 * j / (nv - 1)
                    for i in range(nu):
                        s = -1 + 2 * i / (nu - 1)
                        belly = (0.2 - 0.035 * t) * tr["fill"] * (1 - s * s) * (
                            1 - 0.55 * v * v
                        ) + 0.012 * math.sin(s * 9) * (1 - v) * 0.5
                        width = hw * (1.0 + 0.06 * -v)
                        pts.append(
                            V(
                                *(
                                    centre
                                    + u * s * width
                                    + nrm * belly
                                    + np.array([0, (y0 + y1) / 2 + v * (y1 - y0) / 2, 0])
                                )
                            )
                        )
                sheet(pts, nu, nv, M["sail"])
            else:
                rod(
                    V(*(centre - u * hw + up - np.array([0, 0.05, 0]))),
                    V(*(centre + u * hw + up - np.array([0, 0.05, 0]))),
                    0.046,
                    0.046,
                    M["furl"],
                )
                for k in (-0.6, 0, 0.6):
                    p = centre + u * hw * k + up - np.array([0, 0.05, 0])
                    rod(V(*(p - u * 0.012)), V(*(p + u * 0.012)), 0.05, 0.05, M["rope"])
        if name != "brig":
            rod(
                V(mx, free + mh * 0.64, 0),
                V(mx, free + mh * 0.64 + 0.025, 0),
                0.1,
                0.1,
                M["spar"],
                12,
            )
    return c


# ---------------------------------------------------------------- scene


def stage(c, lamps_only):
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = 48 if lamps_only else (160 if PX > 100 else 96)
    sc.cycles.use_denoising = True
    sc.cycles.max_bounces = 4
    sc.render.film_transparent = True
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGBA"
    sc.render.threads_mode = "FIXED"
    sc.render.threads = int(os.environ.get("SHIP_THREADS", "4"))
    f = frame(c)
    sc.render.resolution_x = round(f["wu"] * PX)
    sc.render.resolution_y = round(f["hu"] * PX)
    sc.render.resolution_percentage = 100
    try:
        sc.view_settings.view_transform = "AgX"
        sc.view_settings.look = "AgX - Medium High Contrast"
    except TypeError:
        pass
    cam = bpy.data.cameras.new("cam")
    cam.type = "ORTHO"
    cam.ortho_scale = (
        max(f["wu"], f["hu"] * sc.render.resolution_x / sc.render.resolution_y)
        if False
        else f["wu"]
    )
    cam.clip_end = 100
    co = bpy.data.objects.new("cam", cam)
    sc.collection.objects.link(co)
    forward = Vector((0, math.cos(EL), -math.sin(EL)))
    aim = Vector((0, 0, f["vc"] / math.cos(EL)))
    co.location = aim - forward * 30
    co.rotation_euler = (math.pi / 2 - EL, 0, 0)
    sc.camera = co
    world = bpy.data.worlds.new("sky")
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    bg.inputs[0].default_value = (0.30, 0.46, 0.78, 1)
    bg.inputs[1].default_value = 0.0 if lamps_only else 0.85
    sc.world = world
    if not lamps_only:
        sun = bpy.data.lights.new("sun", "SUN")
        sun.energy = 4.8
        sun.color = (1.0, 0.86, 0.66)
        sun.angle = math.radians(5)
        so = bpy.data.objects.new("sun", sun)
        d = Vector((SUN[0], -SUN[2], SUN[1]))
        so.rotation_mode = "QUATERNION"
        so.rotation_quaternion = d.to_track_quat("Z", "Y")
        sc.collection.objects.link(so)
    # The sea: catches the ship's shadow and hides everything under the water.
    bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=30)
    me = bpy.data.meshes.new("sea")
    bm.to_mesh(me)
    bm.free()
    sea = obj("sea", me, principled("sea", (0.02, 0.1, 0.14), 0.4))
    sea.is_shadow_catcher = True


def clear():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def main():
    args = sys.argv[sys.argv.index("--") + 1 :]
    out, jobs = args[0], args[1:]
    os.makedirs(out, exist_ok=True)
    for job in jobs:
        name, token, k = job.split("-")
        sails, twin, lamps = parse(token)
        clear()
        c = build(name, sails, int(k))
        stage(c, lamps)
        # Turn the ship: heading k is k * 45 degrees clockwise on screen from east.
        pivot = bpy.data.objects.new("pivot", None)
        bpy.context.scene.collection.objects.link(pivot)
        for o in list(bpy.context.scene.collection.objects):
            if o.name.startswith(("cam", "sun", "sea", "pivot")):
                continue
            o.parent = pivot
        # Under sail she lies over to leeward, the more the more canvas she carries; at anchor she
        # rolls a little, one way in one render and the other way in its twin.
        # (XYZ: the roll about her keel first, then her heading.)
        pivot.rotation_mode = "XYZ"
        pivot.rotation_euler = (
            roll(name, sails, int(k), twin),
            0,
            -int(k) * 2 * math.pi / HEADINGS,
        )
        bpy.context.scene.render.filepath = os.path.join(out, job + ".png")
        bpy.ops.render.render(write_still=True)


if __name__ == "__main__":
    main()
