# resolve_cutter.py

Turns a `*_clip_candidates.json` file (from `cf.py`) into one exported vertical
**1080x1920** MP4 per candidate, cut to the candidate's in/out points and
centre-cropped from the horizontal source, using DaVinci Resolve's scripting API.

Straight cut + static crop only. No captions, no beat-matched zooms — those stay
manual (or AutoSubs v2 / Caption Pro) afterwards.

## Setup

1. **Resolve must be running with a project already open.** The script refuses to
   create one — silently rendering into a fresh empty project is worse than stopping.
2. **Preferences → General → "External scripting using" → `Local`.**
   It ships as `None`, and the change needs a Resolve restart.
3. Environment (Windows):

   | Variable | Value |
   |---|---|
   | `RESOLVE_SCRIPT_API` | `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting` |
   | `RESOLVE_SCRIPT_LIB` | `C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll` |
   | `PYTHONPATH` | append `%RESOLVE_SCRIPT_API%\Modules\` |

   If these are unset the script fills in the platform defaults itself before
   importing, so it often works anyway — `--check` reports which path it actually
   loaded, so you can tell the difference between "configured" and "got lucky".

## Use it in this order

```
# 1. Preflight. Connects, introspects the installed version, prints the frame
#    maths. Creates no timelines and renders nothing.
python resolve_cutter.py vid1_clip_candidates.json vid1.mp4 --check

# 2. One clip, end to end. Open the file and watch it.
python resolve_cutter.py vid1_clip_candidates.json vid1.mp4 --only 0

# 3. Only once that clip is right:
python resolve_cutter.py vid1_clip_candidates.json vid1.mp4
```

The source video argument is optional — `vid1_clip_candidates.json` infers
`vid1.mp4` sitting next to it.

Output lands in `clips_out/` next to the source video. Timeline names and
filenames share one slug: `00_story_i-lost-everything-in-one-week`.

### Options worth knowing

| Flag | Why |
|---|---|
| `--check` | Preflight. Run it first, every time you touch a new source. |
| `--only N` / `--limit N` | One clip, or the first N. |
| `--no-render` | Build the timelines, skip the export — for eyeballing crops in the Edit page. |
| `--fit-mode {fit,fill,none}` | Matches Resolve's "Mismatched Resolution" setting. See below. |
| `--zoom N` | Override the computed zoom outright. |
| `--pan N` | Horizontal nudge in timeline px (+right) for off-centre subjects. |
| `--out-dir` | Somewhere other than `clips_out`. |
| `--quality KBPS` | H.264 bitrate, default 12000. |

## The three things that go wrong silently

**Frame rate.** Read off the `MediaPoolItem` (`FPS` property), never assumed.
Assuming 30 against 29.97 footage drifts ~1 frame every 33 seconds — invisible on
clip 1, badly wrong at the end of a long source. Each candidate is also computed
from its own absolute seconds, so error cannot accumulate down the batch.

Two related details the script handles and `--check` shows you:

- Resolve's `endFrame` in the `AppendToTimeline` clip-info dict is **inclusive**,
  so a 12.5s–45.0s candidate is 974 frames at 29.97, not 975.
- A clip's frames are numbered from its own `Start` property, which is not always
  0 — media carrying embedded timecode starts wherever its timecode starts, and
  every cut has to be offset by it.

`--check` prints per-candidate frames and in/out timecode. Scrub the source to one
of those timecodes and confirm before running the batch.

**The crop.** `ZoomX`/`ZoomY` are relative to whatever base scaling the project's
"Mismatched Resolution" setting already applied, so the right multiplier depends
on that setting — there is no single correct number:

| Project setting | `--fit-mode` | Zoom for 16:9 → 9:16 |
|---|---|---|
| Scale entire image to fit *(factory default)* | `fit` *(default)* | 3.1605 |
| Scale entire image to fill / centre crop with resizing | `fill` | 1.0 |
| Centre crop with no resizing | `none` | 1.7778 |

If clip 1 comes out over-zoomed or letterboxed, that's the fit mode — re-run with
a different `--fit-mode`, or `--zoom` to override.

The crop keeps ~32% of the source width. A centred speaker is fine; two people, or
anyone framed off to one side, will fall out of shot. Nudge those individually
with `--pan`, or per-candidate by adding `"pan": -120` to that entry in the JSON —
the per-candidate value wins. Face-tracked reframing is deliberately out of scope.

Property names differ between Resolve versions, so the script introspects the live
`TimelineItem` via `GetProperty()` rather than committing to one spelling, falls
back from `ZoomX`/`ZoomY` to `Zoom`, and **reads every value back after setting
it** — `SetProperty` returns `False` on an unknown key on some versions and
silently no-ops on others, and a crop that quietly didn't apply looks exactly like
a correct one until you open the render.

**Render queue timing.** `StartRendering()` does **not** block — it returns as soon
as the job reaches the render engine. Firing the next job off that return value is
how two jobs end up writing the same output file. So: one job queued at a time,
polled on `IsRenderingInProgress()` until it clears, job status checked, and the
output file confirmed on disk before moving on. Render jobs you already had queued
are left alone — only jobs this script added are ever started.

Format and codec are resolved by searching `GetRenderFormats()` /
`GetRenderCodecs()` for MP4 and H.264 rather than hardcoding an identifier, since
those vary by version and platform (`H264`, `H.264`, hardware variants).

## Tests

```
python test_resolve_cutter.py
```

76 tests. The frame maths, zoom maths, slug generation and JSON parsing are tested
directly; the whole build-and-render pass runs against a fake Resolve object graph
that checks the call *sequence* — in/out points reaching `AppendToTimeline`,
timeline resolution landing before the clip does, the render poll actually waiting.

**What the tests cannot tell you:** the fake answers the way Blackmagic's docs and
community scripts say Resolve answers. Only the live application can confirm that
the real API agrees — which is what `--check` and `--only 0` are for.
