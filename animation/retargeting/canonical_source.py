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
    ("LeftUpLeg",   0, (0.069520, -0.091406, -0.006815)),         #1
    ("LeftLeg",     1, (0.034277, -0.375199, -0.004496)),         #2
    ("LeftFoot",    2, (-0.013596, -0.397960, -0.043693)),        #3
    ("LeftToe",     3, (0.026358, -0.055791, 0.119288)),          #4
    ("RightUpLeg",  0, (-0.067670, -0.090522, -0.004320)),        #5
    ("RightLeg",    5, (-0.038290, -0.382569, -0.008850)),        #6
    ("RightFoot",   6, (0.015774, -0.398415, -0.042312)),         #7
    ("RightToe",    7, (-0.025372, -0.048144, 0.123348)),         #8
    ("Spine",       0, (-0.002533, 0.108963, -0.026696)),         #9
    ("Spine1",      9, (0.005487, 0.135180, 0.001092)),           #10
    ("Spine2",      10, (0.001457, 0.052922, 0.025425)),          #11
    ("Neck",        11, (-0.002778, 0.213870, -0.042857)),        #12
    ("Head",        12, (0.005152, 0.064970, 0.051349)),          #13
    ("LeftShoulder",11, (0.078845, 0.121749, -0.034090)),         #14
    ("LeftArm",     14, (0.090977, 0.030469, -0.008868)),         #15
    ("LeftForeArm", 15, (0.259612, -0.012772, -0.027456)),        #16
    ("LeftHand",    16, (0.249234, 0.008986, -0.001171)),         #17
    ("RightShoulder",11, (-0.081759, 0.118833, -0.038615)),       #18
    ("RightArm",    18, (-0.096012, 0.032551, -0.009143)),        #19
    ("RightForeArm",19, (-0.253742, -0.013329, -0.021401)),       #20
    ("RightHand",   20, (-0.255298, 0.007772, -0.005559)),        #21
)

CANONICAL_SOURCE_TOPOLOGY = tuple(
    (name, parent) for name, parent, _offset in CANONICAL_SOURCE_JOINTS
)

CANONICAL_SOURCE_ROOT_NAME = "Hips"
SOURCE_ACTION_FPS_PROPERTY = "avacapo_source_fps"
DEFAULT_SOURCE_ACTION_FPS = 30.0 #TODO: check


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
