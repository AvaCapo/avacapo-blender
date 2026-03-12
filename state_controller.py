# this to-be-big-ass-file is central for all state controls
# its done a simple "static-class" (class with no instance)
# called simple "State"
# as its not bpy thing, but a python-level thing it can be accesed
# from any thread.

# here is all async things, qeues etc...


class State:
    server_busy: bool = False
    server_status: str = ""
    current_fps: int = 24


# Update handlers, which smartly update local state every click:
def update_handler(context): ...
