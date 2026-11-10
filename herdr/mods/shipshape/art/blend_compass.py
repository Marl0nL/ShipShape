"""A brass compass rose lying on the chart, rendered in Blender with the chart's sun.

blender -b -P blend_compass.py -- OUT.png
"""

import math
import os
import sys

import bmesh
import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from blend_ship import obj, principled
from shipspec import EL, SUN


def prism(pts, depth, mat):
    """A flat shape (x, y) raised by depth, its top pitched to a ridge toward its first point."""
    bm = bmesh.new()
    base = [bm.verts.new((x, y, 0)) for x, y in pts]
    ridge = [bm.verts.new((0, 0, depth)), bm.verts.new((pts[0][0], pts[0][1], depth * 0.35))]
    for i in range(len(base)):
        a, b = base[i], base[(i + 1) % len(base)]
        bm.faces.new([a, b, ridge[0]])
    bm.faces.new(list(reversed(base)))
    bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=1e-6)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    me = bpy.data.meshes.new("pt")
    bm.to_mesh(me)
    bm.free()
    return obj("pt", me, mat)


def point(angle, length, half, depth, mats):
    """One point of the rose, in two halves: lit and shaded."""
    c, s = math.cos(angle), math.sin(angle)
    tip = (c * length, s * length)
    left = (-s * half, c * half)
    right = (s * half, -c * half)
    prism([tip, left, (0, 0)], depth, mats[0])
    prism([tip, (0, 0), right], depth, mats[1])


def main():
    out = sys.argv[sys.argv.index("--") + 1]
    bpy.ops.wm.read_factory_settings(use_empty=True)
    brass = principled("brass", (0.80, 0.55, 0.18), 0.28, 0.95)
    brass_dk = principled("brassdk", (0.42, 0.26, 0.07), 0.35, 0.95)
    ivory = principled("ivory", (0.10, 0.07, 0.04), 0.5)
    steel = principled("steel", (0.08, 0.12, 0.16), 0.35, 0.6)
    face = principled("face", (0.62, 0.55, 0.40), 0.6)
    red = principled("red", (0.55, 0.06, 0.04), 0.4)
    # The bezel and the card it holds.
    bpy.ops.mesh.primitive_torus_add(
        major_radius=1.0, minor_radius=0.07, major_segments=96, minor_segments=16
    )
    bpy.context.object.data.materials.append(brass)
    bpy.ops.mesh.primitive_cylinder_add(vertices=96, radius=1.0, depth=0.04, location=(0, 0, 0.0))
    bpy.context.object.data.materials.append(face)
    for i in range(32):
        a = i * math.pi / 16
        r0 = 0.83 if i % 4 == 0 else 0.88
        bpy.ops.mesh.primitive_cylinder_add(
            vertices=6,
            radius=0.008 if i % 4 else 0.013,
            depth=0.92 - r0,
            location=(math.cos(a) * (r0 + 0.92) / 2, math.sin(a) * (r0 + 0.92) / 2, 0.025),
            rotation=(0, math.pi / 2, a),
        )
        bpy.context.object.data.materials.append(ivory)
    # The rose: four great points (the north one red), four lesser between.
    for i in range(4):
        a = math.pi / 4 + i * math.pi / 2
        point(a, 0.52, 0.09, 0.05, (steel, brass_dk))
    for i in range(4):
        a = math.pi / 2 - i * math.pi / 2
        point(a, 0.8, 0.14, 0.09, (red, brass_dk) if i == 0 else (brass, brass_dk))
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.06, location=(0, 0, 0.08))
    bpy.context.object.data.materials.append(brass)
    # N.
    bpy.ops.object.text_add(location=(0, 0.88, 0.03))
    t = bpy.context.object
    t.data.body = "N"
    t.data.size = 0.2
    t.data.align_x = "CENTER"
    t.data.align_y = "CENTER"
    t.data.extrude = 0.01
    t.data.materials.append(ivory)

    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = 96
    sc.cycles.use_denoising = True
    sc.render.film_transparent = True
    sc.render.resolution_x = 300
    sc.render.resolution_y = 220
    sc.render.image_settings.color_mode = "RGBA"
    try:
        sc.view_settings.view_transform = "AgX"
        sc.view_settings.look = "AgX - Medium High Contrast"
    except TypeError:
        pass
    cam = bpy.data.cameras.new("cam")
    cam.type = "ORTHO"
    cam.ortho_scale = 2.35
    co = bpy.data.objects.new("cam", cam)
    sc.collection.objects.link(co)
    # The chart's camera: looking down at EL from the south. The rose lies flat, north up the chart.
    fwd = Vector((0, math.cos(EL), -math.sin(EL)))
    co.location = Vector((0, 0, 0.02)) - fwd * 20
    co.rotation_euler = (math.pi / 2 - EL, 0, 0)
    sc.camera = co
    world = bpy.data.worlds.new("sky")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (0.30, 0.46, 0.78, 1)
    world.node_tree.nodes["Background"].inputs[1].default_value = 0.9
    sc.world = world
    sun = bpy.data.lights.new("sun", "SUN")
    sun.energy = 4.8
    sun.color = (1.0, 0.86, 0.66)
    so = bpy.data.objects.new("sun", sun)
    so.rotation_mode = "QUATERNION"
    so.rotation_quaternion = Vector((SUN[0], -SUN[2], SUN[1])).to_track_quat("Z", "Y")
    sc.collection.objects.link(so)
    bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=10)
    me = bpy.data.meshes.new("sea")
    bm.to_mesh(me)
    bm.free()
    sea = obj("sea", me, principled("sea", (0.02, 0.1, 0.14), 0.4))
    sea.location.z = -0.03
    sea.is_shadow_catcher = True
    sc.render.filepath = out
    bpy.ops.render.render(write_still=True)


main()
