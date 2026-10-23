"""Temporary take generation utilities."""


TIMER_CALLBACKS = set()


def cancel_generation(callbacks=None) -> None:
    # TODO(test-generation): a real server adapter will own request callbacks
    # again. There are no local fake-generation timers anymore.
    (TIMER_CALLBACKS if callbacks is None else callbacks).clear()
