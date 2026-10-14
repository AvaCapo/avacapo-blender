"""Blender orchestration for mapped retargeting.

This module owns the operations between Blender scene data and the portable
``workflow`` module. UI operators may call it, but it contains no panel state
or drawing code.
"""

from __future__ import annotations

import bpy

from . import action_provenance, blender_adapter, blender_motion_writer, known_rig_mapping, mapping, workflow


COORDINATE_CONTRACT_VERSION = 2


def mapping_dict(state) -> dict[str, str]:
    """Read non-empty assignments from Blender's persisted mapping state."""
    return {
        entry.source_joint: entry.target_bone.strip()
        for entry in state.entries
        if entry.source_joint and entry.target_bone.strip()
    }


def ensure_mapping_entries(armature, state) -> None:
    """Create one persisted editable row for every canonical source joint."""
    if armature is None or armature.type != "ARMATURE":
        raise ValueError("Select an armature for retarget mapping.")
    existing = {entry.source_joint for entry in state.entries}
    for source_name in mapping.canonical_source_hierarchy().joint_names:
        if source_name in existing:
            continue
        entry = state.entries.add()
        entry.source_joint = source_name


def initialize_target_mapping(armature, state) -> None:
    """Initialize and validate a selected target without involving panel draw."""
    invalidate_mapping_state(state)
    ensure_mapping_entries(armature, state)
    _try_apply_known_rig_mapping(armature, state)
    refresh_mapping_state(armature, state, mapping_dict(state))
    refresh_active_action_status(armature, state)


def mapping_entry_changed(armature, state) -> None:
    """Validate an explicit user edit and mark any owned Action stale."""
    state.confirmed = False
    state.draft_error = ""
    state.detected_rig = ""
    state.known_detection_attempted = True
    refresh_mapping_state(armature, state, mapping_dict(state))
    _mark_active_action_stale(armature, state)


def apply_auto_mapping(armature, state) -> int:
    """Fill empty rows from Broom's proposal without overwriting user choices."""
    ensure_mapping_entries(armature, state)
    state.known_detection_attempted = True
    source = mapping.canonical_source_hierarchy()
    target = blender_adapter.armature_to_hierarchy(armature, _identity())
    proposal = mapping.suggest_mapping(source, target)
    matches = {match.source: match for match in proposal.matches}
    used_targets = {
        entry.target_bone.strip()
        for entry in state.entries
        if entry.target_bone.strip()
    }
    changed = 0
    state.is_updating_mapping = True
    try:
        for entry in state.entries:
            if entry.target_bone.strip():
                continue
            match = matches.get(entry.source_joint)
            if match is None or match.target in used_targets:
                continue
            entry.target_bone = match.target
            used_targets.add(match.target)
            changed += 1
    finally:
        state.is_updating_mapping = False
    if changed:
        state.confirmed = False
        state.draft_error = ""
        state.detected_rig = ""
        _mark_active_action_stale(armature, state)
    refresh_mapping_state(armature, state, mapping_dict(state))
    return changed


def clear_mapping(armature, state) -> None:
    """Clear persisted assignments while retaining source-joint rows."""
    ensure_mapping_entries(armature, state)
    state.is_updating_mapping = True
    try:
        for entry in state.entries:
            entry.target_bone = ""
    finally:
        state.is_updating_mapping = False
    state.confirmed = False
    state.confirmed_coordinate_contract = 0
    state.draft_error = ""
    state.detected_rig = ""
    state.known_detection_attempted = True
    refresh_mapping_state(armature, state, mapping_dict(state))
    _mark_active_action_stale(armature, state)


def confirm_mapping(armature, state) -> list[str]:
    """Validate and persist confirmation state; return errors when invalid."""
    refresh_mapping_state(armature, state, mapping_dict(state))
    errors = _cached_validation_errors(state)
    if errors:
        state.confirmed = False
        state.draft_error = "\n".join(errors)
        return errors
    state.confirmed = True
    state.confirmed_coordinate_contract = COORDINATE_CONTRACT_VERSION
    state.draft_error = ""
    _mark_active_action_stale(armature, state)
    return []


def mapping_validation(armature, joint_map):
    """Compile and validate one armature/map pair without mutating UI state."""
    source = mapping.canonical_source_hierarchy()
    mapping_errors = []
    coordinate_errors = []
    invalid_sources = set()
    target = None
    canonical_from_armature = None
    try:
        target = blender_adapter.armature_to_hierarchy(armature, _identity())
    except ValueError as error:
        mapping_errors.append(str(error))
    if target is not None:
        mapping_errors.extend(mapping.mapping_errors(source, target, joint_map))
        invalid_sources.update(mapping.invalid_source_joints(source, target, joint_map))
    if not mapping_errors:
        try:
            canonical_from_armature = blender_adapter.canonical_from_armature_mapping(
                armature,
                joint_map,
            )
            target = blender_adapter.armature_to_hierarchy(
                armature,
                canonical_from_armature,
            )
        except ValueError as error:
            coordinate_errors.append(str(error))
    return (
        source,
        target,
        joint_map,
        canonical_from_armature,
        mapping_errors,
        coordinate_errors,
        frozenset(invalid_sources),
    )


def refresh_mapping_state(armature, state, joint_map):
    """Cache current validation data on one Blender mapping state object."""
    result = mapping_validation(armature, joint_map)
    mapping_errors = result[4]
    coordinate_errors = result[5]
    invalid_sources = result[6]
    state.validation_errors = "\n".join((*mapping_errors, *coordinate_errors))
    state.invalid_source_joints = "\n".join(sorted(invalid_sources))
    state.validation_ready = True
    return result


def invalidate_mapping_state(state) -> None:
    """Mark cached UI validation stale after an unvalidated mapping edit."""
    state.validation_errors = ""
    state.invalid_source_joints = ""
    state.validation_ready = False
    state.retarget_action_status = "unknown"
    state.retarget_action_name = ""


def current_retarget_build(armature, joint_map):
    """Return the current compiled target or raise its first actionable error."""
    (
        _source,
        target,
        _joint_map,
        canonical_from_armature,
        mapping_errors,
        coordinate_errors,
        _invalid_sources,
    ) = mapping_validation(armature, joint_map)
    errors = [*mapping_errors, *coordinate_errors]
    if errors or target is None or canonical_from_armature is None:
        raise ValueError(errors[0] if errors else "Could not compile target hierarchy.")
    return target, joint_map, canonical_from_armature


def retarget_into_action(
    armature,
    source_motion,
    action,
    source_provenance,
    joint_map,
):
    """Replace an existing retarget Action only after a staged build succeeds."""
    # TODO(blender-runtime-test): force a failure after staging output and
    # assert that this Action's FCurves/provenance remain unchanged and the
    # temporary Action is removed. Also force a new-output failure before
    # assignment and assert no orphan Action remains. This needs real Blender;
    # a pure Python unit test cannot exercise Action slots/channel bags.
    _target, _joint_map, _canonical_from_armature, result, prepared, provenance_value = (
        _prepared_retarget_output(
            armature,
            source_motion,
            source_provenance,
            joint_map,
        )
    )
    staging_action = bpy.data.actions.new(name=f"__AvaCapo Retarget Stage - {action.name}")
    try:
        blender_motion_writer.apply_prepared_motion(
            armature,
            staging_action,
            prepared,
            assign_to_skeleton=False,
        )
        action_provenance.write_serialized(staging_action, provenance_value)
        previous_provenance = action_provenance.saved_value(action)
        blender_motion_writer.replace_action_slot_from_staging(
            armature,
            action,
            staging_action,
            finalize=lambda: action_provenance.write_serialized(action, provenance_value),
            rollback_finalize=lambda: action_provenance.restore_saved_value(
                action,
                previous_provenance,
            ),
        )
    finally:
        bpy.data.actions.remove(staging_action)
    return result


def create_retargeted_action(
    armature,
    source_motion,
    action_name,
    source_provenance,
    joint_map,
):
    """Create a new Action only after compilation/retarget preparation succeeds."""
    (
        _target,
        _joint_map,
        _canonical_from_armature,
        result,
        prepared,
        provenance_value,
    ) = _prepared_retarget_output(
        armature,
        source_motion,
        source_provenance,
        joint_map,
    )
    action = bpy.data.actions.new(name=action_name)
    animation_data = armature.animation_data
    previous_action = None if animation_data is None else animation_data.action
    previous_slot = None if animation_data is None else getattr(animation_data, "action_slot", None)
    try:
        blender_motion_writer.apply_prepared_motion(
            armature,
            action,
            prepared,
            assign_to_skeleton=False,
        )
        action_provenance.write_serialized(action, provenance_value)
        blender_motion_writer.assign_action(
            armature,
            action,
            blender_motion_writer.action_slot_for_object(action, armature),
        )
    except Exception:
        animation_data = armature.animation_data
        if animation_data is not None and animation_data.action == action:
            animation_data.action = previous_action
            if previous_slot is not None:
                animation_data.action_slot = previous_slot
        if action.users == 0:
            bpy.data.actions.remove(action)
        raise
    return action, result


def _prepared_retarget_output(armature, source_motion, source_provenance, joint_map):
    """Perform every mathematical/output preflight before creating an Action."""
    target, joint_map, canonical_from_armature = current_retarget_build(armature, joint_map)
    result = workflow.retarget_mapped_motion(source_motion, target, joint_map)
    prepared = blender_motion_writer.prepare_retargeted_motion(
        armature,
        result,
        canonical_from_armature=canonical_from_armature,
    )
    provenance_value = action_provenance.serialized(
        target_armature=armature,
        target_hierarchy=target,
        canonical_from_armature=canonical_from_armature,
        joint_map=joint_map,
        source=source_provenance,
    )
    return (
        target,
        joint_map,
        canonical_from_armature,
        result,
        prepared,
        provenance_value,
    )


def retargeted_action_status(armature, action, joint_map):
    """Return provenance status for an Action against the current armature."""
    if action_provenance.read(action) is None:
        return None
    target, joint_map, canonical_from_armature = current_retarget_build(armature, joint_map)
    return action_provenance.current_status(
        action,
        target_hierarchy=target,
        canonical_from_armature=canonical_from_armature,
        joint_map=joint_map,
    )


def refresh_retargeted_action_status(armature, state, action, joint_map):
    """Cache provenance status against the armature's current rest skeleton."""
    status = retargeted_action_status(armature, action, joint_map)
    state.retarget_action_status = "none" if status is None else status
    state.retarget_action_name = "" if action is None else action.name
    return status


def refresh_active_action_status(armature, state):
    """Refresh status for the target armature's active Action, if owned."""
    action = armature.animation_data.action if armature.animation_data else None
    try:
        return refresh_retargeted_action_status(
            armature,
            state,
            action,
            mapping_dict(state),
        )
    except ValueError:
        state.retarget_action_status = "unknown"
        state.retarget_action_name = "" if action is None else action.name
        return None


def _mark_active_action_stale(armature, state) -> None:
    action = armature.animation_data.action if armature.animation_data else None
    if action_provenance.read(action) is not None:
        state.retarget_action_status = "stale"
        state.retarget_action_name = action.name
    else:
        state.retarget_action_status = "unknown"
        state.retarget_action_name = ""


def _try_apply_known_rig_mapping(armature, state) -> str | None:
    """Auto-confirm only a complete deterministic mapping on an untouched rig."""
    if state.known_detection_attempted:
        return None
    state.known_detection_attempted = True
    if any(entry.target_bone.strip() for entry in state.entries):
        return None
    source = mapping.canonical_source_hierarchy()
    target = blender_adapter.armature_to_hierarchy(armature, _identity())
    detected = known_rig_mapping.deterministic_mapping(source, target)
    if detected is None:
        return None
    rig_kind, joint_map = detected
    if mapping.mapping_errors(source, target, joint_map):
        return None
    canonical_from_armature = blender_adapter.canonical_from_armature_mapping(
        armature,
        joint_map,
    )
    target = blender_adapter.armature_to_hierarchy(armature, canonical_from_armature)
    if mapping.mapping_errors(source, target, joint_map):
        return None
    state.is_updating_mapping = True
    try:
        for entry in state.entries:
            entry.target_bone = joint_map[entry.source_joint]
    finally:
        state.is_updating_mapping = False
    state.confirmed = True
    state.confirmed_coordinate_contract = COORDINATE_CONTRACT_VERSION
    state.draft_error = ""
    state.detected_rig = rig_kind
    return rig_kind


def _cached_validation_errors(state) -> list[str]:
    return [error for error in state.validation_errors.splitlines() if error]


def _identity():
    return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
