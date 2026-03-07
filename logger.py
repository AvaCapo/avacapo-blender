import logging
log = logging.getLogger('blender_logger')
log.setLevel(logging.DEBUG)
log.addHandler(logging.StreamHandler())
log.debug('logger works')
