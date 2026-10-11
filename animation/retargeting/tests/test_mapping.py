"""Pure tests for the reusable explicit-mapping contract."""

import unittest

import numpy as np

from broom import Hierarchy, Joint, Motion
from broom.kinematics import compute_global_transforms_from_local

from animation.retargeting import blender_adapter, mapping, workflow


def _source_hierarchy():
    return Hierarchy((
        Joint("Hips", -1, (0.0, 0.0, 0.0), ("Xposition", "Yposition", "Zposition", "Xrotation", "Yrotation", "Zrotation")),
        Joint("Spine", 0, (0.0, 1.0, 0.0), ("Xrotation", "Yrotation", "Zrotation")),
        Joint("Head", 1, (0.0, 1.0, 0.0), ("Xrotation", "Yrotation", "Zrotation")),
    ))


def _target_hierarchy():
    return Hierarchy((
        Joint("Root", -1, (0.0, 0.0, 0.0), ("Xposition", "Yposition", "Zposition", "Xrotation", "Yrotation", "Zrotation")),
        Joint("Pelvis", 0, (0.0, 1.0, 0.0), ("Xrotation", "Yrotation", "Zrotation")),
        Joint("Twist", 1, (0.0, 0.5, 0.0), ("Xrotation", "Yrotation", "Zrotation")),
        Joint("Chest", 2, (0.0, 0.5, 0.0), ("Xrotation", "Yrotation", "Zrotation")),
        Joint("Skull", 3, (0.0, 1.0, 0.0), ("Xrotation", "Yrotation", "Zrotation")),
    ))


class ExplicitMappingTests(unittest.TestCase):
    def test_broom_suggestion_keeps_match_metadata(self):
        source = mapping.canonical_source_hierarchy()
        target = Hierarchy((
            Joint("Root", -1, (0.0, 0.0, 0.0), ("Xposition", "Yposition", "Zposition", "Xrotation", "Yrotation", "Zrotation")),
            Joint("mixamorig:Hips", 0, (0.0, 1.0, 0.0), ("Xrotation", "Yrotation", "Zrotation")),
            Joint("mixamorig:Spine", 1, (0.0, 1.0, 0.0), ("Xrotation", "Yrotation", "Zrotation")),
        ))
        proposal = mapping.suggest_mapping(source, target)
        self.assertEqual(proposal.mapping["Hips"], "mixamorig:Hips")
        hips_match = next(match for match in proposal.matches if match.source == "Hips")
        self.assertGreaterEqual(hips_match.score, 0.9)

    def test_allows_extra_target_joints_between_mapped_joints(self):
        mapping.validate_mapping(
            _source_hierarchy(),
            _target_hierarchy(),
            {"Hips": "Pelvis", "Spine": "Chest", "Head": "Skull"},
            required_source_names=("Hips",),
        )

    def test_rejects_missing_source_joint(self):
        with self.assertRaisesRegex(ValueError, "missing required source joints"):
            mapping.validate_mapping(
                _source_hierarchy(),
                _target_hierarchy(),
                {"Hips": "Pelvis", "Spine": "Chest"},
                required_source_names=("Hips", "Spine", "Head"),
            )

    def test_workflow_returns_target_motion(self):
        source = _source_hierarchy()
        motion = Motion(source, np.zeros((2, source.total_channels)), 1.0 / 30.0)
        result = workflow.retarget_mapped_motion(
            motion,
            _target_hierarchy(),
            {"Hips": "Pelvis", "Spine": "Chest", "Head": "Skull"},
            scale=1.0,
            required_source_names=("Hips", "Spine", "Head"),
        )
        self.assertEqual(result.hierarchy.joint_names, _target_hierarchy().joint_names)
        self.assertEqual(result.values.shape, (2, _target_hierarchy().total_channels))

    def test_rest_adapter_preserves_heads_and_rest_orientations(self):
        angle = np.deg2rad(90.0)
        rotation = np.asarray(((np.cos(angle), -np.sin(angle), 0.0), (np.sin(angle), np.cos(angle), 0.0), (0.0, 0.0, 1.0)))
        records = (
            {"name": "root", "parent": None, "head": (1.0, 2.0, 3.0), "matrix": np.eye(3)},
            {"name": "child", "parent": "root", "head": (2.0, 2.0, 3.0), "matrix": rotation},
        )
        hierarchy = blender_adapter.compile_rest_records(records, rotation)
        positions, rotations = compute_global_transforms_from_local(hierarchy)
        np.testing.assert_allclose(positions[0, 0], rotation @ records[0]["head"], atol=1e-8)
        np.testing.assert_allclose(positions[0, 1], rotation @ records[1]["head"], atol=1e-8)
        np.testing.assert_allclose(rotations[0, 0], rotation, atol=1e-8)
        np.testing.assert_allclose(rotations[0, 1], rotation @ records[1]["matrix"], atol=1e-8)

    def test_rest_adapter_projects_small_blender_matrix_drift(self):
        records = (
            {"name": "root", "parent": None, "head": (0.0, 0.0, 0.0), "matrix": ((1.000003, 0.0, 0.0), (0.0, 0.999998, 0.0), (0.0, 0.0, 1.0))},
        )
        hierarchy = blender_adapter.compile_rest_records(records, np.eye(3))
        _positions, rotations = compute_global_transforms_from_local(hierarchy)
        np.testing.assert_allclose(rotations[0, 0], np.eye(3), atol=1e-8)

    def test_rest_adapter_rejects_meaningful_scale(self):
        records = (
            {"name": "root", "parent": None, "head": (0.0, 0.0, 0.0), "matrix": ((1.1, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))},
        )
        with self.assertRaisesRegex(ValueError, "proper rotation"):
            blender_adapter.compile_rest_records(records, np.eye(3))


if __name__ == "__main__":
    unittest.main()
