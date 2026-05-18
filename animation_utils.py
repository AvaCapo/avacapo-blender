from bpy.utils import escape_identifier
import bpy
from typing import Literal
import tempfile
import os
from io_anim_bvh.import_bvh import read_bvh, sorted_nodes
from mathutils import Matrix, Euler, Vector
from bpy_extras import anim_utils

from .logger import log
from .rig_utils import infer_rig_type

# action and slots are different from blender 4.4
# now action is animation for MULTIPLE objects
# which makes a structure called "slot"
# https://claude.ai/chat/9a507fcf-bcd6-4225-bb25-6b80c7ae423e


def apply_animation(obj: bpy.types.Object, bvh_bytes, action: bpy.types.Action):
    match infer_rig_type(obj):
        case "avacapo_bvh_v1":
            apply_bvh(obj, bvh_bytes, action)
        case _:
            log.error(f"cannot apply animation to {infer_rig_type}")


def _iter_action_fcurves(action: bpy.types.Action):
    if hasattr(action, "fcurves"):
        yield from action.fcurves

    for layer in getattr(action, "layers", ()):
        for strip in getattr(layer, "strips", ()):
            for channelbag in getattr(strip, "channelbags", ()):
                yield from channelbag.fcurves


def _root_location_data_path(
    obj: bpy.types.Object, action: bpy.types.Action
) -> str | None:
    location_paths = {
        fcurve.data_path
        for fcurve in _iter_action_fcurves(action)
        if fcurve.data_path.startswith('pose.bones["') and fcurve.data_path.endswith('"].location')
    }
    if not location_paths:
        return None
    if len(location_paths) == 1:
        return next(iter(location_paths))

    root_bone_names = {bone.name for bone in obj.data.bones if bone.parent is None}
    for bone_name in root_bone_names:
        candidate = f'pose.bones["{escape_identifier(bone_name)}"].location'
        if candidate in location_paths:
            return candidate

    return sorted(location_paths)[0]


def _location_curves_by_axis(
    obj: bpy.types.Object, action: bpy.types.Action
) -> dict[int, bpy.types.FCurve]:
    data_path = _root_location_data_path(obj, action)
    if data_path is None:
        return {}

    return {
        fcurve.array_index: fcurve
        for fcurve in _iter_action_fcurves(action)
        if fcurve.data_path == data_path
    }


def evaluate_root_location(
    obj: bpy.types.Object, action: bpy.types.Action, frame: float
) -> Vector | None:
    curves = _location_curves_by_axis(obj, action)
    if not curves:
        return None

    return Vector(tuple(curves.get(axis).evaluate(frame) if axis in curves else 0.0 for axis in range(3)))


def offset_root_location(
    obj: bpy.types.Object,
    action: bpy.types.Action,
    *,
    source_frame: float,
    target_location: Vector,
) -> bool:
    curves = _location_curves_by_axis(obj, action)
    if not curves:
        return False

    source_location = Vector(
        tuple(curves.get(axis).evaluate(source_frame) if axis in curves else 0.0 for axis in range(3))
    )
    delta = target_location - source_location

    if delta.length_squared == 0.0:
        return True

    for axis, axis_delta in enumerate(delta):
        curve = curves.get(axis)
        if curve is None:
            continue

        for keyframe in curve.keyframe_points:
            keyframe.co[1] += axis_delta
            keyframe.handle_left[1] += axis_delta
            keyframe.handle_right[1] += axis_delta
        curve.update()

    return True


def apply_bvh(skeleton: bpy.types.Object, bvh_bytes, action):
    action_slot = action.slots[f"OB{skeleton.name}"]
    # https://claude.ai/chat/b30e6841-94a8-40dc-a085-cbefe520c23b
    """which is the final step - just apply animation"""
    log.debug(f"""
        applying animation:
        {skeleton.name}
        {len(bvh_bytes)}
    """)

    first_bone = next(iter(skeleton.pose.bones), None)
    if first_bone and first_bone.rotation_mode == "QUATERNION":
        rotate_mode = "QUATERNION"
    elif first_bone and first_bone.rotation_mode in {
        "XYZ",
        "XZY",
        "YXZ",
        "YZX",
        "ZXY",
        "ZYX",
    }:
        rotate_mode = first_bone.rotation_mode
    else:
        rotate_mode = "NATIVE"

    with tempfile.NamedTemporaryFile(suffix=".bvh", delete=False) as f:
        f.write(bvh_bytes)
        tmp = f.name
    bvh_nodes, bvh_frame_time, _ = read_bvh(bpy.context, tmp)
    os.unlink(tmp)

    bvh_nodes_list = sorted_nodes(bvh_nodes)
    arm_data = skeleton.data
    pose_bones = skeleton.pose.bones

    for bvh_node in bvh_nodes_list:
        pose_bone = pose_bones.get(bvh_node.name)
        if pose_bone is None:
            continue
        if rotate_mode == "NATIVE":
            pose_bone.rotation_mode = bvh_node.rot_order_str
        elif rotate_mode == "QUATERNION":
            pose_bone.rotation_mode = "QUATERNION"
        else:
            pose_bone.rotation_mode = rotate_mode

    channelbag = anim_utils.action_ensure_channelbag_for_slot(action, action_slot)
    for fcurve in list(channelbag.fcurves):
        channelbag.fcurves.remove(fcurve)

    skeleton.animation_data_create()
    skeleton.animation_data.action = action
    skeleton.animation_data.action_slot = action_slot

    # Keep the very first BVH sample so the clip starts from the true source pose.
    skip_frame = 0
    num_frame = len(next(iter(bvh_nodes_list)).anim_data) - skip_frame
    time = [float(1 + i) for i in range(num_frame)]

    for bvh_node in bvh_nodes_list:
        pose_bone = pose_bones.get(bvh_node.name)
        if pose_bone is None:
            continue

        bone_rest_matrix = arm_data.bones[bvh_node.name].matrix_local.to_3x3()
        bone_rest_matrix_inv = Matrix(bone_rest_matrix)
        bone_rest_matrix_inv.invert()
        bone_rest_matrix_inv.resize_4x4()
        bone_rest_matrix.resize_4x4()

        if bvh_node.has_loc:
            data_path = 'pose.bones["%s"].location' % escape_identifier(bvh_node.name)
            location = [
                (
                    bone_rest_matrix_inv
                    @ Matrix.Translation(
                        Vector(bvh_node.anim_data[frame_i + skip_frame][:3])
                        - bvh_node.rest_head_local
                    )
                ).to_translation()
                for frame_i in range(num_frame)
            ]
            for axis_i in range(3):
                curve = channelbag.fcurves.new(
                    data_path=data_path, index=axis_i, group_name=bvh_node.name
                )
                curve.keyframe_points.add(num_frame)
                for frame_i in range(num_frame):
                    curve.keyframe_points[frame_i].co = (
                        time[frame_i],
                        location[frame_i][axis_i],
                    )
                    curve.keyframe_points[frame_i].interpolation = "LINEAR"

        if bvh_node.has_rot:
            if rotate_mode == "QUATERNION":
                data_path = 'pose.bones["%s"].rotation_quaternion' % escape_identifier(
                    bvh_node.name
                )
                num_channels = 4
            else:
                data_path = 'pose.bones["%s"].rotation_euler' % escape_identifier(bvh_node.name)
                num_channels = 3

            rotate = []
            prev_euler = Euler((0.0, 0.0, 0.0))
            for frame_i in range(num_frame):
                bvh_rot = bvh_node.anim_data[frame_i + skip_frame][3:]
                bone_rotation_matrix = (
                    bone_rest_matrix_inv
                    @ Euler(bvh_rot, bvh_node.rot_order_str[::-1]).to_matrix().to_4x4()
                    @ bone_rest_matrix
                )
                if rotate_mode == "QUATERNION":
                    rotate.append(bone_rotation_matrix.to_quaternion())
                else:
                    r = bone_rotation_matrix.to_euler(pose_bone.rotation_mode, prev_euler)
                    rotate.append(r)
                    prev_euler = r

            for axis_i in range(num_channels):
                curve = channelbag.fcurves.new(
                    data_path=data_path, index=axis_i, group_name=bvh_node.name
                )
                curve.keyframe_points.add(num_frame)
                for frame_i in range(num_frame):
                    curve.keyframe_points[frame_i].co = (
                        time[frame_i],
                        rotate[frame_i][axis_i],
                    )
                    curve.keyframe_points[frame_i].interpolation = "LINEAR"


# why obj_name and not obj? because threading and stale data:
# read gotcha in documentation
def find_attempt(obj_name: str, attempt_uid: str):  # -> attempt(runtime type) | none
    obj = bpy.data.objects[obj_name]
    if obj is None:
        return None
    return next(
        (
            attempt
            for clip in obj.avacapo_clips.clips
            for attempt in clip.attempts
            if attempt.uid == attempt_uid
        ),
        None,
    )
