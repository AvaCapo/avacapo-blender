"""Blender draft/approval UI for explicit server-to-armature mappings."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import sys

import bpy
import bpy.props
import bpy.types

from .. import animation_utils
from . import blender_adapter, blender_artifacts, blender_debug_compare, blender_source_adapter, mapping, workflow


_STATE_KEY = "avacapo_retarget_mapping_ui_classes"
# DEBUG ONLY: bundled local BVH used for a temporary retarget smoke test.
# Remove this path and its operator before the server-animation workflow ships.
_TEST_BVH_PATH = Path(__file__).resolve().parents[2] / "assets" / "tmp_animations" / "blender_backflip.bvh"
_RELOAD_TIMER_KEY = "avacapo_retarget_ui_reload"
_COORDINATE_CONTRACT_VERSION = 2


def _entry_changed(entry, _context):
    owner = entry.id_data
    state = getattr(owner, "avacapo_retarget_mapping", None)
    if state is not None:
        state.confirmed = False
        state.draft_error = ""


class AVACAPO_RetargetMappingEntry(bpy.types.PropertyGroup):
    source_joint: bpy.props.StringProperty()
    target_bone: bpy.props.StringProperty(update=_entry_changed)


class AVACAPO_RetargetRestBasisEntry(bpy.types.PropertyGroup):
    bone_name: bpy.props.StringProperty()
    parent_name: bpy.props.StringProperty()
    head: bpy.props.FloatVectorProperty(size=3, default=(0.0, 0.0, 0.0))
    matrix: bpy.props.FloatVectorProperty(
        size=9,
        default=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
    )


class AVACAPO_RetargetMappingState(bpy.types.PropertyGroup):
    entries: bpy.props.CollectionProperty(type=AVACAPO_RetargetMappingEntry)
    basis_entries: bpy.props.CollectionProperty(type=AVACAPO_RetargetRestBasisEntry)
    confirmed: bpy.props.BoolProperty(default=False)
    confirmed_coordinate_contract: bpy.props.IntProperty(default=0)
    draft_error: bpy.props.StringProperty()


def ensure_mapping_entries(armature) -> None:
    """Ensure that an armature has one editable row for every server joint."""
    if armature is None or armature.type != "ARMATURE":
        raise ValueError("Select an armature for retarget mapping.")
    state = armature.avacapo_retarget_mapping
    source = mapping.canonical_source_hierarchy()
    existing = {entry.source_joint for entry in state.entries}
    for source_name in source.joint_names:
        if source_name in existing:
            continue
        entry = state.entries.add()
        entry.source_joint = source_name


def mapping_dict(state) -> dict[str, str]:
    return {
        entry.source_joint: entry.target_bone.strip()
        for entry in state.entries
        if entry.source_joint and entry.target_bone.strip()
    }


def mapping_validation(armature):
    """Return current validation data for drawing and confirmation.

    The target hierarchy is compiled before retargeting, so an unsupported
    armature (for example, multiple roots) is shown as a mapping error instead
    of failing later in the animation pipeline.
    """
    ensure_mapping_entries(armature)
    state = armature.avacapo_retarget_mapping
    source = mapping.canonical_source_hierarchy()
    joint_map = mapping_dict(state)
    mapping_errors = []
    coordinate_errors = []
    invalid_sources = set()
    target = None
    coordinate_transform = None
    try:
        target = blender_adapter.armature_to_hierarchy(armature, _identity())
    except ValueError as error:
        mapping_errors.append(str(error))
    if target is not None:
        mapping_errors.extend(mapping.mapping_errors(source, target, joint_map))
        invalid_sources.update(mapping.invalid_source_joints(source, target, joint_map))
    if not mapping_errors:
        try:
            coordinate_transform = blender_adapter.canonical_from_armature_mapping(
                armature,
                joint_map,
            )
            # Compile with the real G as a final coordinate-system validation.
            target = blender_adapter.armature_to_hierarchy(armature, coordinate_transform)
        except ValueError as error:
            coordinate_errors.append(str(error))
    return (
        source,
        target,
        joint_map,
        coordinate_transform,
        mapping_errors,
        coordinate_errors,
        frozenset(invalid_sources),
    )


def confirmed_mapping_errors(armature) -> list[str]:
    """Return errors that must reject a supposedly confirmed mapping."""
    state = armature.avacapo_retarget_mapping
    (
        _source,
        _target,
        _joint_map,
        _G,
        mapping_errors,
        coordinate_errors,
        _invalid,
    ) = mapping_validation(armature)
    errors = [*mapping_errors, *coordinate_errors]
    if state.confirmed and state.confirmed_coordinate_contract != _COORDINATE_CONTRACT_VERSION:
        errors.append("Mapping must be reconfirmed for the current coordinate contract.")
    if not state.confirmed:
        errors.insert(0, "Mapping has not been confirmed.")
    if state.confirmed:
        errors.extend(blender_adapter.rest_basis_errors(armature, state.basis_entries))
    return errors


def is_valid_mapping(armature) -> bool:
    """Whether this armature has a confirmed, unchanged mapping."""
    return not confirmed_mapping_errors(armature)


def apply_auto_mapping(armature) -> int:
    """Fill empty rows from bone names, without overwriting user choices."""
    ensure_mapping_entries(armature)
    state = armature.avacapo_retarget_mapping
    source = mapping.canonical_source_hierarchy()
    target = blender_adapter.armature_to_hierarchy(armature, skeleton_from_armature=_identity())
    proposal = mapping.suggest_mapping(source, target)
    matches = {match.source: match for match in proposal.matches}
    used_targets = {
        entry.target_bone.strip()
        for entry in state.entries
        if entry.target_bone.strip()
    }
    changed = 0
    for entry in state.entries:
        if entry.target_bone.strip():
            continue
        match = matches.get(entry.source_joint)
        if match is None or match.target in used_targets:
            continue
        entry.target_bone = match.target
        used_targets.add(match.target)
        changed += 1
    if changed:
        state.confirmed = False
        state.draft_error = ""
    return changed


def clear_mapping(armature) -> None:
    """Remove all assignments while retaining the editable server-joint rows."""
    ensure_mapping_entries(armature)
    state = armature.avacapo_retarget_mapping
    for entry in state.entries:
        entry.target_bone = ""
    state.basis_entries.clear()
    state.confirmed = False
    state.confirmed_coordinate_contract = 0
    state.draft_error = ""


def _identity():
    return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def _target_changed(scene, _context):
    armature = scene.avacapo_retarget_target
    if armature is None:
        return
    try:
        ensure_mapping_entries(armature)
    except ValueError as error:
        armature.avacapo_retarget_mapping.draft_error = str(error)


def _armature_poll(_self, obj):
    return obj is not None and obj.type == "ARMATURE"


class AVACAPO_OT_suggest_retarget_mapping(bpy.types.Operator):
    bl_idname = "avacapo.suggest_retarget_mapping"
    bl_label = "Auto Mapping"
    bl_description = "Fill empty mapping rows from bone names"

    def execute(self, context):
        armature = context.scene.avacapo_retarget_target
        try:
            changed = apply_auto_mapping(armature)
        except ValueError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Auto Mapping filled {changed} empty row(s).")
        return {"FINISHED"}


class AVACAPO_OT_clear_retarget_mapping(bpy.types.Operator):
    bl_idname = "avacapo.clear_retarget_mapping"
    bl_label = "Clear Mapping"
    bl_description = "Remove all selected character bones from this mapping"

    def execute(self, context):
        armature = context.scene.avacapo_retarget_target
        try:
            clear_mapping(armature)
        except ValueError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, "Mapping cleared.")
        return {"FINISHED"}


class AVACAPO_OT_export_retarget_bone_names(bpy.types.Operator):
    bl_idname = "avacapo.export_retarget_bone_names"
    bl_label = "Save Bone Names"
    bl_description = "Save exact target-armature bone names for mapping diagnostics"

    def execute(self, context):
        try:
            output_path = blender_artifacts.export_armature_bone_names(
                context.scene.avacapo_retarget_target
            )
        except ValueError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Saved bone names: {output_path}")
        return {"FINISHED"}


def _reload_retarget_ui_after_operator():
    """Reload after the current operator has returned and released its class."""
    addon_module_name = __package__.split(".", maxsplit=1)[0]
    addon_module = sys.modules.get(addon_module_name)
    if addon_module is not None:
        addon_module.reload_retarget_ui()
    bpy.app.driver_namespace.pop(_RELOAD_TIMER_KEY, None)
    return None


class AVACAPO_OT_reload_retarget_ui(bpy.types.Operator):
    bl_idname = "avacapo.reload_retarget_ui"
    bl_label = "Reload Retarget UI"
    bl_description = "Reload only the Retarget panel without restarting Blender"

    def execute(self, _context):
        previous = bpy.app.driver_namespace.get(_RELOAD_TIMER_KEY)
        if previous is not None and bpy.app.timers.is_registered(previous):
            bpy.app.timers.unregister(previous)
        bpy.app.driver_namespace[_RELOAD_TIMER_KEY] = _reload_retarget_ui_after_operator
        bpy.app.timers.register(_reload_retarget_ui_after_operator, first_interval=0.0)
        return {"FINISHED"}


class AVACAPO_OT_confirm_retarget_mapping(bpy.types.Operator):
    bl_idname = "avacapo.confirm_retarget_mapping"
    bl_label = "Confirm Mapping"
    bl_description = "Validate and approve this mapping for later retargeting"

    def execute(self, context):
        armature = context.scene.avacapo_retarget_target
        if armature is None or armature.type != "ARMATURE":
            self.report({"ERROR"}, "Select an armature first.")
            return {"CANCELLED"}
        state = armature.avacapo_retarget_mapping
        (
            _source,
            _target,
            _joint_map,
            _G,
            mapping_errors,
            coordinate_errors,
            _invalid_sources,
        ) = mapping_validation(armature)
        errors = [*mapping_errors, *coordinate_errors]
        if errors:
            state.confirmed = False
            state.draft_error = "\n".join(errors)
            self.report({"ERROR"}, errors[0])
            return {"CANCELLED"}
        blender_adapter.capture_rest_bases(armature, state.basis_entries)
        state.confirmed = True
        state.confirmed_coordinate_contract = _COORDINATE_CONTRACT_VERSION
        state.draft_error = ""
        self.report({"INFO"}, "Mapping confirmed. It can now be used for retargeting.")
        return {"FINISHED"}


class AVACAPO_OT_retarget_test_bvh(bpy.types.Operator):
    """DEBUG ONLY: retarget the bundled backflip BVH into a target Action."""

    bl_idname = "avacapo.retarget_test_bvh"
    bl_label = "DEBUG ONLY: Create Backflip Test Action"
    bl_description = "DEBUG ONLY: retarget the bundled local BVH into a new Action"

    def execute(self, context):
        armature = context.scene.avacapo_retarget_target
        if armature is None or armature.type != "ARMATURE":
            self.report({"ERROR"}, "Select an armature first.")
            return {"CANCELLED"}
        errors = confirmed_mapping_errors(armature)
        if errors:
            self.report({"ERROR"}, errors[0])
            return {"CANCELLED"}
        try:
            (
                _source,
                target,
                joint_map,
                coordinate_transform,
                mapping_errors,
                coordinate_errors,
                _invalid,
            ) = mapping_validation(armature)
            if mapping_errors or coordinate_errors or target is None or coordinate_transform is None:
                raise ValueError((mapping_errors + coordinate_errors)[0])
            source_motion = workflow.load_bvh_motion(_TEST_BVH_PATH)
            result = workflow.retarget_mapped_motion(source_motion, target, joint_map)
            action = bpy.data.actions.new(name=f"AvaCapo Retarget - {_TEST_BVH_PATH.stem}")
            animation_utils.apply_retargeted_motion(
                armature,
                result,
                action,
                canonical_from_armature=coordinate_transform,
                basis_entries=armature.avacapo_retarget_mapping.basis_entries,
            )
            context.view_layer.update()
        except (OSError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Created action {action.name!r} ({result.frame_count} frames).")
        return {"FINISHED"}


class AVACAPO_OT_retarget_source_action(bpy.types.Operator):
    """Retarget an Action on a supported AvaCapo source armature."""

    bl_idname = "avacapo.retarget_source_action"
    bl_label = "Retarget Source Action"
    bl_description = "Retarget the selected AvaCapo source Action to the character"

    def execute(self, context):
        armature = context.scene.avacapo_retarget_target
        source_armature = context.scene.avacapo_retarget_source_armature
        source_action = context.scene.avacapo_retarget_source_action
        if armature is None or armature.type != "ARMATURE":
            self.report({"ERROR"}, "Select a target armature first.")
            return {"CANCELLED"}
        errors = confirmed_mapping_errors(armature)
        if errors:
            self.report({"ERROR"}, errors[0])
            return {"CANCELLED"}
        try:
            (
                _source,
                target,
                joint_map,
                coordinate_transform,
                mapping_errors,
                coordinate_errors,
                _invalid,
            ) = mapping_validation(armature)
            if mapping_errors or coordinate_errors or target is None or coordinate_transform is None:
                raise ValueError((mapping_errors + coordinate_errors)[0])
            source_motion = blender_source_adapter.motion_from_avacapo_action(
                source_armature,
                source_action,
                context.scene,
            )
            result = workflow.retarget_mapped_motion(source_motion, target, joint_map)
            action = bpy.data.actions.new(name=f"AvaCapo Retarget - {source_action.name}")
            animation_utils.apply_retargeted_motion(
                armature,
                result,
                action,
                canonical_from_armature=coordinate_transform,
                basis_entries=armature.avacapo_retarget_mapping.basis_entries,
            )
            context.view_layer.update()
        except (OSError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Created action {action.name!r} ({result.frame_count} frames).")
        return {"FINISHED"}


class AVACAPO_OT_export_debug_source_motion_comparison(bpy.types.Operator):
    """DEBUG ONLY: save direct-BVH versus imported-Action FK evidence."""

    bl_idname = "avacapo.export_debug_source_motion_comparison"
    bl_label = "Save Source Motion Comparison"
    bl_description = "DEBUG ONLY: compare bundled Backflip BVH with the selected imported source Action"

    def execute(self, context):
        try:
            output_path = blender_debug_compare.export_source_motion_comparison(
                context.scene.avacapo_retarget_source_armature,
                context.scene.avacapo_retarget_source_action,
                context.scene,
                _TEST_BVH_PATH,
            )
        except (OSError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Saved source comparison: {output_path}")
        return {"FINISHED"}


class AVACAPO_PT_retarget_mapping(bpy.types.Panel):
    bl_idname = "AVACAPO_PT_retarget_mapping"
    bl_label = "Retarget Mapping"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "AvaCapo Retarget"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.prop(scene, "avacapo_retarget_target", text="Target Armature")
        armature = scene.avacapo_retarget_target
        if armature is None:
            layout.label(text="Choose a custom armature to create a draft.", icon="INFO")
            return
        state = armature.avacapo_retarget_mapping
        # Do not rely on the PointerProperty update callback for the first
        # draw: Blender can restore a scene property without replaying that
        # callback after an extension reload.
        try:
            ensure_mapping_entries(armature)
        except ValueError as error:
            layout.label(text=str(error), icon="ERROR")
            return
        try:
            (
                _source,
                _target,
                joint_map,
                _coordinate_transform,
                mapping_errors,
                coordinate_errors,
                invalid_sources,
            ) = mapping_validation(armature)
        except ValueError as error:
            joint_map = mapping_dict(state)
            mapping_errors = [str(error)]
            coordinate_errors = []
            invalid_sources = frozenset()
        structural_errors = [*mapping_errors, *coordinate_errors]
        visible_structural_errors = _visible_mapping_errors(structural_errors, state.confirmed)
        errors = list(visible_structural_errors)
        if state.confirmed:
            errors.extend(blender_adapter.rest_basis_errors(armature, state.basis_entries))
        if state.draft_error and state.draft_error not in errors:
            errors.append(state.draft_error)

        _draw_mapping_status(layout, state, errors)
        row = layout.row(align=True)
        split = row.split(factor=0.90, align=True)
        mapping_buttons = split.row(align=True)
        reload_button = split.row(align=True)
        mapping_split = mapping_buttons.split(factor=0.5, align=True)
        mapping_split.operator("avacapo.suggest_retarget_mapping", icon="VIEWZOOM")
        mapping_split.operator("avacapo.clear_retarget_mapping", icon="TRASH")
        reload_button.operator("avacapo.reload_retarget_ui", text="", icon="FILE_REFRESH")
        layout.operator("avacapo.export_retarget_bone_names", icon="FILE_TEXT")
        if state.entries:
            _draw_mapping_entries(
                layout,
                armature,
                state.entries,
                invalid_sources,
                show_missing_required_errors=state.confirmed,
            )
        else:
            # Defensive fallback: keep the status, save and debug controls
            # visible even if Blender fails to initialise a collection after
            # reload. The next draw/operator will retry initialisation.
            layout.label(text="Mapping rows are not initialized yet.", icon="INFO")
        if errors:
            box = layout.box()
            box.alert = True
            for error in errors:
                box.label(text=error, icon="ERROR")
        elif structural_errors:
            layout.label(text="Complete required mappings before saving.", icon="INFO")
        else:
            layout.label(text="Required mappings are complete.", icon="CHECKMARK")
        actions = layout.row(align=True)
        actions.enabled = bool(state.entries) and not structural_errors
        actions.operator("avacapo.confirm_retarget_mapping", icon="CHECKMARK")
        test_action = layout.row()
        test_action.enabled = is_valid_mapping(armature)
        test_action.operator("avacapo.retarget_test_bvh", icon="PLAY")
        _draw_source_action_retarget(layout, scene, armature)


def _visible_mapping_errors(errors, confirmed):
    """Keep an incomplete unsaved draft informative, rather than alarming."""
    if confirmed:
        return errors
    return [error for error in errors if not error.startswith("Required mappings:")]


def _draw_mapping_status(layout, state, errors) -> None:
    """Draw the status before controls so it describes the current armature state."""
    if state.confirmed and not errors:
        layout.label(text="Mapping saved", icon="CHECKMARK")
    elif errors:
        layout.label(text="Mapping has some errors", icon="ERROR")
    else:
        layout.label(text="Mapping not saved", icon="INFO")


def _draw_source_action_retarget(layout, scene, armature) -> None:
    """Draw the supported production input: an imported AvaCapo source Action."""
    box = layout.box()
    box.label(text="AvaCapo Source Animation", icon="ARMATURE_DATA")
    box.prop(scene, "avacapo_retarget_source_armature", text="Source Armature")
    box.prop(scene, "avacapo_retarget_source_action", text="Source Action")
    comparison = box.row()
    comparison.enabled = (
        scene.avacapo_retarget_source_armature is not None
        and scene.avacapo_retarget_source_action is not None
    )
    # DEBUG ONLY: compare this imported Action with the bundled Backflip BVH.
    comparison.operator("avacapo.export_debug_source_motion_comparison", icon="FILE_TEXT")
    row = box.row()
    row.enabled = (
        is_valid_mapping(armature)
        and scene.avacapo_retarget_source_armature is not None
        and scene.avacapo_retarget_source_action is not None
    )
    row.operator("avacapo.retarget_source_action", icon="PLAY")


def _draw_mapping_entries(
    layout,
    armature,
    entries,
    invalid_sources,
    *,
    show_missing_required_errors,
):
    """Draw the legacy mapping-editor table with the current entry schema.

    Required source joints keep the normal label treatment. Optional joints are
    deliberately de-emphasised, while every target selector remains editable.
    This is the interaction model from the previous custom-mapping editor.
    """
    header = layout.row(align=True)
    header.label(text="Source Bone")
    header.label(text="Target Bone")

    duplicates = {
        name
        for name, count in Counter(
            entry.target_bone.strip()
            for entry in entries
            if entry.target_bone.strip()
        ).items()
        if count > 1
    }
    for entry in entries:
        missing_required = (
            entry.source_joint in mapping.REQUIRED_SOURCE_JOINTS
            and not entry.target_bone.strip()
        )
        invalid_assignment = bool(
            entry.source_joint in invalid_sources
            and (show_missing_required_errors or entry.target_bone.strip())
        )
        row = layout.row(align=True)
        left = row.row()
        left.alert = invalid_assignment
        left.enabled = entry.source_joint in mapping.REQUIRED_SOURCE_JOINTS
        left.label(text=entry.source_joint)
        right = row.row()
        right.alert = bool(
            (show_missing_required_errors and missing_required)
            or (
                entry.target_bone.strip() and entry.target_bone.strip() in duplicates
            )
            or invalid_assignment
        )
        right.prop_search(entry, "target_bone", armature.data, "bones", text="")


_CLASSES = (
    AVACAPO_RetargetMappingEntry,
    AVACAPO_RetargetRestBasisEntry,
    AVACAPO_RetargetMappingState,
    AVACAPO_OT_suggest_retarget_mapping,
    AVACAPO_OT_clear_retarget_mapping,
    AVACAPO_OT_export_retarget_bone_names,
    AVACAPO_OT_reload_retarget_ui,
    AVACAPO_OT_confirm_retarget_mapping,
    AVACAPO_OT_retarget_test_bvh,
    AVACAPO_OT_retarget_source_action,
    AVACAPO_OT_export_debug_source_motion_comparison,
    AVACAPO_PT_retarget_mapping,
)


def register():
    unregister()
    registered = []
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
        registered.append(cls)
    bpy.app.driver_namespace[_STATE_KEY] = registered
    bpy.types.Object.avacapo_retarget_mapping = bpy.props.PointerProperty(type=AVACAPO_RetargetMappingState)
    bpy.types.Scene.avacapo_retarget_target = bpy.props.PointerProperty(
        type=bpy.types.Object,
        poll=_armature_poll,
        update=_target_changed,
    )
    bpy.types.Scene.avacapo_retarget_source_armature = bpy.props.PointerProperty(
        type=bpy.types.Object,
        poll=_armature_poll,
    )
    bpy.types.Scene.avacapo_retarget_source_action = bpy.props.PointerProperty(
        type=bpy.types.Action,
    )


def unregister():
    if hasattr(bpy.types.Scene, "avacapo_retarget_source_action"):
        del bpy.types.Scene.avacapo_retarget_source_action
    if hasattr(bpy.types.Scene, "avacapo_retarget_source_armature"):
        del bpy.types.Scene.avacapo_retarget_source_armature
    if hasattr(bpy.types.Scene, "avacapo_retarget_target"):
        del bpy.types.Scene.avacapo_retarget_target
    if hasattr(bpy.types.Object, "avacapo_retarget_mapping"):
        del bpy.types.Object.avacapo_retarget_mapping
    for cls in reversed(bpy.app.driver_namespace.pop(_STATE_KEY, [])):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
