"""Persistent custom-rig mappings and the editor opened from Try to Convert."""

from __future__ import annotations

from collections import Counter

import bpy
from mathutils import Matrix, Vector



# Keep this order aligned with the body branches used by Broom's semantic
# projection. The names are the generated AvaCapo BVH joint names.
AVACAPO_TEMPLATE_BONES = (
    "Hips",
    "Spine",
    "Spine1",
    "Spine2",
    "Neck",
    "Head",
    "LeftShoulder",
    "LeftArm",
    "LeftForeArm",
    "LeftHand",
    "RightShoulder",
    "RightArm",
    "RightForeArm",
    "RightHand",
    "LeftUpLeg",
    "LeftLeg",
    "LeftFoot",
    "LeftToe",
    "RightUpLeg",
    "RightLeg",
    "RightFoot",
    "RightToe",
)

REQUIRED_TEMPLATE_BONES = frozenset(
    {
        "Hips",
        "Head",
        "RightArm",
        "LeftArm",
        "RightHand",
        "LeftHand",
        "RightUpLeg",
        "LeftUpLeg",
        "RightFoot",
        "LeftFoot",
    }
)


# Reusable canonical convention for generated AvaCapo BVH.  The orientation
# construction that uses these axes is documented next to the G transform.
# The coordinate system of BVH itself is right-handed (X × Y = Z).  The
# canonical AvaCapo *character* is deliberately mirrored within it: its
# anatomical right points along global -X, while up/forward are +Y/+Z.
# Columns are character right, up, forward expressed in global BVH axes.
CANONICAL_CHARACTER_BASIS = Matrix(
    (
        (-1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
    )
)

_UP_DIRECTION_PAIRS = (
    ("Hips", "Head", 4.0),
    ("Hips", "Neck", 2.0),
    ("Hips", "Spine2", 1.0),
    ("Spine", "Head", 2.0),
    ("Spine2", "Head", 1.0),
)
_RIGHT_DIRECTION_PAIRS = (
    ("LeftShoulder", "RightShoulder", 3.0),
    ("LeftArm", "RightArm", 2.0),
    ("LeftUpLeg", "RightUpLeg", 3.0),
    ("LeftHand", "RightHand", 1.0),
    ("LeftFoot", "RightFoot", 1.0),
)
_BASIS_EPSILON = 1.0e-8
# A rest skeleton is expected to be aligned to its Armature data axes, up to a
# signed axis permutation.  Close scores mean it is not possible to determine
# that permutation reliably from semantic directions alone.
_BASIS_AXIS_AMBIGUITY_MARGIN = 0.15
_AXIS_NAMES = ("X", "Y", "Z")


class AvacapoRigMappingEntry(bpy.types.PropertyGroup):
    template_bone: bpy.props.StringProperty()
    target_bone: bpy.props.StringProperty()


class AvacapoRigBasisEntry(bpy.types.PropertyGroup):
    """The rest/T-pose orientation used to translate BVH local rotations."""

    bone_name: bpy.props.StringProperty()
    parent_name: bpy.props.StringProperty()
    head: bpy.props.FloatVectorProperty(
        size=3,
        default=(0.0, 0.0, 0.0),
    )
    matrix: bpy.props.FloatVectorProperty(
        size=9,
        default=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
    )


class AvacapoRigMapping(bpy.types.PropertyGroup):
    configured: bpy.props.BoolProperty(default=False)
    editing: bpy.props.BoolProperty(default=False)
    # The mapped joints identify up and right, but a symmetric rest skeleton
    # carries no reliable front/back evidence. The artist selects the sign of
    # the only remaining Armature data axis.
    forward_sign: bpy.props.EnumProperty(
        name="Forward direction",
        items=(
            ("POSITIVE", "+", "Use the positive remaining Armature axis as forward"),
            ("NEGATIVE", "-", "Use the negative remaining Armature axis as forward"),
        ),
        default="POSITIVE",
    )
    entries: bpy.props.CollectionProperty(type=AvacapoRigMappingEntry)
    basis_entries: bpy.props.CollectionProperty(type=AvacapoRigBasisEntry)


def mapping_dict(mapping: AvacapoRigMapping) -> dict[str, str]:
    return {
        entry.template_bone: entry.target_bone.strip()
        for entry in mapping.entries
        if entry.template_bone and entry.target_bone.strip()
    }


def _weighted_mapped_direction(
    armature: bpy.types.Object,
    selected: dict[str, str],
    pairs: tuple[tuple[str, str, float], ...],
    label: str,
) -> Vector:
    """Return an anatomical rest-space direction from available mapped pairs."""
    direction = Vector((0.0, 0.0, 0.0))
    used = []
    for start_semantic, end_semantic, weight in pairs:
        start_name = selected.get(start_semantic)
        end_name = selected.get(end_semantic)
        if not start_name or not end_name:
            continue
        start_bone = armature.data.bones.get(start_name)
        end_bone = armature.data.bones.get(end_name)
        if start_bone is None or end_bone is None:
            continue
        vector = end_bone.head_local - start_bone.head_local
        if vector.length <= _BASIS_EPSILON:
            continue
        direction += float(weight) * vector.normalized()
        used.append(f"{start_semantic}->{end_semantic}")
    if direction.length <= _BASIS_EPSILON:
        raise ValueError(
            f"Cannot derive character {label} from mapped rest bones. "
            f"Need at least one usable pair: {', '.join(f'{a}->{b}' for a, b, _ in pairs)}."
        )
    return direction.normalized()


def _select_signed_armature_axis(
    direction: Vector,
    *,
    label: str,
    allowed_axes: tuple[int, ...],
) -> Vector:
    """Snap a semantic direction to one unambiguous Armature data axis.

    The semantic vectors only identify which existing Armature axis means
    character-up or character-right.  They must not introduce a tilted
    canonical frame: the original rest-pose tilt remains in BVH offsets.

    TODO(retarget basis UX): offer an explicit recovery flow when this raises,
    instead of requiring the artist to adjust the rest pose or mapping.
    """
    scores = sorted(
        ((abs(float(direction[axis])), axis) for axis in allowed_axes),
        reverse=True,
    )
    best_score, best_axis = scores[0]
    runner_up_score = scores[1][0] if len(scores) > 1 else 0.0
    if best_score <= _BASIS_EPSILON:
        raise ValueError(
            f"Cannot determine character {label}: mapped direction has no usable "
            "Armature-space component."
        )
    if best_score - runner_up_score < _BASIS_AXIS_AMBIGUITY_MARGIN:
        components = ", ".join(
            f"{_AXIS_NAMES[axis]}={float(direction[axis]):+.3f}"
            for axis in allowed_axes
        )
        raise ValueError(
            f"Cannot determine character {label} axis unambiguously from mapped "
            f"rest bones ({components}). Align the rest pose closer to an Armature "
            "axis, then save the mapping again."
        )
    result = Vector((0.0, 0.0, 0.0))
    result[best_axis] = 1.0 if direction[best_axis] >= 0.0 else -1.0
    return result


def canonical_axes_from_mapping(
    armature: bpy.types.Object,
    mapping: AvacapoRigMapping,
) -> tuple[Vector, Vector, Vector]:
    """Derive target anatomical right/up/forward from mapped rest joints.

    ``Hips -> Head`` identifies the Armature axis that represents character
    up; left-to-right pairs identify character-right. The remaining axis is
    front/back and its sign is selected by the artist. This is an anatomical
    character frame and is allowed to be left-handed relative to Blender/BVH
    global coordinates.
    """
    selected = mapping_dict(mapping)
    up_raw = _weighted_mapped_direction(armature, selected, _UP_DIRECTION_PAIRS, "up")
    up = _select_signed_armature_axis(
        up_raw,
        label="up",
        allowed_axes=(0, 1, 2),
    )
    right_raw = _weighted_mapped_direction(
        armature, selected, _RIGHT_DIRECTION_PAIRS, "right"
    )
    right = _select_signed_armature_axis(
        right_raw,
        label="right",
        allowed_axes=tuple(axis for axis in range(3) if abs(up[axis]) < 0.5),
    )
    remaining_axis = next(
        axis
        for axis in range(3)
        if abs(up[axis]) < 0.5 and abs(right[axis]) < 0.5
    )
    forward = Vector((0.0, 0.0, 0.0))
    forward[remaining_axis] = 1.0 if mapping.forward_sign == "POSITIVE" else -1.0
    return right, up, forward


def target_character_basis_from_mapping(
    armature: bpy.types.Object,
    mapping: AvacapoRigMapping,
) -> Matrix:
    """Return target character axes as columns in Armature data coordinates."""
    right, up, forward = canonical_axes_from_mapping(armature, mapping)
    return Matrix((right, up, forward)).transposed()


def forward_axis_choice_label(
    armature: bpy.types.Object,
    mapping: AvacapoRigMapping,
) -> str:
    """Return the two artist-selectable directions left after up/right detection."""
    right, up, _forward = canonical_axes_from_mapping(armature, mapping)
    axis = next(
        axis
        for axis in range(3)
        if abs(up[axis]) < 0.5 and abs(right[axis]) < 0.5
    )
    return _AXIS_NAMES[axis]


def canonical_from_armature_basis(
    armature: bpy.types.Object,
    mapping: AvacapoRigMapping,
) -> Matrix:
    """Return G: Armature data vectors -> canonical global BVH coordinates.

    Global BVH axes stay right-handed. We first express the target's semantic
    character basis in Armature coordinates, then map it to the fixed
    canonical AvaCapo character basis. This does not assume that either
    character's anatomical right/up/forward triad is right-handed.
    """
    target_character_basis = target_character_basis_from_mapping(armature, mapping)
    return CANONICAL_CHARACTER_BASIS @ target_character_basis.transposed()


def mapping_errors(armature: bpy.types.Object, entries) -> list[str]:
    if armature is None or armature.type != "ARMATURE":
        return ["Select an armature."]

    values = {
        entry.template_bone: entry.target_bone.strip()
        for entry in entries
        if entry.template_bone
    }
    errors = []
    missing = [name for name in AVACAPO_TEMPLATE_BONES if name in REQUIRED_TEMPLATE_BONES and not values.get(name)]
    if missing:
        errors.append("Required mappings: " + ", ".join(missing))

    selected = [value for value in values.values() if value]
    duplicates = sorted(name for name, count in Counter(selected).items() if count > 1)
    if duplicates:
        errors.append("A target bone is assigned more than once: " + ", ".join(duplicates))

    unknown = sorted(name for name in selected if armature.data.bones.get(name) is None)
    if unknown:
        errors.append("Mapped bones no longer exist: " + ", ".join(unknown))
    return errors


def _rest_basis(bone: bpy.types.Bone) -> Matrix:
    """Return the armature-space rest orientation used by Blender pose bones."""
    return Matrix(bone.matrix_local.to_3x3())


def capture_rest_bases(armature: bpy.types.Object, mapping: AvacapoRigMapping) -> None:
    """Persist the T-pose basis of every bone, including unmapped bones.

    Broom's target BVH has identity local bases.  Its solver nevertheless
    produces channels for every target joint, so the Blender bases must be
    captured for the full skeleton, not only for semantic mapping anchors.
    """
    mapping.basis_entries.clear()
    for bone in sorted(armature.data.bones, key=lambda item: item.name):
        entry = mapping.basis_entries.add()
        entry.bone_name = bone.name
        entry.parent_name = bone.parent.name if bone.parent else ""
        entry.head = tuple(bone.head_local)
        entry.matrix = tuple(value for row in _rest_basis(bone) for value in row)


def stored_basis_by_bone(mapping: AvacapoRigMapping) -> dict[str, Matrix]:
    """Load the rest/T-pose bases stored when the mapping was saved."""
    bases = {}
    for entry in mapping.basis_entries:
        values = tuple(entry.matrix)
        if len(values) != 9 or not entry.bone_name:
            continue
        bases[entry.bone_name] = Matrix((values[0:3], values[3:6], values[6:9]))
    return bases


def basis_errors(armature: bpy.types.Object, mapping: AvacapoRigMapping) -> list[str]:
    """Reject a mapping if the saved T-pose no longer describes the armature."""
    stored = stored_basis_by_bone(mapping)
    current_names = {bone.name for bone in armature.data.bones}
    if current_names != set(stored):
        return ["Armature bones changed since the mapping was saved."]

    saved_entries = {entry.bone_name: entry for entry in mapping.basis_entries}
    for bone in armature.data.bones:
        entry = saved_entries.get(bone.name)
        parent_name = bone.parent.name if bone.parent else ""
        if entry is None or entry.parent_name != parent_name:
            return ["Armature hierarchy changed since the mapping was saved."]
        if any(abs(bone.head_local[axis] - entry.head[axis]) > 1e-5 for axis in range(3)):
            return ["Armature rest joint positions changed since the mapping was saved."]
        current = _rest_basis(bone)
        saved = stored[bone.name]
        if any(abs(current[row][column] - saved[row][column]) > 1e-5 for row in range(3) for column in range(3)):
            return ["Armature T-pose changed since the mapping was saved."]
    return []


def is_valid_mapping(armature: bpy.types.Object) -> bool:
    mapping = getattr(armature, "avacapo_rig_mapping", None)
    return bool(
        mapping
        and mapping.configured
        and not mapping_errors(armature, mapping.entries)
        and not basis_errors(armature, mapping)
        and _can_derive_canonical_basis(armature, mapping)
    )


def _can_derive_canonical_basis(
    armature: bpy.types.Object,
    mapping: AvacapoRigMapping,
) -> bool:
    return canonical_basis_error(armature, mapping) is None


def canonical_basis_error(
    armature: bpy.types.Object,
    mapping: AvacapoRigMapping,
) -> str | None:
    """Return the actionable automatic-basis failure, if there is one."""
    try:
        canonical_from_armature_basis(armature, mapping)
    except ValueError as error:
        return str(error)
    return None


def ensure_template_entries(mapping: AvacapoRigMapping) -> None:
    existing = {entry.template_bone for entry in mapping.entries}
    for template_bone in AVACAPO_TEMPLATE_BONES:
        if template_bone not in existing:
            entry = mapping.entries.add()
            entry.template_bone = template_bone


def draw_mapping_editor(layout: bpy.types.UILayout, armature: bpy.types.Object) -> None:
    mapping = armature.avacapo_rig_mapping
    layout.label(text="AvaCapo Template Mapping", icon="SHADERFX")
    layout.label(text="Draft is kept on this armature until you save it.", icon="INFO")
    layout.label(
        text="Up and right are detected from mapping; choose forward below.",
        icon="ORIENTATION_GIMBAL",
    )
    header = layout.row(align=True)
    header.label(text="AvaCapo template bone")
    header.label(text="Character bone")
    duplicates = {
        name
        for name, count in Counter(entry.target_bone.strip() for entry in mapping.entries if entry.target_bone.strip()).items()
        if count > 1
    }
    for entry in mapping.entries:
        row = layout.row(align=True)
        left = row.row()
        left.enabled = entry.template_bone in REQUIRED_TEMPLATE_BONES
        left.label(text=entry.template_bone)
        right = row.row()
        right.alert = bool(entry.target_bone.strip() and entry.target_bone.strip() in duplicates)
        right.prop_search(entry, "target_bone", armature.data, "bones", text="")

    mapping_validation_errors = mapping_errors(armature, mapping.entries)
    if not mapping_validation_errors:
        try:
            remaining_axis = forward_axis_choice_label(armature, mapping)
            basis_box = layout.box()
            basis_box.label(
                text=f"Character forward: choose +{remaining_axis} or -{remaining_axis}",
                icon="ORIENTATION_GIMBAL",
            )
            basis_box.label(
                text="Axes use the armature's local data coordinates, not the 3D View.",
                icon="INFO",
            )
            # TODO(retarget basis UX): label forward choices in the artist-facing
            # 3D View frame by transforming Armature data axes through matrix_world.
            # Validate this with rotated and parented armatures before enabling it.
            axis_choices = basis_box.grid_flow(columns=2, even_columns=True, even_rows=True)
            positive = axis_choices.operator(
                "avacapo.set_mapping_forward_sign",
                text=f"+{remaining_axis} Axis",
                depress=mapping.forward_sign == "POSITIVE",
            )
            positive.forward_sign = "POSITIVE"
            negative = axis_choices.operator(
                "avacapo.set_mapping_forward_sign",
                text=f"-{remaining_axis} Axis",
                depress=mapping.forward_sign == "NEGATIVE",
            )
            negative.forward_sign = "NEGATIVE"
        except ValueError as error:
            mapping_validation_errors.append(str(error))

    errors = mapping_validation_errors
    if not errors:
        basis_error = canonical_basis_error(armature, mapping)
        if basis_error:
            errors.append(basis_error)
    if errors:
        box = layout.box()
        box.alert = True
        for error in errors:
            box.label(text=error, icon="ERROR")
    else:
        layout.label(text="Required mappings are complete.", icon="CHECKMARK")
    layout.label(
        text=f"Save also records the T-pose basis of all {len(armature.data.bones)} bones.",
        icon="BONE_DATA",
    )

    actions = layout.row(align=True)
    actions.enabled = not errors
    actions.operator("avacapo.save_rig_mapping", text="Save Mapping", icon="CHECKMARK")
    layout.operator("avacapo.close_rig_mapping", text="Close (keep draft)", icon="PANEL_CLOSE")


class AVACAPO_OT_edit_rig_mapping(bpy.types.Operator):
    bl_idname = "avacapo.edit_rig_mapping"
    bl_label = "Map AvaCapo Template Bones"
    bl_description = "Assign AvaCapo template bones to this armature"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        armature = context.object
        if armature is None or armature.type != "ARMATURE":
            self.report({"ERROR"}, "Select an armature first.")
            return {"CANCELLED"}
        mapping = armature.avacapo_rig_mapping
        ensure_template_entries(mapping)
        mapping.configured = False
        mapping.editing = True
        return {"FINISHED"}


class AVACAPO_OT_save_rig_mapping(bpy.types.Operator):
    bl_idname = "avacapo.save_rig_mapping"
    bl_label = "Save Mapping"

    def execute(self, context):
        armature = context.object
        if armature is None or armature.type != "ARMATURE":
            self.report({"ERROR"}, "Select an armature first.")
            return {"CANCELLED"}
        mapping = armature.avacapo_rig_mapping
        errors = mapping_errors(armature, mapping.entries)
        if not errors:
            basis_error = canonical_basis_error(armature, mapping)
            if basis_error:
                errors.append(basis_error)
        if errors:
            self.report({"ERROR"}, errors[0])
            return {"CANCELLED"}
        capture_rest_bases(armature, mapping)
        mapping.configured = True
        mapping.editing = False
        self.report({"INFO"}, "Custom rig mapping and T-pose bases saved.")
        return {"FINISHED"}


class AVACAPO_OT_close_rig_mapping(bpy.types.Operator):
    bl_idname = "avacapo.close_rig_mapping"
    bl_label = "Close Mapping"

    def execute(self, context):
        armature = context.object
        if armature is not None and armature.type == "ARMATURE":
            armature.avacapo_rig_mapping.editing = False
        return {"FINISHED"}


class AVACAPO_OT_set_mapping_forward_sign(bpy.types.Operator):
    """Set the artist-selected sign of the remaining forward axis."""

    bl_idname = "avacapo.set_mapping_forward_sign"
    bl_label = "Set Character Forward Axis"
    bl_options = {"INTERNAL"}

    forward_sign: bpy.props.EnumProperty(
        items=(
            ("POSITIVE", "Positive", "Use the positive remaining Armature axis"),
            ("NEGATIVE", "Negative", "Use the negative remaining Armature axis"),
        )
    )

    def execute(self, context):
        armature = context.object
        if armature is None or armature.type != "ARMATURE":
            return {"CANCELLED"}
        armature.avacapo_rig_mapping.forward_sign = self.forward_sign
        return {"FINISHED"}


MAPPING_CLASSES = (
    AvacapoRigMappingEntry,
    AvacapoRigBasisEntry,
    AvacapoRigMapping,
    AVACAPO_OT_edit_rig_mapping,
    AVACAPO_OT_save_rig_mapping,
    AVACAPO_OT_close_rig_mapping,
    AVACAPO_OT_set_mapping_forward_sign,
)
