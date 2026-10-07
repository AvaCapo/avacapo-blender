"""Install packages with pip into Blender's."""

import importlib
import subprocess
import sys

import bpy


def install(package=None, *, requirements=None):
    """Install a package specifier or a requirements file (requires pip).

    Examples: install("scipy"), install(requirements="/path/requirements.txt").
    Runs synchronously and raises CalledProcessError if pip fails.
    """
    if bool(package) == bool(requirements):
        raise ValueError("Provide either package or requirements.")
    if not bpy.app.online_access:
        raise RuntimeError("Enable Allow Online Access in Blender Preferences.")

    target = bpy.utils.user_resource("SCRIPTS", path="modules", create=True)
    arguments = ["-r", str(requirements)] if requirements else [package]
    subprocess.run(
        [sys.executable, *bpy.app.python_args, "-m", "pip", "install",
         "--target", target, *arguments],
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    if target not in sys.path:
        sys.path.append(target)
    importlib.invalidate_caches()
