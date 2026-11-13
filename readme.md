# AvaCapo Blender Add-on

AvaCapo Blender add-on for generating character animation from text prompts and
assembling it directly on your Blender timeline.

Describe a motion, and AvaCapo animates your own character with it. Every result is
kept as a separate take, so you can try again, compare, and pick the one you like.

## Highlights

- Generate motion from text prompts inside Blender
- Lay out clips directly on the timeline, one prompt per clip
- Keep several takes for each clip and switch between them
- New clips continue from where the previous clip ends
- Guide the motion with your own keyframed poses
- Animate your own character: Mixamo and Rigify rigs are recognized automatically,
  other humanoid rigs can be set up by matching bones
- Authenticate from the UI with browser login or a pasted API token

## Requirements

- Blender 5.1 or newer
- An AvaCapo API token or browser login
- Internet access to the AvaCapo service
- `Allow Online Access` enabled in Blender Preferences (the add-on downloads a
  required component the first time it starts)

## Current Scope

The current add-on is focused on a simple, fast, artist-facing workflow:

- one clip -> one time range on the timeline
- one generation -> one take of that clip
- the selected take is the animation you see

## Current Limitations

- Generation depends on the online AvaCapo service and does not work offline
- Characters must be humanoid (hips, spine, head, arms and legs)
- The add-on is designed around short clips, roughly 0.25 to 10 seconds each

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

1. Open Blender and go to `3D View -> Sidebar -> AvaCapo`. Keep a Timeline open as well:
   that is where your clips appear.
2. Connect with `Login via Browser`, or paste an API token manually.
3. Select your character's armature. If you do not have one yet, click
   `New Armature` to add the AvaCapo character.
4. Check the icon next to `Set up retargeting`. A check mark means your character is
   ready. Otherwise, open it and match the bones (see [Character Setup](#character-setup)).
5. Set `Frames` and click `New from Current Frame` to create a clip, or double-click
   an empty spot on the timeline.
6. Type a prompt, for example `a person walks forward and waves`, and click
   `Generate Take`.
7. Wait a moment. The take appears in the clip and plays on your character.
8. Add more clips after the first one to build a longer performance.

## Workflow Concepts

### Clip

A clip is one segment of your animation, shown as a bar on the timeline. Each clip
has its own prompt.

Use clips when you want to build a longer animation from short generated chunks such as:

- idle -> turn -> walk
- step back -> recover -> point
- look around -> react -> run

When clips touch, each new take continues smoothly from the end of the previous
clip and leads into the start of the next one.

### Take

A take is one generated version of a clip. Every time you click `Generate Take`, a
new take is added; older takes are kept.

This means you can:

- generate a different version with the same prompt
- change the prompt and try again without losing earlier results
- switch between takes at any time to compare them

### Your own poses

You can guide the generation with your own keyframes. Pose the character on any
frame inside a clip, insert a keyframe, then click `Generate Take`. The new take
will pass through your pose.

Generated keyframes are marked with the `Generated` keyframe type, so you can tell
them apart from yours. Your own keyframes are never overwritten by a new take.

## Character Setup

AvaCapo needs to know which bone of your character is the hips, the head, the left
hand, and so on.

- Mixamo and Rigify characters are recognized automatically. Nothing to do.
- For other characters, open `Set up retargeting`, click `Auto Mapping`, and check
  the list. Each AvaCapo bone on the left needs a bone of your character on the right.
  Fix any empty or wrong rows by hand, then click `Confirm Mapping`.

Rows shown in normal text are required; the dimmed ones are optional. When the
status shows `Mapping saved`, your character is ready.

## Working on the Timeline

Select your armature to see its clips on the timeline.

- Double-click empty space: create a clip and enter a prompt for it
- Double-click a clip: rename it
- Click a clip: make it the current clip in the sidebar
- Drag a clip to move it, or drag its edges to change its length
- Right-click empty space: `Create Clip`, `Fill In` (fill the gap with empty clips),
  `Shrink Neighbors to Current Frame`, `Paste Clip`
- Right-click a clip: `Generate Take`, `Change Prompt / Settings...`, `Change Take`,
  `Stick to Neighbors`, `Copy Clip`, `Bake Clip`, `Delete All Takes`, `Delete Clip`

`Bake Clip` keeps the animation you see as regular keyframes and removes the clip
and its takes. `Delete Clip` removes the clip together with its animation, including
keyframes you added inside it.

## Panel Overview

The main panel lives in `3D View -> Sidebar -> AvaCapo`.

### Connection

- `Login via Browser`: opens the AvaCapo web app and completes login in the add-on
- `Connect`: saves a manually pasted API token
- `Disconnect` (the `X` next to `Connected`): logs out

The token is stored locally in `storage.json` inside the add-on folder.

### Character

- The name of the selected armature
- `New Armature`: adds the AvaCapo character when no armature is selected
- `Set up retargeting`: bone matching for your character (see
  [Character Setup](#character-setup))

### New Clip

- `Frames`: length of the new clip
- `New from Current Frame`: creates a clip at the current frame

### Current Clip

- Prompt: describe the motion you want
- `Generate Take`: creates a new take for the clip
- `Advanced Generation Settings`:
  - `Model`: which AvaCapo model generates the motion
  - `Seed`: the same seed and prompt give a similar result; the clock button picks a
    new seed every time
  - `Temperature`: lower is calmer and more predictable, higher is more varied
  - `Cycled`: ask for a looping motion
  - `In Place`: keep the character in place instead of moving through the scene
  - `Enhance Prompt`: let AvaCapo improve your prompt before generating
- `Takes`: click a take to show it, hover over it to see the prompt and settings
  it was made with. Use the trash button to delete it, or the cancel button to
  stop one that is still generating.
- `Clip Settings`: name, start, end and duration of the clip. The link buttons
  keep a clip attached to its neighbor when either one is resized.

### Seed Randomiztion:
By default seed is randomized every new generated take, which gives you _different_
animation every time, however, if you press on a clock button it stays the same:
its usable if you like particular motion variant and want to extend it:
expand clip, add new keyframes in the middle and still get the same motion variant
on "Generate new Take". clicking on previos takes in this mode sets their seed
as active.

### Your Keyframes Are Different from generated ones
you dont need to manualy delete generated keyframes, its done automatically on 
new take generation. Feel Free to operate your keyframes how you want.
Your keyframes are used to guide future generated animations.

## Troubleshooting

### The panel says "Not Connected"

Use `Login via Browser` or paste a valid API token manually.

### The icon next to "Set up retargeting" shows an error

Your character's bones are not matched yet. Open `Set up retargeting`, fix the rows
marked in red, and click `Confirm Mapping`. You can also click `New Armature` to try
the add-on with the AvaCapo character.

### Generation fails

Check:

- your API token
- your internet connection
- AvaCapo service availability
- whether the selected model is available for your account

Hover over the failed take to see the error message.

### The transition between clips does not look right

Try:

- making sure the clips touch, with no gap between them
- generating a new take of the later clip
- keyframing a pose at the start of the later clip to guide it
