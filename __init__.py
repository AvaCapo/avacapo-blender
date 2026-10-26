"""AvaCapo Blender add-on entry points. Importing the package has no UI side effects."""


def register():
    from . import installer

    installer.ensure_package("scipy")

    from . import ui

    ui.register()


def unregister():
    from . import ui

    ui.unregister()
