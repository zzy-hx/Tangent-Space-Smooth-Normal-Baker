bl_info = {
    "name": "Tangent Space Smooth Normal Baker",
    "author": "Pozemka",
    "version": (1, 0, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > Normal Baker",
    "description": "Bake smooth normals in tangent space to vertex colors for selected meshes",
    "category": "Mesh",
}

import bpy
import os
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from bpy.props import (
    StringProperty,
    BoolProperty,
    FloatProperty,
    PointerProperty,
)
from bpy.types import PropertyGroup, Operator, Panel


# ------------------------------------------------------------
# Math & Utility Functions
# ------------------------------------------------------------

def normalize_or(vector, fallback):
    if vector.length > 1e-8:
        return vector.normalized()
    return fallback.normalized()


def encode_normal(normal):
    n = normalize_or(normal, Vector((0, 0, 1)))
    return (
        n.x * 0.5 + 0.5,
        n.y * 0.5 + 0.5,
        n.z * 0.5 + 0.5,
    )


def get_corner_normal(mesh, loop_index):
    """Compatible retrieval of split corner normals across Blender 3.6 ~ 4.x."""
    if hasattr(mesh, "corner_normals") and len(mesh.corner_normals) > loop_index:
        cn = mesh.corner_normals[loop_index]
        vec = getattr(cn, "vector", cn)
        return Vector(vec).normalized()
    
    try:
        return mesh.loops[loop_index].normal.normalized()
    except Exception:
        poly_idx = mesh.loops[loop_index].polygon_index
        return mesh.polygons[poly_idx].normal.normalized()


def get_or_create_color_attribute(mesh, name):
    attr = mesh.color_attributes.get(name)
    if attr is not None and (attr.domain != 'CORNER' or attr.data_type != 'FLOAT_COLOR'):
        mesh.color_attributes.remove(attr)
        attr = None

    if attr is None:
        attr = mesh.color_attributes.new(
            name=name,
            type='FLOAT_COLOR',
            domain='CORNER',
        )
    return attr


def set_active_color(mesh, attr):
    try:
        mesh.color_attributes.active_color = attr
    except Exception:
        try:
            mesh.color_attributes.active_color_index = list(mesh.color_attributes).index(attr)
        except Exception:
            pass


def triangle_corner_angle(mesh, loop_indices, target_loop_index):
    local_index = loop_indices.index(target_loop_index)
    prev_index = loop_indices[(local_index - 1) % 3]
    next_index = loop_indices[(local_index + 1) % 3]

    loops = mesh.loops
    center = mesh.vertices[loops[target_loop_index].vertex_index].co
    prev_co = mesh.vertices[loops[prev_index].vertex_index].co
    next_co = mesh.vertices[loops[next_index].vertex_index].co

    a = prev_co - center
    b = next_co - center
    if a.length < 1e-8 or b.length < 1e-8:
        return 1.0
    return a.angle(b)


# ------------------------------------------------------------
# Spatial BVH Overlap Detection
# ------------------------------------------------------------

def build_world_bvh(obj):
    mesh = obj.data
    if not mesh.vertices or not mesh.polygons:
        return None

    world_vertices = [obj.matrix_world @ vertex.co for vertex in mesh.vertices]
    triangles = []

    for poly in mesh.polygons:
        indices = list(poly.vertices)
        if len(indices) == 3:
            triangles.append(indices)
        elif len(indices) > 3:
            for i in range(1, len(indices) - 1):
                triangles.append([indices[0], indices[i], indices[i + 1]])

    if not triangles:
        return None

    try:
        return BVHTree.FromPolygons(world_vertices, triangles, all_triangles=True)
    except Exception as exc:
        print(f"[NormalBaker] {obj.name}: BVH creation failed: {exc}")
        return None


def smoothstep01(value):
    t = max(0.0, min(1.0, value))
    return t * t * (3.0 - 2.0 * t)


def proximity_mask_factor(distance, overlap_dist, fade_dist, min_mask):
    if distance <= overlap_dist:
        return min_mask
    if distance >= fade_dist:
        return 1.0

    span = fade_dist - overlap_dist
    if span <= 1e-8:
        return 1.0

    t = (distance - overlap_dist) / span
    return min_mask + (1.0 - min_mask) * smoothstep01(t)


def build_occluder_trees(target_objects, current_obj):
    trees = []
    for other in target_objects:
        if other == current_obj:
            continue
        tree = build_world_bvh(other)
        if tree is not None:
            trees.append((other.name, tree))
    return trees


def calculate_proximity_factors(obj, trees, overlap_dist, fade_dist, min_mask):
    mesh = obj.data
    factors = [1.0] * len(mesh.vertices)
    if not trees:
        return factors

    for vertex in mesh.vertices:
        world_position = obj.matrix_world @ vertex.co
        min_distance = float("inf")

        for _, tree in trees:
            result = tree.find_nearest(world_position)
            if result is None:
                continue
            location, _, _, distance = result
            if location is None or distance is None:
                continue

            min_distance = min(min_distance, float(distance))
            if min_distance <= overlap_dist:
                break

        if min_distance != float("inf"):
            factors[vertex.index] = proximity_mask_factor(
                min_distance, overlap_dist, fade_dist, min_mask
            )

    return factors


# ------------------------------------------------------------
# Core Baking Function
# ------------------------------------------------------------

def bake_mesh_tangent_normals(obj, occluder_objects, settings, report_func=None):
    if obj.type != 'MESH':
        return False, "Object is not a mesh"

    mesh = obj.data
    if not mesh.vertices or not mesh.polygons:
        return False, "Mesh is empty"

    # 1. Geometry Check: Must be triangulated
    non_triangles = [poly.index for poly in mesh.polygons if len(poly.vertices) != 3]
    if non_triangles:
        return False, f"Non-triangular face detected (Face #{non_triangles[0]}). Please triangulate first."

    # 2. UV Layer Check
    uv_layer = mesh.uv_layers.active
    if uv_layer is None:
        return False, "Active UV layer not found. Tangent calculation requires UV."

    mesh.update()

    # Split normals calculation fallback for Blender 3.6 (not required in 4.1+)
    if hasattr(mesh, "calc_normals_split"):
        try:
            mesh.calc_normals_split()
        except Exception:
            pass

    try:
        mesh.calc_tangents(uvmap=uv_layer.name)
        mesh.update()
    except Exception as exc:
        return False, f"Failed to compute tangents: {exc}"

    loops = mesh.loops
    vertices = mesh.vertices
    polygons = mesh.polygons

    # 3. Position Welding (Cluster spatially coincident vertices)
    pos_to_cluster = {}
    vert_to_cluster = [0] * len(vertices)
    cluster_vertices = []

    for v in vertices:
        pos_key = (round(v.co.x, 5), round(v.co.y, 5), round(v.co.z, 5))
        if pos_key not in pos_to_cluster:
            cluster_id = len(cluster_vertices)
            pos_to_cluster[pos_key] = cluster_id
            cluster_vertices.append([])
        else:
            cluster_id = pos_to_cluster[pos_key]

        vert_to_cluster[v.index] = cluster_id
        cluster_vertices[cluster_id].append(v.index)

    # 4. Aggregate Face Normals
    cluster_faces = [[] for _ in range(len(cluster_vertices))]

    for poly in polygons:
        loop_indices = list(poly.loop_indices)
        face_normal = poly.normal.normalized()

        for loop_index in loop_indices:
            vertex_index = loops[loop_index].vertex_index
            cluster_id = vert_to_cluster[vertex_index]

            weight = (
                triangle_corner_angle(mesh, loop_indices, loop_index)
                if settings.use_angle_weight else 1.0
            )
            cluster_faces[cluster_id].append((face_normal.copy(), weight))

    # 5. Compute Smooth Object-Space Normals per Cluster
    cluster_smooth_normals = [Vector((0, 0, 1)) for _ in cluster_vertices]

    for cluster_id, entries in enumerate(cluster_faces):
        if not entries:
            continue

        reference = entries[0][0].copy()
        accumulated = Vector((0, 0, 0))

        for face_normal, weight in entries:
            normal = face_normal.copy()
            if settings.hemisphere_align and normal.dot(reference) < 0.0:
                normal.negate()
            accumulated += normal * weight

        cluster_smooth_normals[cluster_id] = normalize_or(accumulated, reference)

    smooth_vertex_normals = [
        cluster_smooth_normals[vert_to_cluster[v.index]] for v in vertices
    ]

    # 6. Prepare Color Attributes & Mask
    normal_attr = get_or_create_color_attribute(mesh, settings.normal_attr_name)
    mask_attr = mesh.color_attributes.get(settings.mask_attr_name)

    if mask_attr is not None and (
        mask_attr.domain != 'CORNER' or mask_attr.data_type != 'FLOAT_COLOR'
    ):
        mesh.color_attributes.remove(mask_attr)
        mask_attr = None

    if mask_attr is None:
        mask_attr = get_or_create_color_attribute(mesh, settings.mask_attr_name)
        for item in mask_attr.data:
            item.color = (1.0, 1.0, 1.0, settings.default_mask)

    if settings.preserve_existing_mask:
        base_masks = [max(0.0, min(1.0, float(item.color[3]))) for item in mask_attr.data]
    else:
        base_masks = [max(0.0, min(1.0, settings.default_mask)) for _ in loops]

    # 7. Proximity Occlusion Mask
    if settings.auto_mask_overlaps:
        trees = build_occluder_trees(occluder_objects, obj)
        proximity_factors = calculate_proximity_factors(
            obj,
            trees,
            settings.overlap_distance,
            settings.fade_distance,
            settings.min_overlap_mask,
        )
    else:
        proximity_factors = [1.0] * len(vertices)

    # 8. Project to Tangent Space and Store in Vertex Color
    for loop_index, loop in enumerate(loops):
        vertex_index = loop.vertex_index
        smooth_normal = smooth_vertex_normals[vertex_index]

        tangent = normalize_or(loop.tangent.copy(), Vector((1, 0, 0)))
        corner_normal = normalize_or(get_corner_normal(mesh, loop_index), Vector((0, 0, 1)))
        sign = loop.bitangent_sign

        # Standard MikkTSpace bitangent reconstruction: sign * (N x T)
        bitangent = corner_normal.cross(tangent) * sign

        if bitangent.length < 1e-8:
            tangent_normal = Vector((0, 0, 1))
        else:
            bitangent.normalize()
            tangent_normal = Vector((
                smooth_normal.dot(tangent),
                smooth_normal.dot(bitangent),
                smooth_normal.dot(corner_normal),
            ))
            tangent_normal = normalize_or(tangent_normal, Vector((0, 0, 1)))

        rgb = encode_normal(tangent_normal)
        alpha = max(0.0, min(1.0, base_masks[loop_index] * proximity_factors[vertex_index]))

        normal_attr.data[loop_index].color = (rgb[0], rgb[1], rgb[2], alpha)

    set_active_color(mesh, normal_attr)

    try:
        mesh.free_tangents()
    except Exception:
        pass

    mesh.update()
    return True, f"Vertices: {len(vertices)}, Loops: {len(loops)}"


# ------------------------------------------------------------
# Properties Definition
# ------------------------------------------------------------

class TangentNormalBakerSettings(PropertyGroup):
    normal_attr_name: StringProperty(
        name="Normal Attribute",
        description="Target vertex color attribute name for tangent space normals",
        default="OutlineNormalTS",
    )
    mask_attr_name: StringProperty(
        name="Mask Attribute",
        description="Source vertex color attribute name storing the alpha mask",
        default="OutlineMask",
    )
    default_mask: FloatProperty(
        name="Default Mask",
        description="Default mask value when no existing mask attribute is found",
        default=1.0,
        min=0.0,
        max=1.0,
    )
    preserve_existing_mask: BoolProperty(
        name="Preserve Existing Mask",
        description="Keep existing alpha channel mask values from the mask attribute",
        default=True,
    )
    use_angle_weight: BoolProperty(
        name="Angle Weighted",
        description="Weight smoothing normals by corner angle to prevent bias on irregular topology",
        default=True,
    )
    hemisphere_align: BoolProperty(
        name="Hemisphere Align",
        description="Flip opposing face normals into the same hemisphere before blending",
        default=True,
    )
    auto_mask_overlaps: BoolProperty(
        name="Suppress Overlaps",
        description="Calculate proximity distances to other meshes and suppress outline width",
        default=True,
    )
    overlap_distance: FloatProperty(
        name="Overlap Distance",
        description="Distance threshold where outline is suppressed to minimum (meters)",
        default=0.003,
        min=0.0,
        precision=4,
        unit='LENGTH',
    )
    fade_distance: FloatProperty(
        name="Fade Distance",
        description="Distance threshold where outline suppression fades to full width (meters)",
        default=0.010,
        min=0.0,
        precision=4,
        unit='LENGTH',
    )
    min_overlap_mask: FloatProperty(
        name="Min Overlap Mask",
        description="Minimum outline factor within the overlap distance",
        default=0.0,
        min=0.0,
        max=1.0,
    )
    export_fbx: BoolProperty(
        name="Export FBX After Bake",
        description="Automatically trigger FBX export after baking finishes",
        default=False,
    )
    fbx_export_path: StringProperty(
        name="FBX Path",
        description="File path where the baked FBX will be saved",
        default="",
        subtype='FILE_PATH',
    )
    export_selected_only: BoolProperty(
        name="Selected Only",
        description="Export only selected objects",
        default=True,
    )
    export_animation: BoolProperty(
        name="Include Animation",
        description="Include animation when exporting FBX",
        default=True,
    )
    export_apply_modifiers: BoolProperty(
        name="Apply Modifiers",
        description="Apply mesh modifiers during FBX export",
        default=False,
    )
    export_triangulate: BoolProperty(
        name="Triangulate Faces",
        description="Triangulate faces during FBX export",
        default=False,
    )


# ------------------------------------------------------------
# Operators
# ------------------------------------------------------------

class OBJECT_OT_bake_tangent_normals(Operator):
    bl_idname = "object.bake_tangent_normals"
    bl_label = "Bake Tangent Space Normals"
    bl_description = "Bake tangent space smoothed normals into vertex color attributes"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.tangent_normal_baker_settings
        selected_meshes = [obj for obj in context.selected_objects if obj.type == 'MESH']

        if not selected_meshes:
            self.report({'WARNING'}, "No mesh objects selected.")
            return {'CANCELLED'}

        occluders = selected_meshes
        success_count = 0

        for obj in selected_meshes:
            success, msg = bake_mesh_tangent_normals(obj, occluders, settings)
            if success:
                success_count += 1
                print(f"[NormalBaker] {obj.name}: Success ({msg})")
            else:
                self.report({'WARNING'}, f"{obj.name}: {msg}")
                print(f"[NormalBaker] {obj.name}: Failed ({msg})")

        self.report({'INFO'}, f"Baking completed: {success_count}/{len(selected_meshes)} meshes processed.")

        # Optional FBX Export
        if settings.export_fbx and settings.fbx_export_path.strip():
            export_path = bpy.path.abspath(settings.fbx_export_path)
            directory = os.path.dirname(export_path)
            if directory:
                os.makedirs(directory, exist_ok=True)

            try:
                bpy.ops.export_scene.fbx(
                    filepath=export_path,
                    use_selection=settings.export_selected_only,
                    object_types={'MESH', 'ARMATURE', 'EMPTY'},
                    use_mesh_modifiers=settings.export_apply_modifiers,
                    mesh_smooth_type='OFF',
                    colors_type='LINEAR',
                    prioritize_active_color=True,
                    use_tspace=True,
                    use_triangles=settings.export_triangulate,
                    add_leaf_bones=False,
                    bake_anim=settings.export_animation,
                    path_mode='AUTO',
                    embed_textures=False,
                )
                self.report({'INFO'}, f"FBX exported to: {export_path}")
            except Exception as exc:
                self.report({'ERROR'}, f"FBX export failed: {exc}")

        return {'FINISHED'}


# ------------------------------------------------------------
# User Interface Panel
# ------------------------------------------------------------

class VIEW3D_PT_tangent_normal_baker(Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Normal Baker'
    bl_label = "Tangent Normal Baker"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.tangent_normal_baker_settings

        # Vertex Color Attributes Section
        box = layout.box()
        box.label(text="Attributes", icon='GROUP_VCOL')
        box.prop(settings, "normal_attr_name")
        box.prop(settings, "mask_attr_name")
        box.prop(settings, "default_mask")
        box.prop(settings, "preserve_existing_mask")

        # Normal Computation Parameters
        box = layout.box()
        box.label(text="Normal Settings", icon='ORIENTATION_NORMAL')
        box.prop(settings, "use_angle_weight")
        box.prop(settings, "hemisphere_align")

        # Proximity Overlap Mask Section
        box = layout.box()
        box.label(text="Overlap Suppression", icon='MOD_SHRINKWRAP')
        box.prop(settings, "auto_mask_overlaps")
        if settings.auto_mask_overlaps:
            col = box.column(align=True)
            col.prop(settings, "overlap_distance")
            col.prop(settings, "fade_distance")
            col.prop(settings, "min_overlap_mask")

        # Optional FBX Export Section
        box = layout.box()
        box.label(text="Export Options", icon='EXPORT')
        box.prop(settings, "export_fbx")
        if settings.export_fbx:
            box.prop(settings, "fbx_export_path")
            col = box.column(align=True)
            col.prop(settings, "export_selected_only")
            col.prop(settings, "export_animation")
            col.prop(settings, "export_apply_modifiers")
            col.prop(settings, "export_triangulate")

        layout.separator()
        layout.operator(
            "object.bake_tangent_normals",
            text="Bake Tangent Space Normals",
            icon='RENDER_STILL',
        )


# ------------------------------------------------------------
# Registration
# ------------------------------------------------------------

classes = (
    TangentNormalBakerSettings,
    OBJECT_OT_bake_tangent_normals,
    VIEW3D_PT_tangent_normal_baker,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.tangent_normal_baker_settings = PointerProperty(
        type=TangentNormalBakerSettings
    )


def unregister():
    del bpy.types.Scene.tangent_normal_baker_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()