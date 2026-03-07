import bpy

from .logger import log


def apply_fvh(obj: bpy.types.Object, fvh):
    log.debug(
        f'''
        aplling animation:
        {obj.name}
        {len(fvh)}
        '''
    )
