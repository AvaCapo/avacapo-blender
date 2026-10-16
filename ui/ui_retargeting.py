"""Blender draft/approval UI for explicit server-to-armature mappings."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import bpy
import bpy.props
import bpy.types

from ..animation.retargeting import (
    action_provenance,
    blender_retarget_service,
    blender_source_adapter,
    canonical_source,
    mapping,
    workflow,
)
from ..animation.retargeting.debug import reference_rig


_STATE_KEY = "avacapo_retarget_mapping_ui_classes"
# DEBUG ONLY: bundled Backflip BVH fixture.
_TEST_BVH_PATH = Path(__file__).resolve().parents[1] / "assets" / "tmp_animations" / "blender_backflip.bvh"


def _entry_changed(entry, _context):
    owner = entry.id_data
    state = getattr(owner, "avacapo_retarget_mapping", None)
    if state is not None and not state.is_updating_mapping:
        try:
            blender_retarget_service.mapping_entry_changed(owner, state)
        except ValueError as error:
            state.draft_error = str(error)


class AVACAPO_RetargetMappingEntry(bpy.types.PropertyGroup):
    source_joint: bpy.props.StringProperty()
    target_bone: bpy.props.StringProperty(update=_entry_changed)


class AVACAPO_RetargetMappingState(bpy.types.PropertyGroup):
    entries: bpy.props.CollectionProperty(type=AVACAPO_RetargetMappingEntry)
    confirmed: bpy.props.BoolProperty(default=False)
    confirmed_coordinate_contract: bpy.props.IntProperty(default=0)
    draft_error: bpy.props.StringProperty()
    detected_rig: bpy.props.StringProperty()
    known_detection_attempted: bpy.props.BoolProperty(default=False)
    validation_errors: bpy.props.StringProperty()
    invalid_source_joints: bpy.props.StringProperty()
    validation_ready: bpy.props.BoolProperty(default=False)
    retarget_action_status: bpy.props.StringProperty(default="unknown")
    retarget_action_name: bpy.props.StringProperty()
    is_updating_mapping: bpy.props.BoolProperty(default=False)
    rest_skeleton_dirty: bpy.props.BoolProperty(default=False)


def _target_changed(scene, _context):
    armature = scene.avacapo_retarget_target
    if armature is None:
        return
    try:
        blender_retarget_service.initialize_target_mapping(
            armature,
            armature.avacapo_retarget_mapping,
        )
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
            changed = blender_retarget_service.apply_auto_mapping(
                armature,
                armature.avacapo_retarget_mapping,
            )
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
            blender_retarget_service.clear_mapping(
                armature,
                armature.avacapo_retarget_mapping,
            )
        except ValueError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, "Mapping cleared.")
        return {"FINISHED"}


class AVACAPO_OT_append_avacapo_reference_armature(bpy.types.Operator):
    """DEBUG ONLY: add the bundled AvaCapo reference fixture to this scene."""

    bl_idname = "avacapo.append_avacapo_reference_armature"
    bl_label = "DEBUG ONLY: Add AvaCapo Reference Rig"
    bl_description = "DEBUG ONLY: append the bundled AvaCapo armature and select it as target"

    def execute(self, context):
        try:
            armature = reference_rig.append_avacapo_reference_armature(context.scene)
        except ValueError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        context.scene.avacapo_retarget_target = armature
        armature.select_set(True)
        context.view_layer.objects.active = armature
        self.report({"INFO"}, f"AvaCapo reference rig ready: {armature.name!r}.")
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
        errors = blender_retarget_service.confirm_mapping(armature, state)
        if errors:
            self.report({"ERROR"}, errors[0])
            return {"CANCELLED"}
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
        errors = blender_retarget_service.confirmed_mapping_errors(armature)
        if errors:
            self.report({"ERROR"}, errors[0])
            return {"CANCELLED"}
        try:
            source_motion = workflow.load_bvh_motion(_TEST_BVH_PATH)
            action, result = blender_retarget_service.create_retargeted_action(
                armature,
                source_motion,
                f"AvaCapo Retarget - {_TEST_BVH_PATH.stem}",
                action_provenance.bvh_source(_TEST_BVH_PATH),
                blender_retarget_service.mapping_dict(armature.avacapo_retarget_mapping),
            )
            state = armature.avacapo_retarget_mapping
            state.retarget_action_status = "up_to_date"
            state.retarget_action_name = action.name
            context.view_layer.update()
        except (OSError, RuntimeError, ValueError) as error:
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
        errors = blender_retarget_service.confirmed_mapping_errors(armature)
        if errors:
            self.report({"ERROR"}, errors[0])
            return {"CANCELLED"}
        try:
            source_motion = blender_source_adapter.motion_from_avacapo_action(
                source_armature,
                source_action,
                context.scene,
            )
            action, result = blender_retarget_service.create_retargeted_action(
                armature,
                source_motion,
                f"AvaCapo Retarget - {source_action.name}",
                action_provenance.blender_action_source(source_armature, source_action),
                blender_retarget_service.mapping_dict(armature.avacapo_retarget_mapping),
            )
            state = armature.avacapo_retarget_mapping
            state.retarget_action_status = "up_to_date"
            state.retarget_action_name = action.name
            context.view_layer.update()
        except (OSError, RuntimeError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Created action {action.name!r} ({result.frame_count} frames).")
        return {"FINISHED"}


class AVACAPO_OT_retarget_again(bpy.types.Operator):
    """Overwrite the active retargeted Action using the current target armature."""

    bl_idname = "avacapo.retarget_again"
    bl_label = "Retarget Again"
    bl_description = "Overwrite the active retargeted Action using the current target armature"

    def execute(self, context):
        armature = context.scene.avacapo_retarget_target
        if armature is None or armature.type != "ARMATURE":
            self.report({"ERROR"}, "Select a target armature first.")
            return {"CANCELLED"}
        errors = blender_retarget_service.confirmed_mapping_errors(armature)
        if errors:
            self.report({"ERROR"}, errors[0])
            return {"CANCELLED"}
        action = armature.animation_data.action if armature.animation_data else None
        provenance = action_provenance.read(action)
        if action is None or provenance is None:
            self.report({"ERROR"}, "The active Action is not an AvaCapo retargeted Action.")
            return {"CANCELLED"}
        try:
            source_motion = action_provenance.load_source_motion(provenance, context.scene)
            result = blender_retarget_service.retarget_into_action(
                armature,
                source_motion,
                action,
                provenance["source"],
                blender_retarget_service.mapping_dict(armature.avacapo_retarget_mapping),
            )
            state = armature.avacapo_retarget_mapping
            state.retarget_action_status = "up_to_date"
            state.retarget_action_name = action.name
            context.view_layer.update()
        except (OSError, RuntimeError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Updated action {action.name!r} ({result.frame_count} frames).")
        return {"FINISHED"}


class AVACAPO_OT_refresh_retarget_status(bpy.types.Operator):
    """Refresh cached provenance status without changing an Action."""

    bl_idname = "avacapo.refresh_retarget_status"
    bl_label = "Refresh Retarget Status"
    bl_description = "Check whether the active retargeted Action matches the current armature"

    def execute(self, context):
        armature = context.scene.avacapo_retarget_target
        if armature is None or armature.type != "ARMATURE":
            self.report({"ERROR"}, "Select a target armature first.")
            return {"CANCELLED"}
        action = armature.animation_data.action if armature.animation_data else None
        try:
            status = blender_retarget_service.refresh_retargeted_action_status(
                armature,
                armature.avacapo_retarget_mapping,
                action,
                blender_retarget_service.mapping_dict(armature.avacapo_retarget_mapping),
            )
        except ValueError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        if status is None:
            self.report({"INFO"}, "The active Action is not an AvaCapo retargeted Action.")
        else:
            self.report({"INFO"}, f"Retarget status: {status.replace('_', ' ')}.")
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
        # A failed or incomplete extension registration can leave the panel
        # visible before its Scene properties exist. Do not access them here.
        required_scene_properties = (
            "avacapo_retarget_target",
            "avacapo_retarget_source_armature",
            "avacapo_retarget_source_action",
        )
        if (
            not all(hasattr(scene, name) for name in required_scene_properties)
            or not hasattr(bpy.types.Object, "avacapo_retarget_mapping")
        ):
            layout.label(text="Retarget properties are unavailable; re-enable the extension.", icon="ERROR")
            return
        layout.prop(scene, "avacapo_retarget_target", text="Target Armature")
        # DEBUG ONLY: fixture setup for strict direct-compatibility checks.
        layout.operator("avacapo.append_avacapo_reference_armature", icon="ARMATURE_DATA")
        armature = scene.avacapo_retarget_target
        if armature is None:
            layout.label(text="Choose a custom armature to create a draft.", icon="INFO")
            return
        state = armature.avacapo_retarget_mapping
        structural_errors = blender_retarget_service.mapping_status_errors(armature)
        invalid_sources = blender_retarget_service.cached_invalid_source_joints(state)
        visible_structural_errors = _visible_mapping_errors(structural_errors, state.confirmed)
        errors = list(visible_structural_errors)
        if state.draft_error and state.draft_error not in errors:
            errors.append(state.draft_error)

        _draw_mapping_status(layout, state, errors)
        mapping_buttons = layout.row(align=True)
        mapping_split = mapping_buttons.split(factor=0.5, align=True)
        mapping_split.operator("avacapo.suggest_retarget_mapping", icon="VIEWZOOM")
        mapping_split.operator("avacapo.clear_retarget_mapping", icon="TRASH")
        if state.entries:
            _draw_mapping_entries(
                layout,
                armature,
                state.entries,
                invalid_sources,
                show_missing_required_errors=state.confirmed,
            )
        else:
            layout.label(text="Mapping rows are not initialized. Reselect the target armature.", icon="INFO")
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
        actions.enabled = blender_retarget_service.can_confirm_mapping(armature)
        actions.operator("avacapo.confirm_retarget_mapping", icon="CHECKMARK")
        test_action = layout.row()
        test_action.enabled = blender_retarget_service.is_valid_mapping(armature)
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
    row = box.row()
    row.enabled = (
        blender_retarget_service.is_valid_mapping(armature)
        and scene.avacapo_retarget_source_armature is not None
        and scene.avacapo_retarget_source_action is not None
    )
    row.operator("avacapo.retarget_source_action", icon="PLAY")
    _draw_active_retarget_action_status(box, armature)


def _draw_active_retarget_action_status(layout, armature) -> None:
    """Show cached provenance status without evaluating the armature in draw()."""
    action = armature.animation_data.action if armature.animation_data else None
    if action_provenance.read(action) is None:
        return
    state = armature.avacapo_retarget_mapping
    status = state.retarget_action_status
    if state.retarget_action_name != action.name:
        status = "unknown"
    if status == "up_to_date":
        layout.label(text="Active retargeted Action is up to date", icon="CHECKMARK")
        return
    if status == "stale":
        status_row = layout.row()
        status_row.alert = True
        status_row.label(text="Retarget recommended", icon="ERROR")
        row = layout.row()
        row.enabled = blender_retarget_service.is_valid_mapping(armature)
        row.operator("avacapo.retarget_again", icon="FILE_REFRESH")
        return
    layout.label(text="Retarget status has not been refreshed", icon="INFO")
    row = layout.row()
    row.operator("avacapo.refresh_retarget_status", icon="FILE_REFRESH")


def _draw_mapping_entries(
    layout,
    armature,
    entries,
    invalid_sources,
    *,
    show_missing_required_errors,
):
    """Draw the Source Bone to Target Bone mapping table.

    Required source joints keep the normal label treatment. Optional joints are
    deliberately de-emphasised, while every target selector remains editable.
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
    display_order = {
        source_name: index
        for index, (source_name, _parent) in enumerate(
            canonical_source.CANONICAL_SOURCE_TOPOLOGY
        )
    }
    ordered_entries = sorted(
        entries,
        key=lambda entry: (display_order.get(entry.source_joint, len(display_order)), entry.source_joint),
    )
    for entry in ordered_entries:
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
    AVACAPO_RetargetMappingState,
    AVACAPO_OT_suggest_retarget_mapping,
    AVACAPO_OT_clear_retarget_mapping,
    AVACAPO_OT_append_avacapo_reference_armature,
    AVACAPO_OT_confirm_retarget_mapping,
    AVACAPO_OT_retarget_test_bvh,
    AVACAPO_OT_retarget_source_action,
    AVACAPO_OT_retarget_again,
    AVACAPO_OT_refresh_retarget_status,
    AVACAPO_PT_retarget_mapping,
)


def register():
    unregister()
    registered = []
    try:
        for cls in _CLASSES:
            _register_class_if_needed(cls)
            registered.append(cls)
            # Registration can fail partway through when Blender reloads an
            # extension. Persist enough state for the next unregister() to
            # clean up those partial classes instead of leaving a duplicate.
            bpy.app.driver_namespace[_STATE_KEY] = tuple(registered)
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
    except Exception:
        unregister()
        raise


def unregister():
    registered = _registered_retarget_classes()
    # Remove UI classes first. This prevents Blender from calling panel draw
    # after Scene properties below have gone away.
    for cls in reversed(registered):
        if _is_property_group(cls):
            continue
        _unregister_class_if_needed(cls)
    if hasattr(bpy.types.Scene, "avacapo_retarget_source_action"):
        del bpy.types.Scene.avacapo_retarget_source_action
    if hasattr(bpy.types.Scene, "avacapo_retarget_source_armature"):
        del bpy.types.Scene.avacapo_retarget_source_armature
    if hasattr(bpy.types.Scene, "avacapo_retarget_target"):
        del bpy.types.Scene.avacapo_retarget_target
    if hasattr(bpy.types.Object, "avacapo_retarget_mapping"):
        del bpy.types.Object.avacapo_retarget_mapping
    for cls in reversed(registered):
        if _is_property_group(cls):
            _unregister_class_if_needed(cls)
    bpy.app.driver_namespace.pop(_STATE_KEY, None)


def _registered_retarget_classes():
    """Return every current or stale Retarget UI class Blender may retain."""
    expected_names = frozenset(cls.__name__ for cls in _CLASSES)
    known = []
    seen_ids = set()

    def add(candidate):
        if (
            candidate is None
            or id(candidate) in seen_ids
            or getattr(candidate, "__name__", None) not in expected_names
            or not _is_retarget_ui_class(candidate)
        ):
            return
        seen_ids.add(id(candidate))
        known.append(candidate)

    for cls in bpy.app.driver_namespace.get(_STATE_KEY, []):
        add(cls)
    for cls in _CLASSES:
        add(getattr(bpy.types, cls.__name__, None))
    for base_class in (bpy.types.PropertyGroup, bpy.types.Operator, bpy.types.Panel):
        lookup = getattr(base_class, "bl_rna_get_subclass_py", None)
        if lookup is not None:
            for class_name in expected_names:
                add(lookup(class_name, None))
        for cls in base_class.__subclasses__():
            add(cls)
    return tuple(known)


def _is_retarget_ui_class(cls) -> bool:
    """Whether a class belongs to this UI module, including an older reload."""
    module_name = getattr(cls, "__module__", "")
    return module_name == __name__ or module_name.endswith(".ui.ui_retargeting")


def _is_property_group(cls) -> bool:
    return cls.__name__ in {
        "AVACAPO_RetargetMappingEntry",
        "AVACAPO_RetargetMappingState",
    }


def _unregister_class_if_needed(cls) -> None:
    try:
        bpy.utils.unregister_class(cls)
    except RuntimeError as error:
        message = str(error).lower()
        if "not registered" in message or "missing bl_rna attribute" in message:
            return
        raise


def _register_class_if_needed(cls) -> None:
    """Register after removing a stale class with the same Blender type name."""
    for stale in _registered_retarget_classes():
        if stale.__name__ == cls.__name__:
            _unregister_class_if_needed(stale)
    bpy.utils.register_class(cls)
