import bpy

from .logger import log


def apply_bvh(obj: bpy.types.Object, bvh):
    log.debug(
        f'''
        aplling animation:
        {obj.name}
        {len(bvh)}
        '''
    )
