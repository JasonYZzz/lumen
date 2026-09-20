"""Rebuild the authored Lumen fox with Blender's Python runtime.

Run: Blender --background --factory-startup --python scripts/build_fox_model.py
Reference: repository-owned docs/brand/mascot/fox-video-reference.png.
This is an authored stylized mesh, not an automatic reconstruction of the image.
"""

from __future__ import annotations

import math
from pathlib import Path

import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs/brand/mascot/three"
ASSETS.mkdir(parents=True, exist_ok=True)
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)


def material(name, color, roughness=0.48, metallic=0):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*color, 1)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = (*color, 1)
    bsdf.inputs["Roughness"].default_value = roughness
    bsdf.inputs["Metallic"].default_value = metallic
    return mat


cream = material("Ivory porcelain fur", (0.91, 0.76, 0.55))
white = material("Warm white face and chest", (1, 0.94, 0.81))
pink = material("Rose inner ears and nose", (0.76, 0.28, 0.23))
ink = material("Plum eye lining", (0.065, 0.022, 0.048), 0.38)
iris = material("Amber iris", (0.83, 0.32, 0.045), 0.26)
violet = material("Violet iris rim", (0.28, 0.095, 0.38), 0.3)
black = material("Deep pupils", (0.011, 0.006, 0.021), 0.22)
glint = material("Eye highlights", (1, 1, 1), 0.18)
gold = material("Forehead gold", (0.84, 0.43, 0.13), 0.4, 0.15)
tailmat = material("Pastel tail gradient", (1, 1, 1))
attr = tailmat.node_tree.nodes.new("ShaderNodeVertexColor")
attr.layer_name = "Color"
tailmat.node_tree.links.new(
    attr.outputs["Color"], tailmat.node_tree.nodes.get("Principled BSDF").inputs["Base Color"]
)


def empty(name, loc=(0, 0, 0), parent=None):
    obj = bpy.data.objects.new(name, None)
    bpy.context.collection.objects.link(obj)
    obj.location = loc
    obj.parent = parent
    return obj


root = empty("LumenFox")
body = empty("Breathing", parent=root)
head = empty("Head", (0, -0.08, 1.72), body)
animated = [body, head]


def ellipsoid(name, loc, scale, mat, parent=body, segments=24, rings=16):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=segments, ring_count=rings)
    obj = bpy.context.object
    obj.name = name
    obj.parent = parent
    obj.location = loc
    obj.scale = scale
    obj.data.materials.append(mat)
    for poly in obj.data.polygons:
        poly.use_smooth = True
    return obj


def tapered(name, points, widths, mat, parent, depth=0.65, tip_color=None):
    """Closed swept organic volume, with rounded cross sections and a pointed tip."""
    verts, faces, colors = [], [], []
    sides = 12
    count = len(points)
    for i, point in enumerate(points):
        p = Vector(point)
        tangent = Vector(points[min(i + 1, count - 1)]) - Vector(points[max(0, i - 1)])
        tangent.normalize()
        side = tangent.cross(Vector((0, 1, 0))).normalized()
        normal = side.cross(tangent).normalized()
        for j in range(sides):
            angle = 2 * math.pi * j / sides
            verts.append(p + widths[i] * (math.cos(angle) * side + depth * math.sin(angle) * normal))
            blend = max(0, min(1, (i / (count - 1) - 0.67) / 0.24))
            base = (0.98, 0.85, 0.65)
            colors.append(
                (*(base[k] * (1 - blend) + (tip_color or base)[k] * blend for k in range(3)), 1)
            )
    for i in range(count - 1):
        for j in range(sides):
            a = i * sides + j
            b = i * sides + (j + 1) % sides
            faces.append((a, b, b + sides, a + sides))
    faces += [tuple(reversed(range(sides))), tuple((count - 1) * sides + j for j in range(sides))]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    obj.parent = parent
    mesh.materials.append(mat)
    for poly in mesh.polygons:
        poly.use_smooth = True
    if tip_color:
        color = mesh.color_attributes.new(name="Color", type="FLOAT_COLOR", domain="POINT")
        for entry, rgba in zip(color.data, colors, strict=True):
            entry.color = rgba
    return obj


def bezier(points, t):
    a, b, c, d = (Vector(p) for p in points)
    return (1 - t) ** 3 * a + 3 * (1 - t) ** 2 * t * b + 3 * (1 - t) * t * t * c + t**3 * d


# Nine individually named, rounded, gently curved tail volumes behind the body.
for i in range(9):
    angle = math.radians(-114 + i * 28.5)
    x, z = math.sin(angle), math.cos(angle)
    end = Vector((x * 1.82, 0.28 + abs(x) * 0.13, 1.65 + z * 1.62))
    start = Vector((x * 0.25, 0.48, 0.64))
    control = [start, (x * 0.92, 0.63, 0.74 + z * 0.3), (end.x * 0.93, 0.49, end.z - 0.40), end]
    pivot = empty(f"Tail_{i + 1:02d}", start, root)
    points = [bezier(control, j / 24) - start for j in range(25)]
    widths = [0.012 + 0.275 * math.sin(math.pi * j / 24) ** 0.75 for j in range(25)]
    widths[-1] = 0.003
    colors = [(0.63, 0.46, 0.85), (0.46, 0.69, 0.89), (0.82, 0.47, 0.77)]
    tapered(f"Tail_{i + 1:02d}_fur", points, widths, tailmat, pivot, 0.82, colors[i % 3])
    animated.append(pivot)

ellipsoid("Body", (0, 0.06, 0.91), (0.46, 0.39, 0.69), cream)
ellipsoid("Chest bib", (0, -0.294, 1.09), (0.335, 0.145, 0.47), white)
for s in (-1, 1):
    ellipsoid(f"Haunch_{s}", (s * 0.36, 0.13, 0.38), (0.29, 0.35, 0.34), cream)
    leg = empty(f"Foreleg_{s}", (s * 0.23, -0.22, 0.85), body)
    ellipsoid(f"Foreleg fur_{s}", (0, 0, -0.30), (0.13, 0.15, 0.40), white, leg)
    ellipsoid(f"Paw_{s}", (0, -0.09, -0.66), (0.17, 0.22, 0.14), white, leg)
    for j in (-1, 1):
        ellipsoid(f"Toe crease_{s}_{j}", (j * 0.052, -0.286, -0.68), (0.009, 0.009, 0.04), cream, leg, 12, 8)
    if s == 1:
        animated.append(leg)

ellipsoid("Skull", (0, 0, 0.11), (0.58, 0.40, 0.49), white, head, 32, 24)
ellipsoid("Brow crown", (0, 0.015, 0.36), (0.43, 0.34, 0.30), cream, head)
for s in (-1, 1):
    # A thick swept triangular ear, with a smaller inset pink volume.
    pts = [(s * 0.32, 0.015, 0.39), (s * 0.46, 0.035, 0.62), (s * 0.59, 0.075, 0.91), (s * 0.63, 0.1, 1.21)]
    tapered(f"Ear_{s}", pts, [0.20, 0.19, 0.13, 0.004], white, head, 0.55)
    pts = [
        (s * 0.37, -0.086, 0.49),
        (s * 0.48, -0.065, 0.69),
        (s * 0.575, -0.015, 0.91),
        (s * 0.61, 0.038, 1.105),
    ]
    tapered(f"Inner ear_{s}", pts, [0.115, 0.12, 0.076, 0.003], pink, head, 0.18)
    # Almond sockets: dark contour, ivory eye, violet rim, amber pupil surround.
    eye = empty(f"Eye_{s}", (s * 0.238, -0.332, 0.15), head)
    eye.rotation_euler[1] = s * -0.10
    ellipsoid(f"Eye contour_{s}", (0, 0, 0), (0.202, 0.084, 0.194), ink, eye)
    ellipsoid(f"Eye white_{s}", (0, -0.043, -0.009), (0.177, 0.065, 0.157), white, eye)
    ellipsoid(f"Iris rim_{s}", (-s * 0.012, -0.096, -0.005), (0.105, 0.029, 0.137), violet, eye)
    ellipsoid(f"Iris amber_{s}", (-s * 0.012, -0.117, -0.018), (0.083, 0.016, 0.105), iris, eye)
    ellipsoid(f"Pupil_{s}", (-s * 0.012, -0.132, 0.004), (0.037, 0.012, 0.094), black, eye)
    ellipsoid(f"Catchlight_{s}", (-0.037, -0.147, 0.059), (0.038, 0.012, 0.044), glint, eye, 16, 12)
    ellipsoid(f"Catchlight small_{s}", (0.035, -0.144, -0.051), (0.015, 0.009, 0.018), glint, eye, 12, 8)
    ellipsoid(f"Muzzle_{s}", (s * 0.115, -0.383, -0.113), (0.185, 0.15, 0.115), white, head)
    # Cheek tufts and ruff use pointed fur volumes instead of flat picture planes.
    for j in range(3):
        p = [
            (s * 0.37, -0.15, -0.08 + j * 0.09),
            (s * 0.52, -0.17, -0.09 + j * 0.10),
            (s * (0.66 - j * 0.018), -0.12, 0.01 + j * 0.11),
        ]
        tapered(f"Cheek tuft_{s}_{j}", p, [0.11, 0.085, 0.002], white, head, 0.6)
    for j in range(3):
        p = [
            (s * (0.07 + j * 0.095), -0.31, 1.31 - j * 0.02),
            (s * (0.11 + j * 0.08), -0.42, 1.12 - j * 0.035),
            (s * (0.06 + j * 0.07), -0.36, 0.91 - j * 0.03),
        ]
        tapered(f"Chest tuft_{s}_{j}", p, [0.085, 0.08, 0.002], white, body, 0.55)
    tapered(
        f"Gold brow_{s}",
        [(s * 0.12, -0.341, 0.40), (s * 0.24, -0.335, 0.435), (s * 0.34, -0.296, 0.39)],
        [0.009, 0.02, 0.002],
        gold,
        head,
        0.45,
    )

ellipsoid("Nose", (0, -0.526, -0.091), (0.071, 0.042, 0.049), pink, head, 20, 12)
tapered(
    "Forehead flame",
    [(0, -0.29, 0.65), (-0.055, -0.344, 0.52), (0, -0.389, 0.39), (0, -0.39, 0.29)],
    [0.001, 0.055, 0.038, 0.001],
    gold,
    head,
    0.18,
)
for s in (-1, 1):
    tapered(
        f"Smile_{s}",
        [(0, -0.492, -0.148), (s * 0.065, -0.501, -0.176), (s * 0.115, -0.478, -0.151)],
        [0.008, 0.009, 0.002],
        ink,
        head,
        0.6,
    )

# Object-transform rig: editable articulated tail roots/head/foreleg, no baked movie.
# Common NLA track names merge each object's channels into exactly two GLB clips.
scene = bpy.context.scene
scene.render.fps = 24
for obj in animated:
    base_loc = obj.location.copy()
    for clip, frames in (("idle", [1, 25, 49, 73, 97]), ("react", [1, 9, 17, 25, 33, 41])):
        obj.animation_data_clear()
        for index, frame in enumerate(frames):
            phase = index / (len(frames) - 1) * 2 * math.pi
            obj.location = base_loc
            obj.rotation_euler = (0, 0, 0)
            obj.scale = (1, 1, 1)
            if obj == body:
                obj.scale = (
                    1 + 0.012 * math.sin(phase),
                    1 + 0.015 * math.sin(phase),
                    1 + 0.012 * math.sin(phase),
                )
            elif obj == head:
                obj.rotation_euler[1] = (0.035 if clip == "idle" else 0.10) * math.sin(phase)
                obj.rotation_euler[0] = 0.025 * math.sin(phase)
            elif obj.name.startswith("Tail_"):
                offset = int(obj.name.split("_")[1]) * 0.38
                obj.rotation_euler[1] = (0.025 if clip == "idle" else 0.075) * math.sin(phase + offset)
                obj.rotation_euler[2] = 0.024 * math.sin(phase + offset)
            elif clip == "react":
                obj.rotation_euler[1] = [0, -0.75, -1.1, -0.8, -1.05, 0][index]
                obj.rotation_euler[0] = [0, -0.3, -0.15, -0.4, -0.1, 0][index]
            obj.keyframe_insert(data_path="location", frame=frame)
            obj.keyframe_insert(data_path="rotation_euler", frame=frame)
            obj.keyframe_insert(data_path="scale", frame=frame)
        action = obj.animation_data.action
        action.name = f"{obj.name}_{clip}"
        # Stash tracks after both actions; clearing animation_data doesn't delete actions.
        obj.animation_data.action = None
    obj.animation_data_clear()
    obj.animation_data_create()
    for clip in ("idle", "react"):
        track = obj.animation_data.nla_tracks.new()
        track.name = clip
        track.strips.new(clip, 1, bpy.data.actions[f"{obj.name}_{clip}"])
    obj.location = base_loc
    obj.rotation_euler = (0, 0, 0)
    obj.scale = (1, 1, 1)

scene.frame_start = 1
scene.frame_end = 97
scene.frame_set(1)
# Export only the model hierarchy, not reference planes, camera or studio.
bpy.ops.object.select_all(action="DESELECT")
for obj in [root, *root.children_recursive]:
    obj.select_set(True)
bpy.context.view_layer.objects.active = root
out = ROOT / "docs/brand/mascot/three/rejected-fox.glb"
bpy.ops.export_scene.gltf(
    filepath=str(out),
    export_format="GLB",
    use_selection=True,
    export_animations=True,
    export_animation_mode="NLA_TRACKS",
    export_nla_strips=True,
    export_force_sampling=True,
    export_materials="EXPORT",
    export_yup=True,
    export_cameras=False,
    export_lights=False,
    export_extras=False,
    export_unused_images=False,
)

# Packed source reference, visible in the Blender file but excluded from GLB.
ref = bpy.data.images.load(str(ROOT / "docs/brand/mascot/fox-video-reference.png"))
ref.pack()
ref_obj = empty("REFERENCE — existing Lumen artwork", (3.5, 0.7, 1.7))
ref_obj.empty_display_type = "IMAGE"
ref_obj.data = ref
ref_obj.empty_display_size = 3.4
ref_obj.rotation_euler = (math.pi / 2, 0, 0)
ref_obj.hide_render = True


def aim(obj, point):
    obj.rotation_euler = (Vector(point) - obj.location).to_track_quat("-Z", "Y").to_euler()


bpy.ops.object.camera_add(location=(0.15, -8.5, 3.25))
cam = bpy.context.object
cam.name = "Portrait camera"
aim(cam, (0, 0, 1.63))
cam.data.type = "ORTHO"
cam.data.ortho_scale = 4.35
scene.camera = cam
for name, loc, power, size in [
    ("Key", (-3, -4, 6), 650, 4),
    ("Fill", (4, -2, 3), 400, 3),
    ("Rim", (0, 3, 5), 850, 3),
]:
    bpy.ops.object.light_add(type="AREA", location=loc)
    light = bpy.context.object
    light.name = name
    light.data.energy = power
    light.data.shape = "DISK"
    light.data.size = size
    aim(light, (0, 0, 1.5))
scene.world.color = (0.16, 0.16, 0.16)
scene.render.engine = "CYCLES"
scene.cycles.samples = 32
scene.render.resolution_x = 900
scene.render.resolution_y = 900
scene.render.resolution_percentage = 100
scene.render.film_transparent = True
scene.view_settings.view_transform = "AgX"
scene.render.image_settings.file_format = "PNG"
scene.render.filepath = str(ASSETS / "fox-front.png")
for area in bpy.context.screen.areas:
    if area.type == "VIEW_3D":
        area.spaces.active.region_3d.view_perspective = "CAMERA"
bpy.ops.wm.save_as_mainfile(filepath=str(ASSETS / "lumen-nine-tail-fox.blend"))
bpy.ops.render.render(write_still=True)
cam.location = (5, -7, 3.6)
aim(cam, (0, 0, 1.6))
scene.render.filepath = str(ASSETS / "fox-three-quarter.png")
bpy.ops.render.render(write_still=True)
print(f"EXPORTED {out}: {out.stat().st_size} bytes")
