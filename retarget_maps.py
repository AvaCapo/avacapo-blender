MIXAMO_REQUIRED_BONES = frozenset(
    {
        "hips",
        "spine",
        "head",
        "leftarm",
        "leftforearm",
        "lefthand",
        "rightarm",
        "rightforearm",
        "righthand",
        "leftupleg",
        "leftleg",
        "leftfoot",
        "rightupleg",
        "rightleg",
        "rightfoot",
    }
)


MIXAMO_RETARGET_BONES = [
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
    "LeftToeBase",
    "RightUpLeg",
    "RightLeg",
    "RightFoot",
    "RightToeBase",
]


MIXAMO_SCALE_REFERENCE_BONES = [
    "Spine",
    "Spine1",
    "Spine2",
    "Neck",
    "Head",
    "LeftUpLeg",
    "LeftLeg",
    "LeftFoot",
    "RightUpLeg",
    "RightLeg",
    "RightFoot",
    "LeftArm",
    "LeftForeArm",
    "LeftHand",
    "RightArm",
    "RightForeArm",
    "RightHand",
]


def canonical_bone_name(name: str) -> str:
    return name.split(":")[-1].strip().casefold()
