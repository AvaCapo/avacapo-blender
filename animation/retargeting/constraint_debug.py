#TODO: remove file after debugging is complete

"""DEBUG ONLY: snapshot user-authored keys before constraint generation.

This intentionally creates visible Blender data.  It is not part of normal
retargeting or Take storage: the copied armature exists only to compare the
keys sent as user constraints with the generated result.
"""

import bpy

from ...timeline import actions


# Hardcoded debug toggle. Move to UI only if this comparison remains useful.
# The copied armature holds the user-authored, non-GENERATED keyframes captured
# immediately before the request. The original armature receives the generated
# Take, so both can be compared in the scene.
DEBUG_SHOW_ARMATURE = True

# DEBUG ONLY: replace user-authored keys with generated keys in the original
# armature. The normal contract preserves user keys, so keep this disabled.
DEBUG_REPLACE_USER_KEYS_WITH_GENERATED = False


def copy_user_key_armature(scene, armature, working_action, working_slot):
    """Create an armature copy with an Action containing only user keys.

    ``Action.copy()`` is Blender's native full-fidelity keyframe copy.  The
    debug Action is then stripped of GENERATED keys in its own Slot; neither
    the source armature nor its Working Action is changed.
    """
    if scene is None:
        raise ValueError("Choose a Blender scene before creating constraint debug data.")
    if armature is None or armature.type != "ARMATURE":
        raise ValueError("Constraint debug source must be an armature.")
    if working_action is None or working_slot is None:
        raise ValueError("Constraint debug requires a Working Action and Slot.")

    slot_index = next(
        (index for index, slot in enumerate(working_action.slots) if slot == working_slot),
        None,
    )
    if slot_index is None:
        raise ValueError("Working Action Slot does not belong to its Action.")

    debug_armature = debug_data = debug_action = None
    try:
        debug_armature = armature.copy()
        debug_data = armature.data.copy()
        debug_armature.data = debug_data
        debug_armature.name = f"{armature.name} Constraints Debug"
        collections = tuple(armature.users_collection) or (scene.collection,)
        for collection in collections:
            collection.objects.link(debug_armature)

        debug_action = working_action.copy()
        debug_action.name = f"{working_action.name} Constraints Debug"
        debug_slot = debug_action.slots[slot_index]
        debug_armature.animation_data_clear()
        animation_data = debug_armature.animation_data_create()
        animation_data.action = debug_action
        animation_data.action_slot = debug_slot

        for curve in actions.read_slot_curves(debug_action, debug_slot).values():
            points = curve.keyframe_points
            removed = False
            for index in range(len(points) - 1, -1, -1):
                if points[index].type == "GENERATED":
                    points.remove(points[index], fast=True)
                    removed = True
            if removed:
                curve.update()
        return debug_armature
    except Exception:
        if debug_armature is not None:
            bpy.data.objects.remove(debug_armature, do_unlink=True)
        if debug_action is not None:
            bpy.data.actions.remove(debug_action)
        if debug_data is not None and debug_data.users == 0:
            bpy.data.armatures.remove(debug_data)
        raise
