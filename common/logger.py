import logging

log = logging.getLogger("avacapo")
log.setLevel(logging.DEBUG)
log.propagate = False

_HANDLER_NAME = "avacapo.logger"

if not any(handler.get_name() == _HANDLER_NAME for handler in log.handlers):
    _handler = logging.StreamHandler()
    _handler.set_name(_HANDLER_NAME)
    log.addHandler(_handler)
