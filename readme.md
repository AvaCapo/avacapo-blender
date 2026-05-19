# AvaCapo Blender Add-on

AvaCapo Blender add-on for generating character animation clips from text
prompts and assembling them directly in one workflow.

The add-on connects Blender to the AvaCapo service, requests motion in BVH format,
applies it to a compatible armature, and keeps the process non-destructive by storing
generated takes as separate Blender actions.

## Highlights

- Generate motion from text prompts inside Blender
- Work with clips in the NLA editor instead of overwriting a single action
- Keep multiple attempts for the same clip and switch between them
- Chain newly generated clips after the previous one
- Continue root motion so later clips start where the previous clip ended
- Add optional frame overlap for smoother transitions between clips
- Authenticate from the UI with browser login or a pasted API token

## Requirements

- Blender 4.2 or newer
- An AvaCapo API token or browser login
- Internet access to the AvaCapo service

## Current Scope

The current add-on is focused on a simple, fast, artist-facing workflow:

- one prompt -> one generated motion clip
- one clip -> one NLA track / strip
- one attempt -> one Blender action


## Current Limitations

- Retargeting to Mixamo, Rigify, and arbitrary rigs is not implemented yet
- `Try to Convert` is currently a placeholder
- Generation depends on the online AvaCapo service and does not work offline
- The add-on is designed around short clip generation, not full-scene authoring

## Installation

This repository root is the add-on root. `__init__.py` and `blender_manifest.toml`
must stay at the top level.

### Install in Blender

1. Download this repository as a ZIP, or create a ZIP from this folder.
2. In Blender, open `Edit -> Preferences -> Get Extensions`.
3. Click `Install from Disk`.
4. Select the ZIP archive.
5. Enable `AvaCapo AI animation` if Blender does not enable it automatically.

## Quick Start

1. Open Blender and go to `3D View -> Sidebar -> Avacapo`.
2. Connect with `Login via Browser`, or paste an API token manually.
3. Select a compatible armature.
4. If you do not have one yet, click `New Armature` to append the bundled rig.
5. Set the clip timing with `Start`, `Seconds`, and `End`.
6. Enter a prompt, choose a model, and optionally set `Transition`.
7. Click `Generate`.
8. Generate additional clips to build a longer performance.

New clips are automatically placed after the latest existing clip. If `Transition` is
greater than `0`, the new clip overlaps the previous one by that many frames for a
smoother handoff.

## Workflow Concepts

### Clip

A clip is one animation segment in your timeline. In Blender terms, a clip is backed
by a single NLA track / strip.

Use clips when you want to build a longer animation from short generated chunks such as:

- idle -> turn -> walk
- step back -> recover -> point
- look around -> react -> run

### Attempt

An attempt is one generated variation for a clip. In Blender terms, an attempt is
stored as a single action.

This means you can:

- generate a different take for the same prompt
- keep older takes instead of destroying them
- switch the active take for a clip from the panel

## Panel Overview

The main panel lives in `3D View -> Sidebar -> Avacapo`.

### Connection

- `Login via Browser`: opens the AvaCapo web app and completes auth in the add-on
- `Connect`: saves a manually pasted API token
- `Disconnect`: clears the stored token

The token is stored locally in `storage.json` inside the add-on folder.

### Generation Controls

- `Start`: frame for the next generated clip
- `Seconds`: clip duration in seconds
- `End`: end frame, linked to `Start` and `Seconds`
- `Prompt`: text description of the motion
- `Model`: model catalog from the AvaCapo service, with local defaults as fallback
- `Transition`: overlap, in frames, between the previous clip and the next one

If `Start Record Lock` is enabled, the `Start` value follows the current timeline frame
when playback is stopped.

### Clip Controls

Each generated clip exposes:

- `start` / `end`: strip timing in the timeline
- `fade in` / `fade out`: NLA strip blend amounts
- `auto fade`: Blender automatic blend handling
- `new take`: generate another attempt for the same clip
- attempt buttons: switch between saved takes

## Supported Rig

The add-on currently expects the bundled AvaCapo armature layout:

- rig object name in the bundled library: `avacapo_bvh_v1`
- bundled library file: `rigs.blend`

If Blender shows `Unknown rig`, the selected armature does not match the currently
supported structure.


## Troubleshooting

### The panel says "Not Connected"

Use `Login via Browser` or paste a valid API token manually.

### The selected armature is shown as "Unknown rig"

Use `New Armature` to append the bundled rig, or retarget your character to the
currently supported structure first.

### Generation fails

Check:

- your API token
- your internet connection
- AvaCapo service availability
- whether the selected model is available for your account

### The next clip does not blend the way you want

Adjust:

- `Transition` before generating the next clip
- `fade in` / `fade out` on the NLA strip
- the strip timing in the clip UI
