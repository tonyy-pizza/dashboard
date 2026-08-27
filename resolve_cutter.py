#!/usr/bin/env python
"""
resolve_cutter.py -- cut + crop vertical clips in DaVinci Resolve.

Reads a ``*_clip_candidates.json`` file (as produced by ``cf.py``) and, for each
candidate, builds a 1080x1920 timeline containing a single trimmed instance of
the source clip, reframed so the horizontal source fills the vertical frame,
then renders it to ``clips_out/`` next to the source video.

Nothing here is mastering-grade: it is a straight cut plus a static centre crop,
meant for throwaway social clips that get polished by hand afterwards.

Quick start
-----------
    # 1. ALWAYS run this first. It connects, introspects the installed Resolve
    #    version and prints the frame-rate / crop / render facts it will use.
    python resolve_cutter.py vid1_clip_candidates.json vid1.mp4 --check

    # 2. Build + render exactly one clip and look at it before trusting a batch.
    python resolve_cutter.py vid1_clip_candidates.json vid1.mp4 --only 0

    # 3. Only once that clip is right:
    python resolve_cutter.py vid1_clip_candidates.json vid1.mp4

Prerequisites
-------------
* Resolve is running with a project already open (this script refuses to create
  one for you -- see ``--check``).
* Preferences -> General -> "External scripting using" -> ``Local``.
* Windows environment:
      RESOLVE_SCRIPT_API = %PROGRAMDATA%\\Blackmagic Design\\DaVinci Resolve\\Support\\Developer\\Scripting
      RESOLVE_SCRIPT_LIB = C:\\Program Files\\Blackmagic Design\\DaVinci Resolve\\fusionscript.dll
      PYTHONPATH        += %RESOLVE_SCRIPT_API%\\Modules\\
  If those are unset this script fills in the platform defaults itself before
  importing, so it usually works regardless -- ``--check`` reports which path
  it actually loaded.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

TARGET_W = 1080
TARGET_H = 1920

# Where DaVinciResolveScript.py lives when PYTHONPATH has not been set up.
_MODULE_DIRS = {
    "win32": [
        os.path.join(
            os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
            r"Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules",
        ),
    ],
    "darwin": [
        "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules",
    ],
    "linux": [
        "/opt/resolve/Developer/Scripting/Modules",
        "/home/resolve/Developer/Scripting/Modules",
    ],
}

# Where fusionscript lives; DaVinciResolveScript.py reads RESOLVE_SCRIPT_LIB at
# import time, so it has to be in os.environ *before* the import happens.
_SCRIPT_LIBS = {
    "win32": [r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"],
    "darwin": ["/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"],
    "linux": ["/opt/resolve/libs/Fusion/fusionscript.so", "/opt/resolve/Developer/Scripting/fusionscript.so"],
}


class ResolveError(RuntimeError):
    """Anything that means we cannot safely keep going."""


# --------------------------------------------------------------------------
# Pure helpers -- no Resolve required, and covered by test_resolve_cutter.py
# --------------------------------------------------------------------------

def slugify(text, max_len=40):
    """Filesystem- and Resolve-safe lowercase slug."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", " ", text).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    if len(text) > max_len:
        # Trim on a word boundary so the slug stays readable.
        text = text[:max_len].rsplit("-", 1)[0] or text[:max_len]
    return text.strip("-")


def make_stem(index, category, hook_line, hook_len=40):
    """``00_category_hook-slug`` -- used for both timeline name and filename."""
    parts = ["%02d" % index]
    cat = slugify(category, 24)
    if cat:
        parts.append(cat)
    hook = slugify(hook_line, hook_len)
    if hook:
        parts.append(hook)
    return "_".join(parts)


def parse_fps(value):
    """Turn a Resolve FPS property ('29.97', '24', 23.976) into a float."""
    if value is None:
        raise ResolveError("clip reported no frame rate")
    if isinstance(value, (int, float)):
        fps = float(value)
    else:
        m = re.search(r"\d+(?:\.\d+)?", str(value))
        if not m:
            raise ResolveError("could not parse frame rate from %r" % (value,))
        fps = float(m.group(0))
    if not 1.0 <= fps <= 1000.0:
        raise ResolveError("implausible frame rate %r" % (value,))
    return fps


def parse_resolution(value):
    """'1920x1080' -> (1920, 1080)."""
    m = re.search(r"(\d+)\s*[xX*]\s*(\d+)", str(value or ""))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def format_fps(fps):
    """Resolve wants a string; keep integers integral and NTSC rates at 3dp."""
    if abs(fps - round(fps)) < 1e-6:
        return str(int(round(fps)))
    return ("%.3f" % fps).rstrip("0").rstrip(".")


def frames_to_timecode(frame, fps):
    """Non-drop-frame timecode, for eyeballing cut points against the source."""
    rate = max(1, int(round(fps)))
    frame = max(0, int(frame))
    f = frame % rate
    total_s = frame // rate
    return "%02d:%02d:%02d:%02d" % (total_s // 3600, (total_s // 60) % 60, total_s % 60, f)


def candidate_frame_range(start_s, end_s, fps, clip_start_frame=0, clip_frame_count=None):
    """Seconds -> (first_frame, last_frame) in the source clip's own frame space.

    Two things here are load-bearing and are the reason this is a separate,
    tested function:

    * ``endFrame`` in Resolve's ``AppendToTimeline`` clip-info dict is
      **inclusive**, so a candidate covering [start, end) ends on the frame
      *before* ``end``. Getting this wrong adds one frame to every clip.
    * A clip's frames are numbered from its own ``Start`` property, which is not
      always 0 (media with embedded timecode starts wherever its TC starts), so
      the offset has to be added, not assumed away.
    """
    fps = float(fps)
    if fps <= 0:
        raise ResolveError("frame rate must be positive, got %r" % (fps,))
    if end_s is None or start_s is None:
        raise ResolveError("candidate is missing start or end")
    start_s = max(0.0, float(start_s))
    end_s = float(end_s)
    if end_s <= start_s:
        raise ResolveError("end (%.3fs) is not after start (%.3fs)" % (end_s, start_s))

    base = int(clip_start_frame or 0)
    first = base + int(round(start_s * fps))
    last = base + int(round(end_s * fps)) - 1  # inclusive
    if last < first:
        last = first  # sub-frame candidate: keep a single frame rather than fail

    if clip_frame_count:
        hard_last = base + int(clip_frame_count) - 1
        if first > hard_last:
            raise ResolveError(
                "start %.3fs is past the end of the source clip (%d frames)"
                % (start_s, int(clip_frame_count))
            )
        last = min(last, hard_last)
    return first, last


def compute_fill_zoom(src_w, src_h, tgt_w=TARGET_W, tgt_h=TARGET_H, fit_mode="fit"):
    """Zoom multiplier that makes the source *fill* the target frame.

    Resolve's ZoomX/ZoomY are relative to whatever base scaling the project's
    "Mismatched Resolution" setting already applied, so the multiplier depends
    on that mode -- which is exactly why ``--fit-mode`` exists and why --check
    prints what it found.

    fit     scale entire image to fit  (Resolve's factory default)
    fill    scale entire image to fill / centre crop with resizing
    none    centre crop with no resizing
    """
    src_w, src_h = float(src_w), float(src_h)
    if src_w <= 0 or src_h <= 0:
        raise ResolveError("bad source resolution %sx%s" % (src_w, src_h))
    fill = max(tgt_w / src_w, tgt_h / src_h)
    if fit_mode == "fit":
        base = min(tgt_w / src_w, tgt_h / src_h)
    elif fit_mode == "fill":
        base = fill
    elif fit_mode == "none":
        base = 1.0
    else:
        raise ResolveError("unknown fit mode %r" % (fit_mode,))
    return fill / base


def visible_source_fraction(src_w, src_h, tgt_w=TARGET_W, tgt_h=TARGET_H):
    """How much of the source width/height survives the fill crop, 0..1.

    Purely informational, but it is the number that tells you a 16:9 -> 9:16
    crop is throwing away ~68% of the frame width, which is what makes
    off-centre subjects fall out of shot.
    """
    src_w, src_h = float(src_w), float(src_h)
    scale = max(tgt_w / src_w, tgt_h / src_h)
    return (
        min(1.0, (tgt_w / scale) / src_w),
        min(1.0, (tgt_h / scale) / src_h),
    )


@dataclass
class Candidate:
    index: int
    start: float
    end: float
    category: str = ""
    hook_line: str = ""
    why: str = ""
    pan: float = None  # optional per-candidate horizontal nudge, timeline px
    raw: dict = field(default_factory=dict)

    @property
    def duration(self):
        return self.end - self.start

    def stem(self, hook_len=40):
        return make_stem(self.index, self.category, self.hook_line, hook_len)


def load_candidates(path):
    """Read + validate a ``*_clip_candidates.json`` file."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    # Tolerate both a bare list and a wrapper object with a list inside.
    if isinstance(data, dict):
        for key in ("candidates", "clips", "clip_candidates", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            raise ResolveError("%s is a JSON object with no candidate list in it" % path)
    if not isinstance(data, list):
        raise ResolveError("%s does not contain a list of candidates" % path)

    out = []
    for i, raw in enumerate(data):
        if not isinstance(raw, dict):
            raise ResolveError("candidate %d is %s, expected an object" % (i, type(raw).__name__))
        if "start" not in raw or "end" not in raw:
            raise ResolveError("candidate %d is missing 'start' and/or 'end'" % i)
        try:
            start = float(raw["start"])
            end = float(raw["end"])
        except (TypeError, ValueError):
            raise ResolveError("candidate %d has non-numeric start/end" % i)
        pan = raw.get("pan")
        out.append(
            Candidate(
                index=i,
                start=start,
                end=end,
                category=str(raw.get("category") or ""),
                hook_line=str(raw.get("hook_line") or ""),
                why=str(raw.get("why") or ""),
                pan=float(pan) if pan is not None else None,
                raw=raw,
            )
        )
    if not out:
        raise ResolveError("%s contains no candidates" % path)
    return out


def guess_source_video(json_path):
    """``vid1_clip_candidates.json`` -> ``vid1.mp4`` sitting next to it."""
    directory = os.path.dirname(os.path.abspath(json_path))
    stem = os.path.basename(json_path)
    for suffix in ("_clip_candidates.json", "_clip_candidates.JSON", ".json"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    for ext in (".mp4", ".mov", ".mkv", ".mxf", ".avi", ".m4v", ".webm"):
        for cand in (stem + ext, stem + ext.upper()):
            full = os.path.join(directory, cand)
            if os.path.isfile(full):
                return full
    return None


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------

def _seed_env():
    """Fill in RESOLVE_SCRIPT_API / RESOLVE_SCRIPT_LIB if the user has not."""
    plat = "win32" if sys.platform.startswith("win") else ("darwin" if sys.platform == "darwin" else "linux")
    notes = []
    if not os.environ.get("RESOLVE_SCRIPT_LIB"):
        for lib in _SCRIPT_LIBS.get(plat, []):
            if os.path.isfile(lib):
                os.environ["RESOLVE_SCRIPT_LIB"] = lib
                notes.append("defaulted RESOLVE_SCRIPT_LIB -> %s" % lib)
                break
    module_dirs = []
    api = os.environ.get("RESOLVE_SCRIPT_API")
    if api:
        module_dirs.append(os.path.join(api, "Modules"))
    module_dirs.extend(_MODULE_DIRS.get(plat, []))
    return module_dirs, notes


def _has_scriptapp(module):
    return callable(getattr(module, "scriptapp", None))


def _exec_resolve_module(path):
    """Run Blackmagic's DaVinciResolveScript.py and return what it *installs*.

    The shipped file is a loader that ends with::

        sys.modules[__name__] = script_module

    -- it replaces itself in ``sys.modules`` with the native ``fusionscript``
    module and never defines ``scriptapp`` in its own namespace. ``scriptapp``
    lives on the native module.

    ``import DaVinciResolveScript as dvr`` handles that for free, because the
    import statement rebinds from ``sys.modules`` after execution. Loading the
    file by path does not: ``exec_module`` leaves our local reference pointing
    at the discarded shell, whose namespace is just ``os``/``sys``/
    ``script_module``. Reading ``sys.modules`` back afterwards is what makes the
    two paths equivalent.

    Returns ``(installed, shell)``; ``installed is shell`` means no swap happened.
    """
    import importlib.util

    name = "DaVinciResolveScript"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ResolveError("could not build an import spec for %s" % path)
    shell = importlib.util.module_from_spec(spec)

    # Register before executing: the file assigns to sys.modules[__name__], and
    # this is also what lets a module import itself mid-execution.
    previous = sys.modules.get(name)
    sys.modules[name] = shell
    try:
        spec.loader.exec_module(shell)
    except Exception as exc:
        if previous is not None:
            sys.modules[name] = previous
        else:
            sys.modules.pop(name, None)
        hint = ""
        if isinstance(exc, ImportError) and "imp" in str(exc) and sys.version_info >= (3, 12):
            hint = ("\nThe shipped loader uses the `imp` module, removed in Python 3.12. "
                    "Run this under Python 3.11 or older, or use a Resolve version whose "
                    "scripting module was updated to importlib.")
        raise ResolveError(
            "found %s but it failed to load: %s: %s\n"
            "This is usually RESOLVE_SCRIPT_LIB pointing at the wrong fusionscript "
            "library (or none at all).%s" % (path, type(exc).__name__, exc, hint)
        )
    return sys.modules.get(name, shell), shell


def _load_fusionscript_direct():
    """Last resort: load the native fusionscript library ourselves.

    This is exactly what Blackmagic's loader does internally, minus the `imp`
    module it uses to do it (removed in Python 3.12). Useful when the shipped
    .py is missing or unrunnable but the library itself is fine.
    """
    import importlib.machinery
    import importlib.util

    plat = "win32" if sys.platform.startswith("win") else ("darwin" if sys.platform == "darwin" else "linux")
    candidates = []
    if os.environ.get("RESOLVE_SCRIPT_LIB"):
        candidates.append(os.environ["RESOLVE_SCRIPT_LIB"])
    candidates.extend(_SCRIPT_LIBS.get(plat, []))

    for lib in candidates:
        if not lib or not os.path.isfile(lib):
            continue
        try:
            loader = importlib.machinery.ExtensionFileLoader("fusionscript", lib)
            spec = importlib.util.spec_from_loader("fusionscript", loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
        except Exception:
            continue
        if _has_scriptapp(module):
            return module, lib
    return None, None


def _diagnose_missing_scriptapp(path, installed, shell):
    """Explain a module that loaded cleanly but has no scriptapp."""
    names = sorted(n for n in vars(installed) if not n.startswith("__"))
    try:
        size = os.path.getsize(path)
    except OSError:
        size = -1
    lib = os.environ.get("RESOLVE_SCRIPT_LIB") or "(unset)"
    lib_state = "exists" if (lib != "(unset)" and os.path.isfile(lib)) else "MISSING"
    return (
        "%s executed without error but exposes no scriptapp().\n"
        "  file size          : %d bytes\n"
        "  sys.modules swap   : %s\n"
        "  names defined      : %s\n"
        "  RESOLVE_SCRIPT_LIB : %s (%s)\n"
        "  python             : %s (%d-bit)\n"
        "If the swap did not happen, the file's internal fusionscript load failed "
        "silently -- which points at RESOLVE_SCRIPT_LIB above, or a Resolve "
        "install whose Developer/Scripting folder is stale."
        % (path, size,
           "no -- module did not replace itself" if installed is shell else "yes",
           ", ".join(names) or "(none)",
           lib, lib_state,
           sys.version.split()[0], 64 if sys.maxsize > 2 ** 32 else 32)
    )


def load_resolve_module():
    """Import DaVinciResolveScript, falling back to the on-disk module path.

    Returns ``(module, notes)`` where notes describe what had to be inferred --
    ``--check`` prints them so a mis-set environment is visible rather than
    silently papered over.

    Whatever is returned is guaranteed to expose ``scriptapp``; a module that
    loads but cannot provide it fails here, loudly, rather than at the call site.
    """
    module_dirs, notes = _seed_env()
    problems = []

    # Bind the error to a name that outlives the except block -- Python 3
    # unbinds `as exc` on the way out, and this message is the first thing a
    # user with an unconfigured environment ever sees.
    first_err = None
    try:
        import DaVinciResolveScript as dvr  # noqa: F401  (PYTHONPATH already correct)

        if _has_scriptapp(dvr):
            notes.append("imported DaVinciResolveScript from PYTHONPATH: %s"
                         % getattr(dvr, "__file__", "?"))
            return dvr, notes
        problems.append("the DaVinciResolveScript on PYTHONPATH (%s) has no scriptapp()"
                        % getattr(dvr, "__file__", "?"))
    except ImportError as exc:
        first_err = exc

    tried = []
    for directory in module_dirs:
        path = os.path.join(directory, "DaVinciResolveScript.py")
        tried.append(path)
        if not os.path.isfile(path):
            continue
        installed, shell = _exec_resolve_module(path)
        if _has_scriptapp(installed):
            notes.append("loaded %s" % path)
            if installed is not shell:
                notes.append("  (it swapped itself for the native %s module, as it should)"
                             % getattr(installed, "__name__", "fusionscript"))
            return installed, notes
        problems.append(_diagnose_missing_scriptapp(path, installed, shell))

    # Nothing usable via the shipped loader -- try the native library directly.
    direct, lib = _load_fusionscript_direct()
    if direct is not None:
        notes.append("loaded the native fusionscript library directly: %s" % lib)
        notes.append("  (the shipped DaVinciResolveScript.py was missing or unusable)")
        return direct, notes

    if problems:
        raise ResolveError(
            "DaVinciResolveScript was found but is not usable.\n\n%s\n\n"
            "Loading the native library directly did not work either. Repair or "
            "reinstall DaVinci Resolve's Developer/Scripting component."
            % "\n\n".join(problems)
        )
    raise ResolveError(
        "could not import DaVinciResolveScript (%s).\n"
        "Looked in:\n  %s\n"
        "Set RESOLVE_SCRIPT_API / RESOLVE_SCRIPT_LIB and add "
        "%%RESOLVE_SCRIPT_API%%\\Modules to PYTHONPATH." % (first_err, "\n  ".join(tried) or "(nowhere)")
    )


def connect():
    """Return ``(resolve, notes)`` for the *running* Resolve instance."""
    dvr, notes = load_resolve_module()
    resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        raise ResolveError(
            "DaVinciResolveScript loaded, but scriptapp('Resolve') returned None.\n"
            "That means the module is fine and the connection is not. Check:\n"
            "  1. DaVinci Resolve is actually running (not just installed).\n"
            "  2. Preferences -> General -> 'External scripting using' is set to Local\n"
            "     (it defaults to None, and the change needs a Resolve restart).\n"
            "  3. This Python is the same architecture as Resolve (64-bit).\n"
            "Everything downstream depends on this, so nothing else has been touched."
        )
    return resolve, notes


def get_current_project(resolve):
    """Fetch the open project, or fail loudly. Never creates one."""
    pm = resolve.GetProjectManager()
    if pm is None:
        raise ResolveError("Resolve connected but GetProjectManager() returned None.")
    project = pm.GetCurrentProject()
    if project is None:
        raise ResolveError(
            "No project is open in Resolve.\n"
            "Open (or create) the project you want these timelines to live in, "
            "then re-run. This script will not create a project for you -- "
            "silently rendering into a brand new empty project is worse than stopping."
        )
    return pm, project


# --------------------------------------------------------------------------
# Media pool
# --------------------------------------------------------------------------

def _clip_path(item):
    for key in ("File Path", "File Name"):
        try:
            val = item.GetClipProperty(key)
        except Exception:
            val = None
        if val:
            return str(val)
    return ""


def iter_pool_items(folder, _depth=0):
    """Depth-first walk of a media pool folder tree."""
    if folder is None or _depth > 12:
        return
    for item in folder.GetClipList() or []:
        yield item
    for sub in folder.GetSubFolderList() or []:
        for item in iter_pool_items(sub, _depth + 1):
            yield item


def find_or_import_media(media_pool, video_path, log=print):
    """Return the MediaPoolItem for ``video_path``, importing only if absent."""
    video_path = os.path.abspath(video_path)
    if not os.path.isfile(video_path):
        raise ResolveError("source video not found: %s" % video_path)
    target_name = os.path.basename(video_path)
    target_real = os.path.normcase(os.path.realpath(video_path))

    root = media_pool.GetRootFolder()
    by_name = None
    for item in iter_pool_items(root):
        existing = _clip_path(item)
        if existing and os.path.normcase(os.path.realpath(existing)) == target_real:
            log("  media pool: reusing existing clip (same file path)")
            return item
        try:
            name = item.GetName()
        except Exception:
            name = None
        if name and os.path.basename(str(name)) == target_name and by_name is None:
            by_name = item
    if by_name is not None:
        log("  media pool: reusing existing clip matched by filename %r" % target_name)
        return by_name

    log("  media pool: importing %s" % video_path)
    imported = media_pool.ImportMedia([video_path])
    if not imported:
        raise ResolveError(
            "ImportMedia failed for %s -- Resolve accepted the call but returned "
            "nothing. Usually an unsupported/unreadable file or a path Resolve "
            "cannot see." % video_path
        )
    return imported[0]


@dataclass
class ClipFacts:
    fps: float
    width: int
    height: int
    start_frame: int
    frame_count: int
    path: str
    raw: dict = field(default_factory=dict)


def read_clip_facts(item):
    """Read frame rate / resolution / frame span off the live MediaPoolItem.

    The frame rate is read, never assumed: a hardcoded 24/30/60 against 29.97
    footage drifts ~0.1 % per clip, which is a frame every 33 s -- invisible on
    clip 1 and badly wrong by the end of a long source.
    """
    props = {}
    try:
        props = item.GetClipProperty() or {}
    except Exception:
        props = {}
    if not isinstance(props, dict):
        props = {}

    def prop(*keys):
        for key in keys:
            if key in props and props[key] not in (None, ""):
                return props[key]
            try:
                val = item.GetClipProperty(key)
            except Exception:
                val = None
            if val not in (None, ""):
                return val
        return None

    fps = parse_fps(prop("FPS", "Frame Rate", "Video Frame Rate"))

    res = parse_resolution(prop("Resolution", "Video Resolution"))
    if not res:
        raise ResolveError(
            "could not read the source resolution from the media pool item "
            "(properties seen: %s)" % ", ".join(sorted(props)[:20])
        )
    width, height = res

    def as_int(val, default=0):
        try:
            return int(round(float(str(val).strip())))
        except (TypeError, ValueError):
            return default

    start_frame = as_int(prop("Start"), 0)
    frame_count = as_int(prop("Frames"), 0)
    if not frame_count:
        end_frame = as_int(prop("End"), 0)
        if end_frame:
            frame_count = end_frame - start_frame + 1

    return ClipFacts(
        fps=fps,
        width=width,
        height=height,
        start_frame=start_frame,
        frame_count=frame_count,
        path=_clip_path(item),
        raw=props,
    )


# --------------------------------------------------------------------------
# Timeline building
# --------------------------------------------------------------------------

_TIMELINE_SETTINGS = (
    ("useCustomSettings", "1"),
    ("timelineResolutionWidth", str(TARGET_W)),
    ("timelineResolutionHeight", str(TARGET_H)),
    ("timelineOutputResolutionWidth", str(TARGET_W)),
    ("timelineOutputResolutionHeight", str(TARGET_H)),
    ("timelinePixelAspectRatio", "1"),
)


def create_vertical_timeline(media_pool, project, name, fps, log=print):
    """Create an empty 1080x1920 timeline at the source frame rate.

    Order matters: Resolve only allows ``timelineFrameRate`` to change while the
    timeline is still empty, so every setting is applied before anything is
    appended, and then read back to confirm it stuck.
    """
    timeline = media_pool.CreateEmptyTimeline(name)
    if timeline is None:
        raise ResolveError(
            "CreateEmptyTimeline(%r) returned None -- a timeline with that name "
            "probably already exists in this project." % name
        )

    warnings = []
    for key, value in _TIMELINE_SETTINGS:
        try:
            timeline.SetSetting(key, value)
        except Exception as exc:
            warnings.append("SetSetting(%s) raised %s" % (key, exc))
    try:
        timeline.SetSetting("timelineFrameRate", format_fps(fps))
    except Exception as exc:
        warnings.append("SetSetting(timelineFrameRate) raised %s" % exc)

    # Verify rather than trust: a silently-ignored resolution means every clip
    # renders horizontal and the whole batch is wasted.
    actual_w = _get_setting(timeline, "timelineResolutionWidth")
    actual_h = _get_setting(timeline, "timelineResolutionHeight")
    if (actual_w, actual_h) != (str(TARGET_W), str(TARGET_H)):
        warnings.append(
            "timeline resolution reads back as %sx%s, expected %dx%d"
            % (actual_w, actual_h, TARGET_W, TARGET_H)
        )
    for msg in warnings:
        log("  ! %s" % msg)
    return timeline, warnings


def _get_setting(obj, key):
    try:
        val = obj.GetSetting(key)
    except Exception:
        return None
    return str(val) if val is not None else None


def append_candidate(media_pool, project, timeline, item, first_frame, last_frame):
    """Append the source clip with in/out already set, via the clip-info dict.

    Passing startFrame/endFrame here is deliberate: appending the whole clip and
    trimming afterwards means every timeline briefly contains the full source,
    and the trim path has its own rounding.
    """
    if not project.SetCurrentTimeline(timeline):
        raise ResolveError("could not make %r the current timeline" % timeline.GetName())
    clip_info = {
        "mediaPoolItem": item,
        "startFrame": int(first_frame),
        "endFrame": int(last_frame),
    }
    appended = media_pool.AppendToTimeline([clip_info])
    if not appended:
        raise ResolveError(
            "AppendToTimeline returned nothing for frames %d-%d. Common causes: "
            "the range falls outside the clip, or the timeline frame rate does "
            "not match the clip." % (first_frame, last_frame)
        )
    return appended[0]


# Property names differ between Resolve versions, so we introspect the live
# object instead of committing to one spelling. First match wins.
_ZOOM_KEYS = (("ZoomX", "ZoomY"), ("Zoom", None))
_PAN_KEY = "Pan"
_TILT_KEY = "Tilt"


def describe_item_properties(item):
    """Whatever ``GetProperty()`` reports for this Resolve version."""
    try:
        props = item.GetProperty()
    except Exception:
        return {}
    return props if isinstance(props, dict) else {}


def apply_fill_crop(item, zoom, pan=0.0, tilt=0.0, log=print):
    """Zoom the clip so it fills the vertical frame, centred (plus optional pan).

    Every SetProperty is checked *and* read back -- the API returns False on an
    unknown key on some versions and silently no-ops on others, and a crop that
    quietly did not apply looks exactly like a correct one until you open the
    render.
    """
    available = describe_item_properties(item)
    applied = {}
    problems = []

    def put(key, value):
        if available and key not in available:
            problems.append("%s is not a property on this Resolve version" % key)
            return False
        try:
            ok = item.SetProperty(key, value)
        except Exception as exc:
            problems.append("SetProperty(%s, %s) raised %s" % (key, value, exc))
            return False
        if ok is False:
            problems.append("SetProperty(%s, %s) returned False" % (key, value))
            return False
        try:
            back = item.GetProperty(key)
        except Exception:
            back = None
        if back is not None:
            try:
                if abs(float(back) - float(value)) > 1e-3:
                    problems.append("%s read back as %s, not %s" % (key, back, value))
                    return False
            except (TypeError, ValueError):
                pass
        applied[key] = value
        return True

    zoom_done = False
    for x_key, y_key in _ZOOM_KEYS:
        if available and x_key not in available:
            continue
        ok = put(x_key, float(zoom))
        if ok and y_key:
            ok = put(y_key, float(zoom))
        if ok:
            zoom_done = True
            break
    if not zoom_done:
        problems.append(
            "no usable zoom property found (looked for %s); available: %s"
            % (", ".join(k for pair in _ZOOM_KEYS for k in pair if k), ", ".join(sorted(available)[:25]) or "unknown")
        )

    # Centred by default; Pan/Tilt of 0 is frame centre in Resolve's transform.
    put(_PAN_KEY, float(pan or 0.0))
    put(_TILT_KEY, float(tilt or 0.0))

    for msg in problems:
        log("  ! %s" % msg)
    return applied, problems


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def pick_h264_mp4(project, log=print):
    """Find this version's name for 'MP4 container, H.264 codec'.

    Introspected, because the identifiers are not stable: 'H264', 'H.264',
    hardware-specific variants, and different container keys all show up
    depending on version and platform.
    """
    try:
        formats = project.GetRenderFormats() or {}
    except Exception:
        formats = {}
    fmt_key = None
    for name, ext in (formats.items() if isinstance(formats, dict) else []):
        if str(ext).lower().lstrip(".") == "mp4":
            fmt_key = str(ext).lower().lstrip(".")
            break
        if "mp4" in str(name).lower():
            fmt_key = str(ext).lower().lstrip(".") or "mp4"
            break
    if not fmt_key:
        fmt_key = "mp4"
        log("  ! no MP4 entry in GetRenderFormats(); trying 'mp4' anyway")

    try:
        codecs = project.GetRenderCodecs(fmt_key) or {}
    except Exception:
        codecs = {}
    codec_key = None
    if isinstance(codecs, dict):
        names = list(codecs.values()) + list(codecs.keys())
        # Prefer a plain software H.264 over a hardware-specific variant.
        for want in ("h264", "h.264"):
            exact = [c for c in names if str(c).lower().replace(".", "") == want.replace(".", "")]
            if exact:
                codec_key = str(exact[0])
                break
        if not codec_key:
            loose = [c for c in names if "264" in str(c).lower()]
            if loose:
                loose.sort(key=lambda c: len(str(c)))
                codec_key = str(loose[0])
    if not codec_key:
        codec_key = "H264"
        log("  ! no H.264 entry in GetRenderCodecs(%r); trying 'H264' anyway" % fmt_key)
    return fmt_key, codec_key


def configure_render(project, out_dir, stem, fps, fmt, codec, quality_kbps, log=print, warned=None):
    """Point the render at one file. Core settings first, niceties individually."""
    if not project.SetCurrentRenderFormatAndCodec(fmt, codec):
        raise ResolveError(
            "SetCurrentRenderFormatAndCodec(%r, %r) failed -- this Resolve build "
            "does not accept that format/codec pair." % (fmt, codec)
        )
    core = {
        "TargetDir": out_dir,
        "CustomName": stem,
        "SelectAllFrames": True,
        "ExportVideo": True,
        "ExportAudio": True,
        "FormatWidth": TARGET_W,
        "FormatHeight": TARGET_H,
        "FrameRate": format_fps(fps),
    }
    if not project.SetRenderSettings(core):
        raise ResolveError("SetRenderSettings failed for %s" % stem)

    # Optional, one at a time: a single unsupported key must not sink the job.
    # Each unsupported key is reported once per run, not once per clip.
    warned = warned if warned is not None else set()
    for key, value in (
        ("UniqueFilenameStyle", 1),  # suffix, not prefix, if Resolve deduplicates
        ("VideoQuality", int(quality_kbps)),
        ("AudioCodec", "aac"),
        ("AudioBitDepth", 16),
    ):
        try:
            ok = project.SetRenderSettings({key: value})
            note = None if ok else "not accepted"
        except Exception as exc:
            note = "raised %s" % exc
        if note and key not in warned:
            warned.add(key)
            log("  ! render setting %s=%r %s (continuing without it)" % (key, value, note))


def render_timeline(project, timeline, out_dir, stem, fps, fmt, codec, quality_kbps,
                    poll=2.0, timeout=3600.0, log=print, warned=None):
    """Queue exactly one job, render it, and wait for it to actually finish.

    ``StartRendering`` does **not** block -- it returns as soon as the job is
    handed to the render engine. Firing the next job off that return value is
    how two jobs end up writing the same output file. So: one job at a time,
    polled to completion, and only our own job id is ever started (any jobs the
    user already had queued are left alone).
    """
    if not project.SetCurrentTimeline(timeline):
        raise ResolveError("could not set current timeline before rendering %s" % stem)
    configure_render(project, out_dir, stem, fps, fmt, codec, quality_kbps, log=log, warned=warned)

    job_id = project.AddRenderJob()
    if not job_id:
        raise ResolveError("AddRenderJob() returned nothing for %s" % stem)

    started = project.StartRendering(job_id)
    if started is False:
        # Older signatures want a list plus the interactive flag.
        try:
            started = project.StartRendering([job_id], False)
        except Exception:
            started = False
    if started is False:
        raise ResolveError("StartRendering failed for job %s (%s)" % (job_id, stem))

    deadline = time.time() + timeout
    while True:
        try:
            busy = project.IsRenderingInProgress()
        except Exception:
            busy = False
        if not busy:
            break
        if time.time() > deadline:
            try:
                project.StopRendering()
            except Exception:
                pass
            raise ResolveError("render of %s exceeded %.0fs; stopped it" % (stem, timeout))
        time.sleep(poll)

    status = {}
    try:
        status = project.GetRenderJobStatus(job_id) or {}
    except Exception:
        pass
    state = str(status.get("JobStatus", "")) if isinstance(status, dict) else ""
    if state and state.lower() not in ("complete", "completed", "success"):
        raise ResolveError("render job for %s finished as %r" % (stem, state))

    # Trust the file on disk over the API's opinion of it.
    expected = os.path.join(out_dir, stem + "." + fmt.lstrip("."))
    if os.path.isfile(expected):
        return expected
    matches = sorted(
        os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.startswith(stem)
    ) if os.path.isdir(out_dir) else []
    if matches:
        return matches[0]
    raise ResolveError(
        "render reported %s but no output file starting with %r appeared in %s"
        % (state or "done", stem, out_dir)
    )


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

def preflight(args, candidates, log=print):
    """Connect, introspect, and print every fact the batch will rely on.

    This is the step the brief insists on doing before anything else: it touches
    the media pool (import is idempotent) but creates no timelines and renders
    nothing.
    """
    log("=" * 72)
    log("PREFLIGHT -- nothing will be created or rendered")
    log("=" * 72)

    resolve, notes = connect()
    for note in notes:
        log("  %s" % note)
    log("  connected: %s" % (resolve.GetProductName() or "Resolve"))
    log("  version:   %s" % ".".join(str(v) for v in (resolve.GetVersion() or [])))

    pm, project = get_current_project(resolve)
    log("  project:   %s" % project.GetName())
    log("  timelines already in project: %s" % project.GetTimelineCount())

    queued = 0
    try:
        queued = len(project.GetRenderJobList() or [])
    except Exception:
        pass
    if queued:
        log("  ! %d render job(s) already in this project's queue." % queued)
        log("    They are left untouched -- only jobs added by this script are started.")

    media_pool = project.GetMediaPool()
    item = find_or_import_media(media_pool, args.video, log=log)
    facts = read_clip_facts(item)

    log("")
    log("SOURCE CLIP")
    log("  file:        %s" % (facts.path or args.video))
    log("  resolution:  %dx%d  (%.4f:1)" % (facts.width, facts.height, facts.width / float(facts.height)))
    log("  frame rate:  %s fps   <- read from the clip, not assumed" % facts.fps)
    log("  start frame: %d%s" % (facts.start_frame,
                                 "" if facts.start_frame == 0 else "  <- non-zero, offsets every cut"))
    log("  frames:      %s (%.2fs)" % (facts.frame_count or "unknown",
                                       (facts.frame_count / facts.fps) if facts.frame_count else 0.0))

    zoom = args.zoom if args.zoom else compute_fill_zoom(facts.width, facts.height, fit_mode=args.fit_mode)
    frac_w, frac_h = visible_source_fraction(facts.width, facts.height)
    log("")
    log("REFRAME  (target %dx%d)" % (TARGET_W, TARGET_H))
    log("  fit mode:    %s%s" % (args.fit_mode, "" if args.zoom else "  (Resolve's 'Mismatched Resolution' setting)"))
    log("  zoom:        %.4f%s" % (zoom, "  (from --zoom)" if args.zoom else ""))
    log("  pan/tilt:    %.1f / %.1f px" % (args.pan, args.tilt))
    log("  keeps ~%.0f%% of source width, %.0f%% of height -- anything outside that is cropped away."
        % (frac_w * 100, frac_h * 100))
    log("  If the crop lands wrong, that is the fit mode: re-run with")
    log("    --fit-mode fill   (if the image is already filling the frame and this over-zooms)")
    log("    --fit-mode none   (if the image is at 1:1 pixels)")
    log("    --zoom N          (to override the maths entirely)")

    log("")
    log("RENDER")
    fmt, codec = pick_h264_mp4(project, log=log)
    log("  format/codec: %s / %s   <- resolved against this install, not hardcoded" % (fmt, codec))
    log("  output dir:   %s" % args.out_dir)

    log("")
    log("CUT POINTS  (verify a couple of these against the source before the batch)")
    log("  %-3s %-10s %-10s %-8s %-14s %-14s %s" %
        ("#", "start s", "end s", "frames", "in TC", "out TC", "name"))
    bad = 0
    for cand in candidates:
        try:
            first, last = candidate_frame_range(
                cand.start, cand.end, facts.fps, facts.start_frame, facts.frame_count
            )
        except ResolveError as exc:
            log("  %-3d SKIP: %s" % (cand.index, exc))
            bad += 1
            continue
        log("  %-3d %-10.3f %-10.3f %-8d %-14s %-14s %s" % (
            cand.index, cand.start, cand.end, last - first + 1,
            frames_to_timecode(first - facts.start_frame, facts.fps),
            frames_to_timecode(last - facts.start_frame + 1, facts.fps),
            cand.stem(args.hook_len),
        ))
    log("")
    log("  endFrame is inclusive in Resolve's API: the %.3fs-%.3fs candidate above"
        % (candidates[0].start, candidates[0].end))
    log("  is %d frames, not %d." % (
        candidate_frame_range(candidates[0].start, candidates[0].end, facts.fps)[1]
        - candidate_frame_range(candidates[0].start, candidates[0].end, facts.fps)[0] + 1,
        int(round(candidates[0].end * facts.fps)) - int(round(candidates[0].start * facts.fps)) + 1,
    ))
    log("")
    log("%d candidate(s) ready, %d unusable." % (len(candidates) - bad, bad))
    log("Next: run one clip end to end and look at it --  --only 0")
    return 0 if bad == 0 else 1


# --------------------------------------------------------------------------
# Main batch
# --------------------------------------------------------------------------

@dataclass
class Result:
    index: int
    stem: str
    ok: bool
    output: str = ""
    error: str = ""
    frames: tuple = None
    warnings: list = field(default_factory=list)


def run(args, candidates, log=print):
    resolve, notes = connect()
    for note in notes:
        log(note)
    pm, project = get_current_project(resolve)
    log("Project: %s" % project.GetName())

    media_pool = project.GetMediaPool()
    item = find_or_import_media(media_pool, args.video, log=log)
    facts = read_clip_facts(item)
    log("Source: %dx%d @ %s fps, start frame %d" % (facts.width, facts.height, facts.fps, facts.start_frame))

    zoom = args.zoom if args.zoom else compute_fill_zoom(facts.width, facts.height, fit_mode=args.fit_mode)
    log("Reframe: zoom %.4f (fit-mode %s), pan %.1f, tilt %.1f" % (zoom, args.fit_mode, args.pan, args.tilt))

    if not os.path.isdir(args.out_dir) and not args.no_render:
        os.makedirs(args.out_dir, exist_ok=True)
        log("Created output folder: %s" % args.out_dir)

    fmt = codec = None
    render_warned = set()
    if not args.no_render:
        fmt, codec = pick_h264_mp4(project, log=log)
        log("Render: %s / %s -> %s" % (fmt, codec, args.out_dir))

    original_page = None
    try:
        original_page = resolve.GetCurrentPage()
        resolve.OpenPage("edit")
    except Exception:
        pass

    results = []
    for cand in candidates:
        stem = cand.stem(args.hook_len)
        log("")
        log("[%d/%d] %s" % (cand.index + 1, len(candidates), stem))
        try:
            first, last = candidate_frame_range(
                cand.start, cand.end, facts.fps, facts.start_frame, facts.frame_count
            )
            log("  cut: %.3fs-%.3fs -> frames %d-%d (%d frames, %.2fs)" % (
                cand.start, cand.end, first, last, last - first + 1, (last - first + 1) / facts.fps))

            timeline_name = stem
            if args.timeline_prefix:
                timeline_name = args.timeline_prefix + stem

            timeline, warns = create_vertical_timeline(media_pool, project, timeline_name, facts.fps, log=log)
            titem = append_candidate(media_pool, project, timeline, item, first, last)
            pan = cand.pan if cand.pan is not None else args.pan
            if cand.pan is not None:
                log("  pan: %.1f px (from candidate JSON)" % pan)
            applied, problems = apply_fill_crop(titem, zoom, pan, args.tilt, log=log)
            warns = list(warns) + list(problems)
            log("  crop applied: %s" % (", ".join("%s=%.4f" % (k, v) for k, v in applied.items()) or "nothing"))

            output = ""
            if args.no_render:
                log("  --no-render: timeline built, render skipped")
            else:
                output = render_timeline(
                    project, timeline, args.out_dir, stem, facts.fps, fmt, codec,
                    args.quality, poll=args.poll, timeout=args.render_timeout, log=log,
                    warned=render_warned,
                )
                size_mb = os.path.getsize(output) / (1024.0 * 1024.0)
                log("  rendered: %s (%.1f MB)" % (output, size_mb))

            results.append(Result(cand.index, stem, True, output, frames=(first, last), warnings=warns))
        except ResolveError as exc:
            log("  FAILED: %s" % exc)
            results.append(Result(cand.index, stem, False, error=str(exc)))
        except Exception as exc:  # keep the batch alive; one bad clip is not all of them
            log("  FAILED (unexpected): %s: %s" % (type(exc).__name__, exc))
            results.append(Result(cand.index, stem, False, error="%s: %s" % (type(exc).__name__, exc)))

    if original_page:
        try:
            resolve.OpenPage(original_page)
        except Exception:
            pass

    return summarise(results, args, log=log)


def summarise(results, args, log=print):
    ok = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    warned = [r for r in ok if r.warnings]

    log("")
    log("=" * 72)
    log("SUMMARY")
    log("=" * 72)
    log("  candidates processed : %d" % len(results))
    log("  succeeded            : %d" % len(ok))
    log("  failed               : %d" % len(bad))
    if warned:
        log("  succeeded w/ warnings: %d  (check the ! lines above -- the crop or" % len(warned))
        log("                          timeline size may not have applied)")
    if args.no_render:
        log("  render               : skipped (--no-render); timelines are in the project")
    else:
        log("  output folder        : %s" % args.out_dir)
    if ok and not args.no_render:
        log("")
        log("  files:")
        for r in ok:
            log("    %s" % (r.output or "(no file)"))
    if bad:
        log("")
        log("  failures:")
        for r in bad:
            log("    [%d] %s: %s" % (r.index, r.stem, r.error))
    log("")
    if ok and not args.no_render:
        log("  Open one and watch it before trusting the rest: cut points and crop")
        log("  are the two things that go wrong silently.")
    return 0 if not bad else 1


def build_parser():
    p = argparse.ArgumentParser(
        prog="resolve_cutter.py",
        description="Cut + centre-crop vertical 1080x1920 clips in DaVinci Resolve "
                    "from a *_clip_candidates.json file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run --check first. Then --only 0. Then the batch.",
    )
    p.add_argument("json", help="path to a *_clip_candidates.json file")
    p.add_argument("video", nargs="?", help="source video (inferred from the JSON name if omitted)")
    p.add_argument("--check", action="store_true",
                   help="preflight only: connect, introspect, print frame maths. Creates nothing.")
    p.add_argument("--only", type=int, metavar="N", help="process just candidate N (0-based)")
    p.add_argument("--limit", type=int, metavar="N", help="process only the first N candidates")
    p.add_argument("--no-render", action="store_true", help="build timelines but do not render")
    p.add_argument("--out-dir", help="output folder (default: clips_out next to the source video)")
    p.add_argument("--fit-mode", choices=("fit", "fill", "none"), default="fit",
                   help="how Resolve already scales mismatched footage (default: fit, the factory default)")
    p.add_argument("--zoom", type=float, help="override the computed zoom entirely")
    p.add_argument("--pan", type=float, default=0.0,
                   help="horizontal nudge in timeline px; +right. Per-candidate 'pan' in the JSON wins.")
    p.add_argument("--tilt", type=float, default=0.0, help="vertical nudge in timeline px; +down")
    p.add_argument("--quality", type=int, default=12000, metavar="KBPS",
                   help="H.264 bitrate target (default 12000 -- plenty for 1080x1920 social)")
    p.add_argument("--hook-len", type=int, default=40, help="max hook slug length in names (default 40)")
    p.add_argument("--timeline-prefix", default="", help="string prepended to every timeline name")
    p.add_argument("--poll", type=float, default=2.0, help="render poll interval, seconds")
    p.add_argument("--render-timeout", type=float, default=3600.0, help="per-clip render timeout, seconds")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    try:
        candidates = load_candidates(args.json)
    except (OSError, ValueError, ResolveError) as exc:
        print("ERROR reading candidates: %s" % exc, file=sys.stderr)
        return 2

    if not args.video:
        args.video = guess_source_video(args.json)
        if not args.video:
            print("ERROR: could not infer the source video from %r -- pass it explicitly."
                  % args.json, file=sys.stderr)
            return 2
        print("Source video inferred from JSON name: %s" % args.video)
    args.video = os.path.abspath(args.video)
    if not os.path.isfile(args.video):
        print("ERROR: source video not found: %s" % args.video, file=sys.stderr)
        return 2

    if not args.out_dir:
        args.out_dir = os.path.join(os.path.dirname(args.video), "clips_out")
    args.out_dir = os.path.abspath(args.out_dir)

    if args.only is not None:
        picked = [c for c in candidates if c.index == args.only]
        if not picked:
            print("ERROR: no candidate with index %d (file has %d)" % (args.only, len(candidates)),
                  file=sys.stderr)
            return 2
        candidates = picked
    elif args.limit:
        candidates = candidates[: args.limit]

    try:
        if args.check:
            return preflight(args, candidates)
        return run(args, candidates)
    except ResolveError as exc:
        print("", file=sys.stderr)
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
