"""Check dependencies and install missing packages into Blender's Python."""

import importlib
import subprocess
import sys

import bpy

from .common.logger import log


def ensure_package(package_name: str):
    """Install a package only when it is missing, before importing the add-on UI."""
    try:
        importlib.import_module(package_name)
    except ModuleNotFoundError as exc:
        if exc.name != package_name:
            raise
        install(package_name)
        importlib.import_module(package_name)
    log.info("Package %s is installed.", package_name)


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
