# Changelog

## [0.2.1]

### Added
- **IK Rig Example:** Added a rigged character with IK constraints for Auto IK.

### Changed
- Automatically assign the selected armature in the Retargeting panel.
- Optimized Take operations: applying animations, time shifting, and dragging.

### Fixed
- Fixed incorrect handling of scene FPS during animation generation.
- Fixed animation keyframes becoming misaligned with Clips when dragging during downloads.

### Removed
- Removed the previous metarig example file.


## [0.2.0]

### Added
- **Clip-based animation workflow:** Create, edit, and organize generated animations directly on Blender's Timeline, with multiple Takes per Clip.
- **Custom armature retargeting:** Generate animations for custom character armatures using configurable joint mapping.
- **Pose constraints:** Use keyframes marked as `KEYFRAME` to guide animation generation.

### Removed
- NLA-based generation workflow.

### Requirements
- Blender 5.1 or newer.