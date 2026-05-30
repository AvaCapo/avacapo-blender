
class GlobalQueue:
    max_concurrent: int = 5
    attempts_to_process: list[str]
    current_attempts = list[str]

    @classmethod
    def add_attempt(cls, attempt_id): ...

    @classmethod
    def update_cycle(cls):
        # here we do update statuses, etc

    @classmethod
    def launch(cls):
        # spawn separate thread, on wich we
        # handle the queue

# if its not possible in python threads
# use blender embeded timers
