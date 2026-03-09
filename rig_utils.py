import bpy
from typing import Literal

RigType = Literal["avacapo_v1"]


def infer_rig_type(obj: bpy.types.Object) -> RigType:
    return "avacapo_v1"
