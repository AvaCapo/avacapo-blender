import bpy
from bpy.app.handlers import persistent
from typing import Literal, TypeAlias

from .config import Config
from .logger import log
from .retarget_maps import MIXAMO_REQUIRED_BONES, canonical_bone_name

config = Config()

RigType = Literal["avacapo_bvh_v1", "mixamo", "unknown"]

# https://claude.ai/chat/aa912047-04f3-4aa8-ba49-b8a4d4309936

# Recursive nested dict: { bone_name: { child_name: {...} } }
BoneTree: TypeAlias = dict[str, "BoneTree"]
AVACAPO_BVH_V1_BONE_TREE: BoneTree | None = None


def collect_bone_tree(armature_obj: bpy.types.Object) -> BoneTree:
    """
    Collect the full bone hierarchy from an armature into a nested dict tree.
    Keys are bone names, values are their children subtrees.
    """
    arm = armature_obj.data

    def build_subtree(bone: bpy.types.Bone) -> BoneTree:
        return {child.name: build_subtree(child) for child in bone.children}

    # Start from root bones only (no parent)
    return {bone.name: build_subtree(bone) for bone in arm.bones if bone.parent is None}


def _find_in_descendants(bone: bpy.types.Bone, name: str) -> bpy.types.Bone | None:
    """Recursively search for a bone by name among all descendants."""
    for child in bone.children:
        if child.name == name:
            return child
        found = _find_in_descendants(child, name)
        if found:
            return found
    return None


def check_if_same_rigtype(
    armature_obj: bpy.types.Object,
    bone_tree: BoneTree,
    *,
    verbose: bool = False,
) -> bool:
    """
    Check if an armature matches the structure described by bone_tree.

    Rules:
    - Every bone present in bone_tree must exist in the armature.
    - Parent → child relationships must be respected (child must be a
      descendant of its parent), but extra intermediate bones are allowed.
    - Bones that exist in the armature but NOT in bone_tree are ignored.

    Args:
        armature_obj: The armature object to test.
        bone_tree:    Reference structure produced by collect_bone_tree().
        verbose:      If True, print the first mismatch found.

    Returns:
        True if the armature satisfies the reference structure.
    """
    arm = armature_obj.data

    def match_subtree(parent_bone: bpy.types.Bone, subtree: BoneTree) -> bool:
        for child_name, child_subtree in subtree.items():
            # Child must appear *somewhere* under parent_bone
            child_bone = _find_in_descendants(parent_bone, child_name)
            if child_bone is None:
                if verbose:
                    print(
                        f"[RigCheck] MISSING: '{child_name}' not found under '{parent_bone.name}'"
                    )
                return False
            # Recurse into the expected children of this bone
            if not match_subtree(child_bone, child_subtree):
                return False
        return True

    for root_name, subtree in bone_tree.items():
        root_bone = arm.bones.get(root_name)
        if root_bone is None:
            if verbose:
                print(f"[RigCheck] MISSING root bone: '{root_name}'")
            return False
        if not match_subtree(root_bone, subtree):
            return False

    return True


def load_bone_tree(rig_name: str) -> BoneTree:

    with bpy.data.libraries.load(config.BLEND_PATH, link=False) as (data_from, data_to):
        if rig_name not in data_from.objects:
            raise ValueError(f"Object '{rig_name}' not found in '{config.BLEND_NAME}'")
        data_to.objects = [rig_name]

    rig_obj = data_to.objects[0]

    scene = bpy.data.scenes[0]  # <-- instead of bpy.context.scene
    scene.collection.objects.link(rig_obj)

    tree = collect_bone_tree(rig_obj)

    scene.collection.objects.unlink(rig_obj)
    bpy.data.objects.remove(rig_obj, do_unlink=True)

    return tree


@persistent
def _init_bone_trees_once(scene, depsgraph):
    global AVACAPO_BVH_V1_BONE_TREE
    AVACAPO_BVH_V1_BONE_TREE = load_bone_tree(config.AVACAPO_RIG_NAME)
    log.debug("Bone trees initialized:", AVACAPO_BVH_V1_BONE_TREE)
    bpy.app.handlers.depsgraph_update_post.remove(_init_bone_trees_once)


def infer_rig_type(obj: bpy.types.Object) -> RigType:
    # log.debug(
    # f"infer rig type: matching {obj} against AVACAPO_BVH_V1_BONE_TREE")
    if obj.type != "ARMATURE":
        log.error(f"{obj.name} is not armature, cannot infer rig type")
        return "unknown"
    if AVACAPO_BVH_V1_BONE_TREE and check_if_same_rigtype(obj, AVACAPO_BVH_V1_BONE_TREE):
        return "avacapo_bvh_v1"
    canonical_bones = {canonical_bone_name(bone.name) for bone in obj.data.bones}
    if MIXAMO_REQUIRED_BONES.issubset(canonical_bones):
        return "mixamo"
    return "unknown"
