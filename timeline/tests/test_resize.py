"""Blender regressions for Clip trims and linked boundaries.

Run from the repository root with the add-on dependencies available:
blender --background --factory-startup --python-exit-code 1 \
    --python timeline/tests/test_resize.py
"""

import importlib
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

import bpy


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT.parent))
actions = importlib.import_module(f"{ROOT.name}.timeline.actions")
operations = importlib.import_module(f"{ROOT.name}.timeline.operations")
properties = importlib.import_module(f"{ROOT.name}.timeline.properties")
tracks = importlib.import_module(f"{ROOT.name}.core.tracks")


class ClipResizeTests(unittest.TestCase):
    def setUp(self):
        working, slot = self.new_action("Working")
        self.track = SimpleNamespace(
            object=None, working_action=working, working_slot_handle=slot.handle,
            clips=[], active_clip_uid=0,
        )
        self.working_slot = slot
        self.left = self.add_clip(1, 10, 20)
        self.right = self.add_clip(2, 20, 30)
        curve = actions.read_slot_curves(working, slot)[("location", 0)]
        for frame in (5, 35):
            self.insert_key(curve, frame, -frame)
        curve.update()
        self.original_working = self.key_states(working, slot)
        self.original_takes = [
            self.key_states(take.take_action, take.take_action.slots[0])
            for clip in self.track.clips for take in clip.takes
        ]

    def new_action(self, name):
        action = bpy.data.actions.new(name)
        self.addCleanup(bpy.data.actions.remove, action)
        return action, action.slots.new('OBJECT', name="Resize test")

    @staticmethod
    def insert_key(curve, frame, value):
        point = curve.keyframe_points.insert(frame, value, options={'FAST'})
        point.interpolation = 'LINEAR'
        point.handle_left_type = point.handle_right_type = 'FREE'
        point.handle_left = (frame - 0.25, value - 0.5)
        point.handle_right = (frame + 0.25, value + 0.5)

    def add_clip(self, uid, start, end):
        action, slot = self.new_action(f"Take {uid}")
        bag = actions.ensure_slot_channelbag(action, slot)
        # One shared channel and one channel unique to each Take.
        for axis in (0, uid):
            curve = bag.fcurves.new("location", index=axis)
            for frame in range(end - start):
                self.insert_key(curve, frame, 100 * uid + 10 * axis + frame)
            curve.update()
        take = SimpleNamespace(
            uid=1, offset=0, take_action=action, take_slot_handle=slot.handle,
        )
        clip = SimpleNamespace(
            uid=uid, start=start, end=end, bind_start=False, bind_end=False,
            takes=[take], active_take_uid=take.uid,
        )
        self.track.clips.append(clip)
        actions.materialize_take(
            self.track.working_action, self.working_slot,
            action, slot, start, end, take.offset,
        )
        return clip

    @staticmethod
    def key_states(action, slot):
        return {
            identity: [
                (tuple(key.co), tuple(key.handle_left), tuple(key.handle_right), key.type,
                 key.interpolation, key.handle_left_type, key.handle_right_type)
                for key in curve.keyframe_points
            ]
            for identity, curve in actions.read_slot_curves(action, slot).items()
        }

    def assert_restored(self):
        self.assertEqual(
            self.key_states(self.track.working_action, self.working_slot),
            self.original_working,
        )
        for clip in (self.left, self.right):
            take = clip.takes[0]
            self.assertEqual(take.offset, 0)
            self.assertEqual(
                self.key_states(take.take_action, take.take_action.slots[0]),
                self.original_takes[clip.uid - 1],
            )

    def resize(self, clip, operation, value):
        self.assertTrue(operations.resize_clip(self.track, clip.uid, operation, value))

    def drag(self, clip, kind, delta, other_uid=0):
        session = properties.edit_track(
            self.track, tracks.begin_drag, kind, clip.uid, other_uid,
        )
        self.assertTrue(properties.edit_track(self.track, tracks.update_drag, session, delta))
        self.assertTrue(operations.finalize_resize_drag(self.track, session))

    def test_unlinked_edges_restore_trimmed_frames(self):
        for operation, trimmed, original in (
            (tracks.resize_start, 13, 10), (tracks.resize_end, 17, 20),
        ):
            with self.subTest(edge=operation.__name__):
                self.resize(self.left, operation, trimmed)
                self.resize(self.left, operation, original)
                self.assert_restored()

    def test_right_edge_restores_frames_with_bound_following_clip(self):
        self.right.bind_start = True
        for operation, trimmed, original in (
            (tracks.resize_end, 17, 20), (tracks.set_duration, 7, 10),
        ):
            with self.subTest(operation=operation.__name__):
                self.resize(self.left, operation, trimmed)
                self.assertEqual(self.right.start, 17)
                self.assertEqual(self.right.takes[0].offset, 3)
                self.resize(self.left, operation, original)
                self.assert_restored()

    def test_left_edge_restores_frames_with_bound_previous_clip(self):
        self.left.bind_end = True
        self.resize(self.right, tracks.resize_start, 23)
        self.assertEqual(self.left.end, 23)
        self.resize(self.right, tracks.resize_start, 20)
        self.assert_restored()

    def test_right_drag_restores_frames_with_bound_following_clip(self):
        self.right.bind_start = True
        self.drag(self.left, 'RIGHT', -3)
        self.drag(self.left, 'RIGHT', 3)
        self.assert_restored()

    def test_shared_border_restores_frames_in_both_directions_and_record_orders(self):
        for reverse in (False, True):
            self.track.clips = [self.right, self.left] if reverse else [self.left, self.right]
            for delta in (-3, 3):
                with self.subTest(reverse=reverse, delta=delta):
                    self.drag(self.left, 'SYNC', delta, self.right.uid)
                    self.drag(self.left, 'SYNC', -delta, self.right.uid)
                    self.assert_restored()


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ClipResizeTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
