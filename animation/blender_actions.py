"""Small Blender Action, slot, and FCurve access primitives.

These helpers are deliberately independent of retargeting and timeline UI.
Reading never creates Blender animation data or slots; writing callers must
explicitly choose the ``ensure_*`` API when slot creation is intended.
"""


def find_action_slot_by_handle(action, handle):
    """Return an existing Action slot by its numeric handle without mutation."""
    if action is None or handle <= 0:
        return None
    return next((slot for slot in action.slots if slot.handle == handle), None)



def iter_slot_fcurves(action, action_slot):
    """Yield FCurves from one explicit Action slot without changing the Action."""
    if action_slot is None:
        raise ValueError("An explicit Action slot is required to read FCurves.")
    for layer in getattr(action, "layers", ()):
        for strip in getattr(layer, "strips", ()):
            for channelbag in getattr(strip, "channelbags", ()):
                if channelbag.slot_handle == action_slot.handle:
                    yield from channelbag.fcurves

