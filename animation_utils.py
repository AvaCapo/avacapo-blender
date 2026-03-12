from bpy.utils import escape_identifier
import bpy
from typing import Literal
import tempfile
import os
from mathutils import Matrix
import uuid
from io_anim_bvh.import_bvh import read_bvh, sorted_nodes
from mathutils import Matrix, Euler, Vector
from bpy_extras import anim_utils

from .logger import log
from .rig_utils import infer_rig_type

AnimationType = Literal["bvh_avocapo_v1"]


class Animation:
    """animation, animation data"""

    data: str
    _type: AnimationType

    def __init__(self, data) -> None:
        self.data = data
        self._type = "bvh_avocapo_v1"


def apply_animation(obj: bpy.types.Object, animation_data: Animation):
    """strategy, choose and apply"""
    animation = Animation(animation_data)
    match infer_rig_type(obj), animation._type:
        case "avacapo_v1", "bvh_avocapo_v1":
            apply_bvh(obj, animation_data)
        case _:
            log.error(
                f"cannot apply animation of type {animation._type} to {infer_rig_type}"
            )


def apply_bvh(skeleton: bpy.types.Object, bvh_bytes):
    # https://claude.ai/chat/b30e6841-94a8-40dc-a085-cbefe520c23b
    # this function was written with claude!
    # it was mainly copied from blender sources
    # no, you cannot just use bpy.ops.import_bvh here, we are using function from it
    # we cannot use operators due to syncronicity of operators, inside import_bvh uses ops heavily
    # so we just to apply data in the function, sadly, they have this piece not as function
    # so i copied it from there.
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

    action = bpy.data.actions.new(name="BVH_Action")
    action_slot = action.slots.new(skeleton.id_type, "Slot")
    channelbag = anim_utils.action_ensure_channelbag_for_slot(action, action_slot)

    skeleton.animation_data_create()
    skeleton.animation_data.action = action
    skeleton.animation_data.action_slot = action_slot

    skip_frame = 1
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
                data_path = 'pose.bones["%s"].rotation_euler' % escape_identifier(
                    bvh_node.name
                )
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
                    r = bone_rotation_matrix.to_euler(
                        pose_bone.rotation_mode, prev_euler
                    )
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
