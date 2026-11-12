"""Portable rest skeleton for AvaCapo's canonical animation source.

This is the 22-joint skeleton expected by the generation service.  It is used
for mapping before a source clip exists and for reverse-retargeting user poses
into constraint payloads.  At normal retarget time, a returned
``Motion.hierarchy`` is still the authoritative source.
"""


# Ordered depth-first rest avacapo skeleton.
#TODO: update offsets after changes on the backend side
CANONICAL_SOURCE_JOINTS = (
    ("Hips",        -1, (0.000000, 0.000000, 0.000000)),          #0
    ("LeftUpLeg",   0, (0.052299179, -0.093935639, -0.027606763)), #1
    ("LeftLeg",     1, (0.058866613, -0.416441321, -0.006556974)), #2
    ("LeftFoot",    2, (-0.041737989, -0.437583506, -0.029511765)),#3
    ("LeftToe",     3, (0.049293540, -0.065279245, 0.126259089)),  #4
    ("RightUpLeg",  0, (-0.057192899, -0.106548190, -0.022217851)),#5
    ("RightLeg",    5, (-0.048074268, -0.397559673, -0.014061437)),#6
    ("RightFoot",   6, (0.014489345, -0.446852267, -0.018029511)), #7
    ("RightToe",    7, (-0.040575184, -0.065286517, 0.127075911)), #8
    ("Spine",       0, (-0.001495834, 0.112929940, -0.024981268)), #9
    ("Spine1",      9, (0.006900469, 0.145636231, -0.006858510)),  #10
    ("Spine2",      10, (-0.010334037, 0.056081813, 0.021115851)), #11
    ("Neck",        11, (-0.011025756, 0.171365142, -0.028827066)),#12
    ("Head",        12, (0.024654359, 0.175390735, 0.024463326)),  #13
    ("LeftShoulder",11, (0.047724526, 0.087643057, -0.008375450)), #14
    ("LeftArm",     14, (0.126284808, 0.057680372, -0.013885141)), #15
    ("LeftForeArm", 15, (0.272907287, -0.069853373, -0.039094493)),#16
    ("LeftHand",    16, (0.276173830, 0.021254137, -0.002478220)), #17
    ("RightShoulder",11, (-0.046636276, 0.086612143, -0.014864366)),#18
    ("RightArm",    18, (-0.109341696, 0.053674292, -0.009117880)),#19
    ("RightForeArm",19, (-0.292028785, -0.035440356, -0.024564851)),#20
    ("RightHand",   20, (-0.271878421, -0.004834589, -0.016445294)),#21
)

CANONICAL_SOURCE_TOPOLOGY = tuple(
    (name, parent) for name, parent, _offset in CANONICAL_SOURCE_JOINTS
)

CANONICAL_SOURCE_ROOT_NAME = "Hips"
SOURCE_ACTION_FPS_PROPERTY = "avacapo_source_fps"


def parent_name(parent_index: int) -> str | None:
    """Return the canonical parent name for one topology parent index."""
    return None if parent_index == -1 else CANONICAL_SOURCE_TOPOLOGY[parent_index][0]


def canonical_source_hierarchy():
    """Build the complete canonical source hierarchy."""
    import broom

    joints = []
    for name, parent, offset in CANONICAL_SOURCE_JOINTS:
        channels = (
            (
                "Xposition", "Yposition", "Zposition",
                "Xrotation", "Yrotation", "Zrotation",
            )
            if parent == -1
            else ("Xrotation", "Yrotation", "Zrotation")
        )
        joints.append(broom.Joint(
            name=name,
            parent=parent,
            offset=offset,
            channels=channels,
            local_orientation=(1.0, 0.0, 0.0, 0.0),
        ))
    return broom.Hierarchy(tuple(joints))
