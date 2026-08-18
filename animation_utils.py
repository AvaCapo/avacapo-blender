from bpy.utils import escape_identifier
import bpy
import uuid
import math
from mathutils import Matrix, Euler, Quaternion, Vector
from bpy_extras import anim_utils
from broom.bvh.schemas import BVHDocument

from .logger import log
from .rig_utils import infer_rig_type
from .config import Config
from .retarget_maps import (
    MIXAMO_RETARGET_BONES,
    MIXAMO_SCALE_REFERENCE_BONES,
    canonical_bone_name,
)

config = Config()


def apply_animation(
    obj: bpy.types.Object,
    document: BVHDocument,
    action: bpy.types.Action,
) -> None:
    match infer_rig_type(obj):
        case "avacapo_bvh_v1":
            apply_bvh(obj, document, action)
        case "mixamo":
            apply_bvh_to_mixamo(obj, document, action)
        case _:
            log.error(f"cannot apply animation to {infer_rig_type}")


def reset_pose_transforms(obj: bpy.types.Object) -> None:
    if obj.type != "ARMATURE" or obj.pose is None:
        return

    for pose_bone in obj.pose.bones:
        pose_bone.location = (0.0, 0.0, 0.0)
        pose_bone.scale = (1.0, 1.0, 1.0)
        pose_bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        pose_bone.rotation_euler = (0.0, 0.0, 0.0)
        pose_bone.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)

    bpy.context.view_layer.update()


def freeze_action_pose(
    obj: bpy.types.Object, action: bpy.types.Action, *, frame: int = 1
) -> bool:
    if obj.type != "ARMATURE" or obj.pose is None:
        return False

    animation_data = obj.animation_data_create()
    action_slots = list(getattr(action, "slots", ()))
    if not action_slots:
        return False

    previous_frame = bpy.context.scene.frame_current
    snapshot: dict[str, dict[str, tuple[float, ...]]] = {}

    try:
        animation_data.action = action
        animation_data.action_slot = action_slots[0]
        bpy.context.scene.frame_set(max(1, int(frame)))
        bpy.context.view_layer.update()

        for pose_bone in obj.pose.bones:
            snapshot[pose_bone.name] = {
                "location": tuple(float(value) for value in pose_bone.location),
                "rotation_quaternion": tuple(
                    float(value) for value in pose_bone.rotation_quaternion
                ),
                "rotation_euler": tuple(float(value) for value in pose_bone.rotation_euler),
                "rotation_axis_angle": tuple(
                    float(value) for value in pose_bone.rotation_axis_angle
                ),
                "scale": tuple(float(value) for value in pose_bone.scale),
            }
    finally:
        animation_data.action = None
        bpy.context.scene.frame_set(previous_frame)

    reset_pose_transforms(obj)

    for pose_bone in obj.pose.bones:
        bone_snapshot = snapshot.get(pose_bone.name)
        if bone_snapshot is None:
            continue
        pose_bone.location = bone_snapshot["location"]
        pose_bone.rotation_quaternion = bone_snapshot["rotation_quaternion"]
        pose_bone.rotation_euler = bone_snapshot["rotation_euler"]
        pose_bone.rotation_axis_angle = bone_snapshot["rotation_axis_angle"]
        pose_bone.scale = bone_snapshot["scale"]

    bpy.context.view_layer.update()
    return True


def _load_reference_rig() -> bpy.types.Object:
    with bpy.data.libraries.load(config.BLEND_PATH, link=False) as (data_from, data_to):
        if config.AVACAPO_RIG_NAME not in data_from.objects:
            raise ValueError(
                f"Object '{config.AVACAPO_RIG_NAME}' not found in '{config.BLEND_NAME}'"
            )
        data_to.objects = [config.AVACAPO_RIG_NAME]

    rig_obj = data_to.objects[0]
    bpy.context.scene.collection.objects.link(rig_obj)
    return rig_obj


def _remove_temporary_object(obj: bpy.types.Object) -> None:
    obj_data = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if obj_data is not None and obj_data.users == 0:
        bpy.data.armatures.remove(obj_data)


def _clear_action_fcurves(action: bpy.types.Action, obj: bpy.types.Object) -> None:
    action_slot = action.slots[f"OB{obj.name}"]
    channelbag = anim_utils.action_ensure_channelbag_for_slot(action, action_slot)
    for fcurve in list(channelbag.fcurves):
        channelbag.fcurves.remove(fcurve)


def _canonical_pose_bone_lookup(obj: bpy.types.Object) -> dict[str, bpy.types.PoseBone]:
    return {canonical_bone_name(bone.name): bone for bone in obj.pose.bones}


def _canonical_data_bone_lookup(obj: bpy.types.Object) -> dict[str, bpy.types.Bone]:
    return {canonical_bone_name(bone.name): bone for bone in obj.data.bones}


def _rotation_curves_for_bone(
    action: bpy.types.Action, bone_name: str
) -> tuple[str | None, dict[int, bpy.types.FCurve]]:
    for data_path, channel_count in (
        (f'pose.bones["{escape_identifier(bone_name)}"].rotation_quaternion', 4),
        (f'pose.bones["{escape_identifier(bone_name)}"].rotation_euler', 3),
    ):
        curves = {
            fcurve.array_index: fcurve
            for fcurve in _iter_action_fcurves(action)
            if fcurve.data_path == data_path
        }
        if len(curves) == channel_count:
            return data_path, curves
    return None, {}


def _location_curves_for_bone(
    action: bpy.types.Action, bone_name: str
) -> tuple[str, dict[int, bpy.types.FCurve]]:
    data_path = f'pose.bones["{escape_identifier(bone_name)}"].location'
    curves = {
        fcurve.array_index: fcurve
        for fcurve in _iter_action_fcurves(action)
        if fcurve.data_path == data_path
    }
    return data_path, curves


def _evaluate_bone_rotation_quaternion(
    obj: bpy.types.Object,
    action: bpy.types.Action,
    bone_name: str,
    frame: float,
) -> Quaternion | None:
    data_path, curves = _rotation_curves_for_bone(action, bone_name)
    if data_path is None or not curves:
        return None

    if data_path.endswith("rotation_quaternion"):
        return Quaternion(
            (
                curves[0].evaluate(frame),
                curves[1].evaluate(frame),
                curves[2].evaluate(frame),
                curves[3].evaluate(frame),
            )
        )

    rotation_mode = obj.pose.bones[bone_name].rotation_mode
    return Euler(
        (
            curves[0].evaluate(frame),
            curves[1].evaluate(frame),
            curves[2].evaluate(frame),
        ),
        rotation_mode,
    ).to_quaternion()


def _rewrite_bone_rotation_quaternions(
    obj: bpy.types.Object,
    action: bpy.types.Action,
    bone_name: str,
    frames: list[float],
    quaternions: list[Quaternion],
) -> None:
    data_path, curves = _rotation_curves_for_bone(action, bone_name)
    if data_path is None or not curves:
        return

    if data_path.endswith("rotation_quaternion"):
        values_by_axis = [[] for _ in range(4)]
        for quaternion in quaternions:
            for axis, value in enumerate(quaternion):
                values_by_axis[axis].append(float(value))
    else:
        rotation_mode = obj.pose.bones[bone_name].rotation_mode
        values_by_axis = [[] for _ in range(3)]
        prev_euler = None
        for quaternion in quaternions:
            euler = quaternion.to_euler(rotation_mode, prev_euler)
            prev_euler = euler
            for axis, value in enumerate(euler):
                values_by_axis[axis].append(float(value))

    for axis, values in enumerate(values_by_axis):
        _rewrite_curve_samples(curves[axis], frames, values)


def _rewrite_curve_samples(
    curve: bpy.types.FCurve,
    frames: list[float],
    values: list[float],
    *,
    interpolation: str = "LINEAR",
) -> None:
    keyframe_points = curve.keyframe_points
    keyframe_points.clear()
    keyframe_points.add(len(frames))
    for index, (frame, value) in enumerate(zip(frames, values)):
        keyframe = keyframe_points[index]
        keyframe.co = (frame, value)
        keyframe.interpolation = interpolation
    curve.update()


def _scale_factor_for_mixamo(source_obj: bpy.types.Object, target_obj: bpy.types.Object) -> float:
    source_bones = _canonical_data_bone_lookup(source_obj)
    target_bones = _canonical_data_bone_lookup(target_obj)

    source_total = 0.0
    target_total = 0.0
    for bone_name in MIXAMO_SCALE_REFERENCE_BONES:
        canonical_name = canonical_bone_name(bone_name)
        source_bone = source_bones.get(canonical_name)
        target_bone = target_bones.get(canonical_name)
        if source_bone is None or target_bone is None:
            continue
        source_total += source_bone.length
        target_total += target_bone.length

    if source_total <= 1e-8 or target_total <= 1e-8:
        return 1.0

    return target_total / source_total


def _rest_basis_matrix(bone: bpy.types.Bone) -> Matrix:
    return bone.matrix_local.to_quaternion().to_matrix()


def _correct_mixamo_rotation_curves(
    source_obj: bpy.types.Object,
    target_obj: bpy.types.Object,
    target_action: bpy.types.Action,
    source_action: bpy.types.Action,
    *,
    frame_start: int,
    frame_end: int,
) -> None:
    frames = [float(frame) for frame in range(frame_start, frame_end + 1)]
    target_hips_name = _canonical_pose_bone_lookup(target_obj).get(canonical_bone_name("Hips"))
    source_hips_name = _canonical_pose_bone_lookup(source_obj).get(canonical_bone_name("Hips"))
    if target_hips_name is None or source_hips_name is None:
        return

    target_hips_name = target_hips_name.name
    source_hips_name = source_hips_name.name

    source_first = _evaluate_bone_rotation_quaternion(source_obj, source_action, source_hips_name, frames[0])
    target_first = _evaluate_bone_rotation_quaternion(target_obj, target_action, target_hips_name, frames[0])
    if source_first is None or target_first is None:
        return

    correction = source_first @ target_first.inverted()
    corrected_quaternions: list[Quaternion] = []
    for frame in frames:
        current = _evaluate_bone_rotation_quaternion(target_obj, target_action, target_hips_name, frame)
        if current is None:
            return
        corrected_quaternions.append((correction @ current).normalized())

    _rewrite_bone_rotation_quaternions(
        target_obj,
        target_action,
        target_hips_name,
        frames,
        corrected_quaternions,
    )


def _correct_mixamo_root_location_curves(
    source_obj: bpy.types.Object,
    target_obj: bpy.types.Object,
    target_action: bpy.types.Action,
    source_action: bpy.types.Action,
    *,
    frame_start: int,
    frame_end: int,
) -> None:
    source_bones = _canonical_data_bone_lookup(source_obj)
    target_bones = _canonical_data_bone_lookup(target_obj)
    canonical_name = canonical_bone_name("Hips")
    source_bone = source_bones.get(canonical_name)
    target_bone = target_bones.get(canonical_name)
    if source_bone is None or target_bone is None:
        return

    _target_data_path, target_curves = _location_curves_for_bone(target_action, target_bone.name)
    if not target_curves:
        return

    scale_factor = _scale_factor_for_mixamo(source_obj, target_obj)
    axis_fix = Matrix.Rotation(math.pi / 2.0, 3, "X")
    frames = [float(frame) for frame in range(frame_start, frame_end + 1)]
    values_by_axis = [[] for _ in range(3)]
    base_target_location = Vector(
        tuple(
            target_curves.get(axis).evaluate(frames[0]) if axis in target_curves else 0.0
            for axis in range(3)
        )
    )
    log.info(f"Mixamo baked root motion delta scale factor: {scale_factor:.4f}")

    for frame in frames:
        target_location = Vector(
            tuple(
                target_curves.get(axis).evaluate(frame) if axis in target_curves else 0.0
                for axis in range(3)
            )
        )
        motion_delta = target_location - base_target_location
        corrected_location = base_target_location + ((axis_fix @ motion_delta) * scale_factor)
        for axis, value in enumerate(corrected_location):
            values_by_axis[axis].append(float(value))

    for axis, values in enumerate(values_by_axis):
        curve = target_curves.get(axis)
        if curve is not None:
            _rewrite_curve_samples(curve, frames, values)


def _postprocess_mixamo_action(
    source_obj: bpy.types.Object,
    target_obj: bpy.types.Object,
    target_action: bpy.types.Action,
    source_action: bpy.types.Action,
    *,
    frame_start: int,
    frame_end: int,
) -> None:
    _correct_mixamo_rotation_curves(
        source_obj,
        target_obj,
        target_action,
        source_action,
        frame_start=frame_start,
        frame_end=frame_end,
    )
    _correct_mixamo_root_location_curves(
        source_obj,
        target_obj,
        target_action,
        source_action,
        frame_start=frame_start,
        frame_end=frame_end,
    )


def _retarget_mixamo_constraints(
    source_obj: bpy.types.Object, target_obj: bpy.types.Object
) -> list[tuple[bpy.types.PoseBone, str]]:
    source_bones = _canonical_pose_bone_lookup(source_obj)
    target_bones = _canonical_pose_bone_lookup(target_obj)
    added_constraints: list[tuple[bpy.types.PoseBone, str]] = []

    for bone_name in MIXAMO_RETARGET_BONES:
        canonical_name = canonical_bone_name(bone_name)
        source_bone = source_bones.get(canonical_name)
        target_bone = target_bones.get(canonical_name)
        if source_bone is None or target_bone is None:
            continue

        if canonical_name == canonical_bone_name("Hips"):
            constraint = target_bone.constraints.new("COPY_LOCATION")
            constraint.name = f"AVACAPO_RETARGET_LOC_{bone_name}"
            constraint.target = source_obj
            constraint.subtarget = source_bone.name
            # Root motion must follow world movement, not the target hip bone's local axes.
            constraint.target_space = "WORLD"
            constraint.owner_space = "WORLD"
            added_constraints.append((target_bone, constraint.name))

        constraint = target_bone.constraints.new("COPY_ROTATION")
        constraint.name = f"AVACAPO_RETARGET_ROT_{bone_name}"
        constraint.target = source_obj
        constraint.subtarget = source_bone.name
        constraint.target_space = "POSE"
        constraint.owner_space = "POSE"
        added_constraints.append((target_bone, constraint.name))

    return added_constraints


def _remove_constraints(
    constraints_to_remove: list[tuple[bpy.types.PoseBone, str]],
) -> None:
    for pose_bone, constraint_name in constraints_to_remove:
        constraint = pose_bone.constraints.get(constraint_name)
        if constraint is not None:
            pose_bone.constraints.remove(constraint)


def _bake_target_action(
    target_obj: bpy.types.Object,
    action: bpy.types.Action,
    *,
    frame_start: int,
    frame_end: int,
) -> None:
    _clear_action_fcurves(action, target_obj)

    target_obj.animation_data_create()
    target_obj.animation_data.action = action
    target_obj.animation_data.action_slot = action.slots[f"OB{target_obj.name}"]

    view_layer = bpy.context.view_layer
    selected_objects = list(bpy.context.selected_objects)
    active_object = view_layer.objects.active

    if active_object is not None and active_object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")

    try:
        bpy.ops.object.select_all(action="DESELECT")
        target_obj.select_set(True)
        view_layer.objects.active = target_obj
        bpy.ops.object.mode_set(mode="POSE")
        bpy.ops.pose.select_all(action="SELECT")
        bpy.ops.nla.bake(
            frame_start=frame_start,
            frame_end=frame_end,
            step=1,
            only_selected=True,
            visual_keying=True,
            clear_constraints=False,
            clear_parents=False,
            use_current_action=True,
            clean_curves=False,
            bake_types={"POSE"},
        )
    finally:
        if target_obj.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        for obj in selected_objects:
            if obj.name in bpy.data.objects:
                obj.select_set(True)
        if active_object is not None and active_object.name in bpy.data.objects:
            view_layer.objects.active = active_object


def apply_bvh_to_mixamo(
    target_obj: bpy.types.Object,
    document: BVHDocument,
    target_action: bpy.types.Action,
) -> None:
    reset_pose_transforms(target_obj)

    source_obj = _load_reference_rig()
    source_obj.name = f"{config.AVACAPO_RIG_NAME}_retarget_{uuid.uuid4().hex[:6]}"
    source_obj.matrix_world = target_obj.matrix_world.copy()
    source_obj.hide_set(True)
    source_obj.hide_viewport = True
    source_obj.hide_render = True

    source_action = bpy.data.actions.new(name=f"__avacapo_mixamo_source_{uuid.uuid4().hex[:8]}")
    source_action.slots.new(source_obj.id_type, name=source_obj.name)

    constraints_to_remove: list[tuple[bpy.types.PoseBone, str]] = []

    try:
        apply_bvh(source_obj, document, source_action)
        constraints_to_remove = _retarget_mixamo_constraints(source_obj, target_obj)
        log.info(f"Mixamo retarget constraints created: {len(constraints_to_remove)}")
        if len(constraints_to_remove) < 6:
            raise RuntimeError("Mixamo retarget failed: not enough matching bones were found.")
        frame_start, frame_end = source_action.frame_range
        _bake_target_action(
            target_obj,
            target_action,
            frame_start=max(1, int(round(frame_start))),
            frame_end=max(1, int(round(frame_end))),
        )
        _postprocess_mixamo_action(
            source_obj,
            target_obj,
            target_action,
            source_action,
            frame_start=max(1, int(round(frame_start))),
            frame_end=max(1, int(round(frame_end))),
        )
    finally:
        _remove_constraints(constraints_to_remove)
        if source_action.name in bpy.data.actions:
            bpy.data.actions.remove(source_action)
        if source_obj.name in bpy.data.objects:
            _remove_temporary_object(source_obj)


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


def apply_bvh(
    skeleton: bpy.types.Object,
    document: BVHDocument,
    action: bpy.types.Action,
) -> None:
    """Apply an in-memory BVH document to an action without temporary files."""

    action_slot = action.slots[f"OB{skeleton.name}"]
    log.debug(
        "Applying %s in-memory BVH frames to %s",
        document.frame_count,
        skeleton.name,
    )

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

    arm_data = skeleton.data
    pose_bones = skeleton.pose.bones
    rotation_orders: dict[str, str] = {}

    for joint in document.joints:
        pose_bone = pose_bones.get(joint.name)
        if pose_bone is None:
            continue

        rotation_order = "".join(
            channel[0].upper()
            for channel in joint.channels
            if channel.endswith("rotation")
        )
        if rotation_order and len(rotation_order) != 3:
            raise ValueError(
                f"Joint {joint.name!r} must have three BVH rotation channels"
            )
        rotation_orders[joint.name] = rotation_order or "XYZ"

        if rotate_mode == "NATIVE":
            pose_bone.rotation_mode = rotation_orders[joint.name]
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

    frame_count = document.frame_count
    keyframe_times = [float(1 + index) for index in range(frame_count)]

    for joint in document.joints:
        pose_bone = pose_bones.get(joint.name)
        if pose_bone is None:
            continue

        bone_rest_matrix = arm_data.bones[joint.name].matrix_local.to_3x3()
        bone_rest_matrix_inv = Matrix(bone_rest_matrix)
        bone_rest_matrix_inv.invert()
        bone_rest_matrix_inv.resize_4x4()
        bone_rest_matrix.resize_4x4()

        position_channels = {
            channel[0].upper(): joint.channel_start + offset
            for offset, channel in enumerate(joint.channels)
            if channel.endswith("position")
        }
        if position_channels:
            data_path = 'pose.bones["%s"].location' % escape_identifier(joint.name)
            locations = []
            for frame_index in range(frame_count):
                source_location = Vector(
                    tuple(
                        float(
                            document.motion_values[
                                frame_index,
                                position_channels[axis],
                            ]
                        )
                        if axis in position_channels
                        else 0.0
                        for axis in "XYZ"
                    )
                )
                locations.append(
                    (
                        bone_rest_matrix_inv
                        @ Matrix.Translation(source_location - Vector(joint.offset))
                    ).to_translation()
                )

            for axis_index in range(3):
                curve = channelbag.fcurves.new(
                    data_path=data_path,
                    index=axis_index,
                    group_name=joint.name,
                )
                curve.keyframe_points.add(frame_count)
                for frame_index in range(frame_count):
                    curve.keyframe_points[frame_index].co = (
                        keyframe_times[frame_index],
                        locations[frame_index][axis_index],
                    )
                    curve.keyframe_points[frame_index].interpolation = "LINEAR"

        rotation_channels = {
            channel[0].upper(): joint.channel_start + offset
            for offset, channel in enumerate(joint.channels)
            if channel.endswith("rotation")
        }
        if not rotation_channels:
            continue

        if rotate_mode == "QUATERNION":
            data_path = 'pose.bones["%s"].rotation_quaternion' % escape_identifier(
                joint.name
            )
            channel_count = 4
        else:
            data_path = 'pose.bones["%s"].rotation_euler' % escape_identifier(
                joint.name
            )
            channel_count = 3

        rotations = []
        previous_euler = Euler((0.0, 0.0, 0.0))
        for frame_index in range(frame_count):
            source_rotation = tuple(
                math.radians(
                    float(
                        document.motion_values[
                            frame_index,
                            rotation_channels[axis],
                        ]
                    )
                )
                for axis in "XYZ"
            )
            bone_rotation_matrix = (
                bone_rest_matrix_inv
                @ Euler(
                    source_rotation,
                    rotation_orders[joint.name][::-1],
                ).to_matrix().to_4x4()
                @ bone_rest_matrix
            )
            if rotate_mode == "QUATERNION":
                rotations.append(bone_rotation_matrix.to_quaternion())
            else:
                rotation = bone_rotation_matrix.to_euler(
                    pose_bone.rotation_mode,
                    previous_euler,
                )
                rotations.append(rotation)
                previous_euler = rotation

        for axis_index in range(channel_count):
            curve = channelbag.fcurves.new(
                data_path=data_path,
                index=axis_index,
                group_name=joint.name,
            )
            curve.keyframe_points.add(frame_count)
            for frame_index in range(frame_count):
                curve.keyframe_points[frame_index].co = (
                    keyframe_times[frame_index],
                    rotations[frame_index][axis_index],
                )
                curve.keyframe_points[frame_index].interpolation = "LINEAR"


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
