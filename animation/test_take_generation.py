"""Temporary local replacement for the future server generation response."""

from pathlib import Path

from .retargeting import workflow


# TODO(test-generation): replace this bundled BVH adapter with the server result
# pipeline. The server result must still be written into a local-time Take Action.
_TEST_BVH_PATH = Path(__file__).resolve().parents[1] / "assets" / "tmp_animations" / "blender_backflip.bvh"


def load_test_motion():
    """Load the local fixture through Broom's established BVH policy."""
    source_motion = workflow.load_bvh_motion(_TEST_BVH_PATH)
    return source_motion, _TEST_BVH_PATH.read_text(encoding="utf-8")
