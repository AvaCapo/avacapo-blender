import bpy
from typing import Literal

from .logger import log
from .rig_utils import infer_rig_type

type AnimationType = Literal["bvh_avocapo_v1"]


class Animation:
    """ animation, animation data """
    data: str
    _type: AnimationType

    def __init__(self, data) -> None:
        self.data = data
        self._type = "bvh_avocapo_v1"


def apply_animation(obj: bpy.types.Object, animation_data: Animation):
    """ strategy, choose and apply """
    animation = Animation(animation_data)
    match infer_rig_type(obj), animation._type:
        case "avacapo_v1", "bvh_avocapo_v1":
            apply_bvh(obj, bvh)
        case _:
            log.error(
                f"cannot apply animation of type {animation._type} to {infer_rig_type}")


def apply_bvh(obj: bpy.types.Object, bvh):
    """ which is the final step - just apply animation """
    log.debug(
        f'''
        aplling animation:
        {obj.name}
        {len(bvh)}
        '''
    )
