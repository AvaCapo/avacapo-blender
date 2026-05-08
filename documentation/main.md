Documentation:
--------------

# GENERAL:

Avacapo integration addon is here for creating your animation interactively with avacapo provided "AI" motion model, which does "prompt"->"animation"

its a simple interactive animation creation addon, which allows user to quickly create and combine short animation clips into complex motion.

So its like copilot for animator.

The basic UX statements:
- User is using a tool, so its human-first approach
- User have control over data, settings, can edit it before and after
- User is smart, curious, teachable and a good person overall
- Try-to-be "non-destructive" approach
- The tool provides *fast* to use interface for user
- Creative process is done by human, routines by tool
- Human shoud focus on results
- Tool Sould provide relaible-for-large-projects workflow
- Workflow should be understood intuitevely trough interface


# CLIP/ATTEMPT:

Animation you create consits of CLIPS
They compose together with fade-ins and fade-outs to create your final animation you can retry generation per every clip, this reties called ATTEMPTS

a clip/attempt is like shot/take in film:
    a single clip is a 2-20s animation of a person
    a single attempt is a variant for that animation
    so you choose between attempts for a single clip

its called "clip" and "attempt" to not confuse it verb or inner blender terminology:

a _clip_ is bound to single "NLA Track"
an _attemp_ is bound to single "Action"

So every (compatible) object have clips, on which you can switch attempts and generate new ones by requesting the server

# WORKFLOW:

1. You have a character
    -> you start to animate your character

2. You dont have a character

