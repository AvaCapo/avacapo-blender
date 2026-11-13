# AvaCapo Blender Add-on

AvaCapo is a Blender add-on that animates your character from a text description.
You write what the character should do, for example `a person walks forward and
waves`, and AvaCapo generates the motion and puts it on your character's timeline.

You can generate as many versions as you like and keep the one you prefer, chain
several motions into one longer performance, and guide the result with your own poses.
Mixamo characters and Rigify metarigs work out of the box, and other humanoid
characters need only a short one-time setup.

Generation runs on the AvaCapo online service, so you need an AvaCapo account and an
internet connection.

## Requirements

- Blender 5.1 or newer
- An AvaCapo account
- Internet access
- `Allow Online Access` enabled in `Edit -> Preferences -> System`. The add-on
  downloads a required component the first time it starts.

## Installation

1. Download this repository as a ZIP.
2. In Blender, open `Edit -> Preferences -> Get Extensions`.
3. Click `Install from Disk` and select the ZIP.
4. Enable `AvaCapo AI animation` if Blender does not enable it automatically.

## Getting Started

### Open the panel

The add-on lives in the 3D View sidebar: press `N` in the 3D View and open the
`AvaCapo` tab. Keep a Timeline open as well, because that is where your animation
will appear.

### Log in

Click `Login via Browser`. Your browser opens the AvaCapo website; log in there and
return to Blender. The panel now shows `Connected`.

If browser login does not suit you, copy an API token from your AvaCapo account,
paste it into the field below the button, and click `Connect`.

To log out, click the `X` next to `Connected`. Your login is stored on your computer,
in `storage.json` inside the add-on folder.

### Choose your character

Select your character's armature. Its name appears in the panel.

If you do not have a character yet, click `New Armature`. It adds the AvaCapo
character to the scene, ready to use.

### Connect AvaCapo to your character's bones

AvaCapo needs to know which bone of your character is the hips, which is the head,
the left hand, and so on. This is called bone mapping, and it lives in the
`Set up retargeting` section of the panel. The icon next to it shows whether your
character is ready:

- A check mark: the mapping is done. Mixamo characters and Rigify metarigs are
  mapped automatically, and so is the AvaCapo character.
- Any other icon: open `Set up retargeting` and click `Auto Mapping`. Each AvaCapo
  bone on the left now needs a bone of your character on the right. Fix any empty or
  wrong rows by hand. Rows in normal text are required, dimmed rows are optional.
  Then click `Confirm Mapping`. When the section shows `Mapping saved`, you are ready.

You only need to do this once per character.

## Your First Animation

### Create a clip

Your animation is built from clips. A clip is a stretch of time on the timeline,
shown as a bar, and it holds one motion.

To create one, set `Frames` to the length you want and click
`New from Current Frame`. You can also double-click an empty spot on the Timeline.

Clips work best between a quarter of a second and ten seconds long.

### Write a prompt and generate

The description of the motion is called the prompt. Type it into the field under
`Current Clip`, then click `Generate Take`.

After a short wait, the motion appears in the clip and plays on your character.

### Takes

Each generated result is called a take. Click `Generate Take` again and you get a new,
different take; the earlier ones are kept. All takes of the clip are listed under
`Takes`:

- click a take to show it on your character
- hover over a take to see the prompt and settings it was made with
- click the trash button to delete a take
- click the cancel button to stop a take that is still generating

You can also change the prompt and generate again. Takes made with the old prompt stay
in the list.

## Building a Longer Animation

### Add more clips

Add the next clip right after the first one and give it its own prompt, for example
`idle`, then `turn around`, then `walk away`. When two clips touch, a new take
continues smoothly from where the previous clip ends and leads into the start of the
next one.

### Arrange clips on the Timeline

Your clips are visible on the Timeline while your armature is selected.

- Click a clip to make it the current clip in the panel.
- Drag a clip to move it, or drag its edges to change its length.
- Double-click a clip to rename it.
- Double-click empty space to create a clip and type its prompt straight away.

Clips do not support native hotkeys like `G`, `S`, `A`, `X` or `Delete` (yet).
Clips support mouse clicks (selecting with left-click, dragging, resizing,
right-click for options).

Right-click empty space for:

- `Create Clip`: add an empty clip
- `Fill In`: fill the gap with empty clips
- `Shrink Neighbors to Current Frame`: make room by trimming the neighboring clip
  that the current frame is inside
- `Paste Clip`: paste a copied clip

Right-click a clip for:

- `Generate Take`: generate a new take with the clip's prompt
- `Change Prompt / Settings...`: edit the prompt and settings, then generate
- `Change Take`: switch to another take
- `Stick to Neighbors`: stretch the clip until it touches the previous clip, the
  next one, or both, keep it attached to them, and generate a take for the new length
- `Copy Clip`: copy the clip with its takes, to paste elsewhere

### Clip settings

The `Clip Settings` section in the panel shows the name, start, end and duration of
the current clip. The link buttons next to start and end turn the attachment to the
neighboring clip on or off: an attached clip follows its neighbor when that neighbor
is resized.

## Guiding the Motion with Your Own Keyframes

You can tell AvaCapo how the character should look at a certain moment. Pose the
character on any frame inside a clip, insert a keyframe, then click `Generate Take`.
The new take will pass through your pose.

Your keyframes are different from generated ones. Generated keyframes are marked with
the `Generated` keyframe type and are replaced automatically on every new take, so you
never need to delete them by hand. Your own keyframes are never overwritten: edit them
however you like, and they will guide future takes.

## Advanced Generation Settings

Open `Advanced Generation Settings` under the prompt to fine-tune the next take:

- `Model`: which AvaCapo model generates the motion
- `Seed`: a number that picks one variant of the motion (see below)
- `Temperature`: lower gives calmer, more predictable motion; higher gives more
  variety
- `Cycled`: ask for a looping motion
- `In Place`: keep the character on the spot instead of moving through the scene
- `Enhance Prompt`: let AvaCapo improve your prompt before generating

### Seed randomization

By default, the seed changes for every new take, which gives you a _different_
animation every time.

Click the clock button next to `Seed` to keep the seed the same. This is useful when
you like a particular motion variant and want to build on it: extend the clip, add new
keyframes in the middle, and still get the same motion variant on `Generate Take`. In
this mode, clicking an earlier take makes its seed the active one.

## Finishing Up

When a clip is the way you want it:

- `Bake Clip` keeps the animation you see as regular keyframes and removes the clip
  and its takes.
- `Delete All Takes` removes the takes but keeps the clip, so you can start over.
- `Delete Clip` removes the clip together with its animation, including keyframes you
  added inside it.

All three are in the clip's right-click menu.

*However*, these operations are not recommended. The AvaCapo add-on supports a fully
_non-destructive_ workflow: clips, takes and settings stay in the .blend file and can
be edited afterwards.

If you need to clean up the file from AvaCapo properties, the "clear properties"
button is available in the AvaCapo developer options in Properties -> Scene. It will
bake all the clips and clear up take data and all AvaCapo properties.
This is also useful if you update the add-on to a new major version in which data
schemas changed.

## Troubleshooting

### The panel says "Not Connected"

Click `Login via Browser`, or paste a valid API token and click `Connect`.

### The icon next to "Set up retargeting" shows an error

Your character's bones are not mapped yet. Open `Set up retargeting`, fix the rows
marked in red, and click `Confirm Mapping`. To try the add-on without your own
character, click `New Armature`.

Ensure the armature does not have additional bones, like a root bone (known bug) or
some other bones violating the basic character structure.

### Generation fails

Hover over the failed take to see the error message, then check:

- your internet connection
- your bone structure is correct and set up for retargeting

### The transition between clips does not look right

Try:

- making sure the clips touch, with no gap between them
- generating a new take of the later clip
- keyframing a pose at the start of the later clip to guide it

### Hands are behind the body, or other weird positioning

Check your armature rest pose: it should be a *T-pose*.

### Interface lags

- check your clip length: it should be under 10 seconds for generation; split your
  motion into smaller chunks
- you are trying to apply too many animations at once; in this case it is expected
  behavior

## Current Limitations

- Generation needs the online AvaCapo service and does not work offline
- Characters must be humanoid (hips, spine, head, arms and legs)
- Clips work best between 0.25 and 10 seconds long
- Root bones (known bug) or structural changes to the armature are not supported yet
- IK rigs are not supported yet.
