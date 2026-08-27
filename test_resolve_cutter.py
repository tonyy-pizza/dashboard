#!/usr/bin/env python
"""
test_resolve_cutter.py -- exercise resolve_cutter.py without DaVinci Resolve.

Two halves:

* Pure maths (frame ranges, zoom, slugs, JSON parsing) tested directly.
* The whole build+render pass driven against a fake Resolve object graph, so
  the call *sequence* is checked -- in/out points reaching AppendToTimeline,
  timeline settings landing before the append, the render poll actually waiting
  for IsRenderingInProgress to clear.

What this cannot do is validate the real API: the fake answers the way the docs
and community scripts say Resolve answers, and only the live application can
confirm that. Run `resolve_cutter.py --check` on the real machine for that.

    python test_resolve_cutter.py
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import resolve_cutter as rc


# --------------------------------------------------------------------------
# Pure logic
# --------------------------------------------------------------------------

class TestFrameMath(unittest.TestCase):
    def test_endframe_is_inclusive(self):
        # 0.0s-1.0s at 24fps is frames 0..23 -- 24 frames, not 25.
        first, last = rc.candidate_frame_range(0.0, 1.0, 24.0)
        self.assertEqual((first, last), (0, 23))
        self.assertEqual(last - first + 1, 24)

    def test_ntsc_rate_is_not_rounded_to_30(self):
        # 60s at 29.97 is frame 1798, not 1800. Assuming 30 drifts 2 frames
        # here and keeps growing.
        first, last = rc.candidate_frame_range(60.0, 70.0, 29.97)
        self.assertEqual(first, 1798)
        self.assertEqual(last, 2097)
        self.assertNotEqual(first, 1800)

    def test_2398_and_5994(self):
        self.assertEqual(rc.candidate_frame_range(10.0, 11.0, 23.976)[0], 240)
        self.assertEqual(rc.candidate_frame_range(10.0, 11.0, 59.94)[0], 599)

    def test_non_zero_clip_start_offsets_everything(self):
        # Media with embedded timecode starts at its TC, not 0.
        first, last = rc.candidate_frame_range(1.0, 2.0, 25.0, clip_start_frame=90000)
        self.assertEqual((first, last), (90025, 90049))

    def test_clamped_to_clip_length(self):
        first, last = rc.candidate_frame_range(0.0, 100.0, 25.0, 0, clip_frame_count=1000)
        self.assertEqual(last, 999)

    def test_start_past_end_of_clip_raises(self):
        with self.assertRaises(rc.ResolveError):
            rc.candidate_frame_range(50.0, 55.0, 25.0, 0, clip_frame_count=100)

    def test_reversed_range_raises(self):
        with self.assertRaises(rc.ResolveError):
            rc.candidate_frame_range(10.0, 5.0, 25.0)

    def test_sub_frame_candidate_keeps_one_frame(self):
        first, last = rc.candidate_frame_range(1.0, 1.001, 24.0)
        self.assertEqual(first, last)

    def test_no_cumulative_drift_across_a_batch(self):
        # Each candidate is computed from absolute seconds, so clip 50 is as
        # accurate as clip 1.
        fps = 29.97
        for i in range(0, 60):
            start = i * 60.0
            first, _ = rc.candidate_frame_range(start, start + 5.0, fps)
            self.assertEqual(first, int(round(start * fps)))

    def test_timecode(self):
        self.assertEqual(rc.frames_to_timecode(0, 24), "00:00:00:00")
        self.assertEqual(rc.frames_to_timecode(24, 24), "00:00:01:00")
        self.assertEqual(rc.frames_to_timecode(3600 * 25 + 7, 25), "01:00:00:07")


class TestFpsParsing(unittest.TestCase):
    def test_strings_and_numbers(self):
        self.assertAlmostEqual(rc.parse_fps("29.97"), 29.97)
        self.assertAlmostEqual(rc.parse_fps("23.976"), 23.976)
        self.assertAlmostEqual(rc.parse_fps(60), 60.0)
        self.assertAlmostEqual(rc.parse_fps("24 fps"), 24.0)

    def test_garbage_raises(self):
        for bad in (None, "", "n/a", "0", "99999"):
            with self.assertRaises(rc.ResolveError):
                rc.parse_fps(bad)

    def test_format_fps_round_trip(self):
        self.assertEqual(rc.format_fps(24.0), "24")
        self.assertEqual(rc.format_fps(29.97), "29.97")
        self.assertEqual(rc.format_fps(23.976), "23.976")


class TestZoomMath(unittest.TestCase):
    def test_fit_mode_16x9_to_9x16(self):
        # Default Resolve behaviour letterboxes 1920x1080 to 1080x607.5, so the
        # zoom back up to full height is 1920/607.5 = 3.1605.
        z = rc.compute_fill_zoom(1920, 1080, fit_mode="fit")
        self.assertAlmostEqual(z, 3.16049, places=4)

    def test_fill_mode_needs_no_zoom(self):
        self.assertAlmostEqual(rc.compute_fill_zoom(1920, 1080, fit_mode="fill"), 1.0)

    def test_none_mode_is_raw_scale(self):
        self.assertAlmostEqual(rc.compute_fill_zoom(1920, 1080, fit_mode="none"), 1920 / 1080.0, places=5)

    def test_4k_source(self):
        # Aspect ratio drives it, not pixel count: 3840x2160 is still 16:9.
        self.assertAlmostEqual(
            rc.compute_fill_zoom(3840, 2160, fit_mode="fit"),
            rc.compute_fill_zoom(1920, 1080, fit_mode="fit"), places=6)

    def test_already_vertical_source_is_untouched(self):
        self.assertAlmostEqual(rc.compute_fill_zoom(1080, 1920, fit_mode="fit"), 1.0)

    def test_visible_fraction_flags_how_much_is_lost(self):
        fw, fh = rc.visible_source_fraction(1920, 1080)
        self.assertAlmostEqual(fw, 0.31640625, places=6)  # ~32% of width survives
        self.assertAlmostEqual(fh, 1.0)

    def test_bad_resolution_raises(self):
        with self.assertRaises(rc.ResolveError):
            rc.compute_fill_zoom(0, 1080)


class TestNaming(unittest.TestCase):
    def test_stem_shape(self):
        self.assertEqual(
            rc.make_stem(0, "hot_take", "This is THE hook, right here!"),
            "00_hot-take_this-is-the-hook-right-here")

    def test_index_is_zero_padded(self):
        self.assertTrue(rc.make_stem(7, "x", "y").startswith("07_"))

    def test_slug_is_filesystem_safe(self):
        stem = rc.make_stem(1, "a/b", 'why: "cash flow" > profit? <100%>')
        for ch in '/\\:*?"<>|':
            self.assertNotIn(ch, stem)

    def test_unicode_is_transliterated_not_dropped_wholesale(self):
        self.assertEqual(rc.slugify("café déjà vu"), "cafe-deja-vu")

    def test_long_hooks_truncate_on_word_boundary(self):
        slug = rc.slugify("the quick brown fox jumps over the lazy dog again and again", 40)
        self.assertLessEqual(len(slug), 40)
        self.assertFalse(slug.endswith("-"))

    def test_empty_fields_degrade_gracefully(self):
        self.assertEqual(rc.make_stem(3, "", ""), "03")

    def test_stems_stay_unique_per_index(self):
        stems = {rc.make_stem(i, "same", "same hook") for i in range(5)}
        self.assertEqual(len(stems), 5)


class TestCandidateLoading(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, data, name="vid1_clip_candidates.json"):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return path

    def test_reads_cf_py_shape(self):
        path = self.write([
            {"start": 12.5, "end": 45.0, "category": "story",
             "hook_line": "I lost everything", "why": "emotional peak"},
        ])
        cands = rc.load_candidates(path)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].category, "story")
        self.assertAlmostEqual(cands[0].duration, 32.5)

    def test_accepts_wrapper_object(self):
        path = self.write({"candidates": [{"start": 0, "end": 5}]})
        self.assertEqual(len(rc.load_candidates(path)), 1)

    def test_missing_keys_raise_with_the_index(self):
        path = self.write([{"start": 0, "end": 5}, {"start": 10}])
        with self.assertRaises(rc.ResolveError) as ctx:
            rc.load_candidates(path)
        self.assertIn("1", str(ctx.exception))

    def test_empty_list_raises(self):
        with self.assertRaises(rc.ResolveError):
            rc.load_candidates(self.write([]))

    def test_optional_per_candidate_pan(self):
        path = self.write([{"start": 0, "end": 5, "pan": -120}])
        self.assertAlmostEqual(rc.load_candidates(path)[0].pan, -120.0)

    def test_guesses_sibling_video(self):
        path = self.write([{"start": 0, "end": 1}])
        open(os.path.join(self.dir, "vid1.mp4"), "wb").close()
        self.assertEqual(rc.guess_source_video(path), os.path.join(self.dir, "vid1.mp4"))

    def test_guess_returns_none_when_absent(self):
        self.assertIsNone(rc.guess_source_video(self.write([{"start": 0, "end": 1}])))


# --------------------------------------------------------------------------
# Fake Resolve
# --------------------------------------------------------------------------

class FakeTimelineItem:
    """Mimics TimelineItem, including GetProperty() -> full property dict."""

    PROPS = {"ZoomX": 1.0, "ZoomY": 1.0, "Pan": 0.0, "Tilt": 0.0,
             "RotationAngle": 0.0, "AnchorPointX": 0.0, "AnchorPointY": 0.0,
             "CropLeft": 0.0, "CropRight": 0.0, "CropTop": 0.0, "CropBottom": 0.0}

    def __init__(self, start, end):
        self.start, self.end = start, end
        self.props = dict(self.PROPS)

    def GetProperty(self, key=None):
        return dict(self.props) if key is None else self.props.get(key)

    def SetProperty(self, key, value):
        if key not in self.PROPS:
            return False
        self.props[key] = float(value)
        return True


class FakeTimeline:
    def __init__(self, name):
        self.name = name
        self.settings = {}
        self.items = []

    def GetName(self):
        return self.name

    def SetSetting(self, key, value):
        if self.items and key == "timelineFrameRate":
            return False  # Resolve locks the rate once a timeline has clips
        self.settings[key] = str(value)
        return True

    def GetSetting(self, key):
        return self.settings.get(key)


class FakeMediaPoolItem:
    def __init__(self, path, props):
        self.path = path
        self.props = dict(props)
        self.props.setdefault("File Path", path)
        self.props.setdefault("File Name", os.path.basename(path))

    def GetName(self):
        return os.path.basename(self.path)

    def GetClipProperty(self, key=None):
        return dict(self.props) if key is None else self.props.get(key, "")


class FakeFolder:
    def __init__(self, clips=None):
        self.clips = clips or []

    def GetClipList(self):
        return list(self.clips)

    def GetSubFolderList(self):
        return []


class FakeMediaPool:
    def __init__(self, project):
        self.project = project
        self.folder = FakeFolder()
        self.imports = []
        self.appends = []

    def GetRootFolder(self):
        return self.folder

    def ImportMedia(self, paths):
        self.imports.append(list(paths))
        items = [FakeMediaPoolItem(p, self.project.clip_props) for p in paths]
        self.folder.clips.extend(items)
        return items

    def CreateEmptyTimeline(self, name):
        if any(t.name == name for t in self.project.timelines):
            return None
        tl = FakeTimeline(name)
        self.project.timelines.append(tl)
        return tl

    def AppendToTimeline(self, clip_infos):
        tl = self.project.current_timeline
        out = []
        for info in clip_infos:
            # Record what the caller actually asked for -- this is the assertion
            # target for "in/out set at append time, not trimmed afterwards".
            self.appends.append({
                "timeline": tl.name,
                "startFrame": info.get("startFrame"),
                "endFrame": info.get("endFrame"),
                "resolution": (tl.GetSetting("timelineResolutionWidth"),
                               tl.GetSetting("timelineResolutionHeight")),
                "frameRate": tl.GetSetting("timelineFrameRate"),
            })
            item = FakeTimelineItem(info.get("startFrame"), info.get("endFrame"))
            tl.items.append(item)
            out.append(item)
        return out


class FakeProject:
    def __init__(self, clip_props, out_root, poll_ticks=2):
        self.clip_props = clip_props
        self.out_root = out_root
        self.timelines = []
        self.current_timeline = None
        self.media_pool = FakeMediaPool(self)
        self.render_settings = {}
        self.jobs = []
        self.started = []
        self.render_log = []
        self._ticks = 0
        self._poll_ticks = poll_ticks
        self._rendering_job = None
        self.fmt = self.codec = None

    def GetName(self):
        return "FakeProject"

    def GetTimelineCount(self):
        return len(self.timelines)

    def GetMediaPool(self):
        return self.media_pool

    def SetCurrentTimeline(self, tl):
        self.current_timeline = tl
        return True

    def GetRenderFormats(self):
        return {"QuickTime": "mov", "MP4": "mp4", "MXF OP1A": "mxf"}

    def GetRenderCodecs(self, fmt):
        # Deliberately messy, the way a real install is.
        return {"H264_NVIDIA": "H.264 NVIDIA", "H264": "H.264", "H265": "H.265"}

    def SetCurrentRenderFormatAndCodec(self, fmt, codec):
        self.fmt, self.codec = fmt, codec
        return True

    def SetRenderSettings(self, settings):
        if "AudioBitDepth" in settings:
            return False  # one unsupported key, to prove it is non-fatal
        self.render_settings.update(settings)
        return True

    def GetRenderJobList(self):
        return list(self.jobs)

    def AddRenderJob(self):
        job = {"JobId": "job%d" % (len(self.jobs) + 1),
               "TargetDir": self.render_settings.get("TargetDir"),
               "OutputFilename": self.render_settings.get("CustomName")}
        self.jobs.append(job)
        return job["JobId"]

    def StartRendering(self, *args):
        job_id = args[0][0] if args and isinstance(args[0], list) else args[0]
        if self._rendering_job is not None:
            raise AssertionError("StartRendering called while a job was still running")
        self._rendering_job = job_id
        self.started.append(job_id)
        self._ticks = 0
        return True

    def IsRenderingInProgress(self):
        # Non-blocking, exactly like the real one: busy for a few polls, then
        # the file appears.
        if self._rendering_job is None:
            return False
        self._ticks += 1
        if self._ticks <= self._poll_ticks:
            return True
        job = next(j for j in self.jobs if j["JobId"] == self._rendering_job)
        target = os.path.join(job["TargetDir"], job["OutputFilename"] + ".mp4")
        os.makedirs(job["TargetDir"], exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(b"\x00" * 2048)
        self.render_log.append(target)
        self._rendering_job = None
        return False

    def GetRenderJobStatus(self, job_id):
        return {"JobStatus": "Complete", "CompletionPercentage": 100}

    def StopRendering(self):
        self._rendering_job = None


class FakeProjectManager:
    def __init__(self, project):
        self.project = project

    def GetCurrentProject(self):
        return self.project


class FakeResolve:
    def __init__(self, project):
        self.pm = FakeProjectManager(project)
        self.page = "media"

    def GetProductName(self):
        return "DaVinci Resolve Studio"

    def GetVersion(self):
        return [19, 0, 3, 0, 0]

    def GetProjectManager(self):
        return self.pm

    def GetCurrentPage(self):
        return self.page

    def OpenPage(self, page):
        self.page = page
        return True


class Args:
    """Stand-in for the argparse namespace."""

    def __init__(self, **kw):
        self.video = kw.get("video")
        self.out_dir = kw.get("out_dir")
        self.fit_mode = kw.get("fit_mode", "fit")
        self.zoom = kw.get("zoom")
        self.pan = kw.get("pan", 0.0)
        self.tilt = kw.get("tilt", 0.0)
        self.quality = kw.get("quality", 12000)
        self.hook_len = kw.get("hook_len", 40)
        self.timeline_prefix = kw.get("timeline_prefix", "")
        self.no_render = kw.get("no_render", False)
        self.poll = kw.get("poll", 0.0)
        self.render_timeout = kw.get("render_timeout", 30.0)
        self.check = kw.get("check", False)
        self.only = kw.get("only")
        self.limit = kw.get("limit")


CLIP_PROPS_2997 = {
    "FPS": "29.97", "Resolution": "1920x1080", "Start": "0",
    "Frames": "18000", "End": "17999",
}


class FakeRunCase(unittest.TestCase):
    """Base: wires a fake Resolve into resolve_cutter and cleans up after."""

    clip_props = CLIP_PROPS_2997
    poll_ticks = 2

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.video = os.path.join(self.dir, "vid1.mp4")
        with open(self.video, "wb") as fh:
            fh.write(b"\x00" * 128)
        self.out_dir = os.path.join(self.dir, "clips_out")
        self.project = FakeProject(self.clip_props, self.out_dir, self.poll_ticks)
        self.resolve = FakeResolve(self.project)
        self._real_connect = rc.connect
        rc.connect = lambda: (self.resolve, ["fake connection"])
        self.logs = []

    def tearDown(self):
        rc.connect = self._real_connect
        shutil.rmtree(self.dir, ignore_errors=True)

    def log(self, *a):
        self.logs.append(" ".join(str(x) for x in a))

    def args(self, **kw):
        kw.setdefault("video", self.video)
        kw.setdefault("out_dir", self.out_dir)
        return Args(**kw)

    def candidates(self, specs):
        return [
            rc.Candidate(index=i, start=s, end=e, category=c, hook_line=h, why="", pan=p)
            for i, (s, e, c, h, p) in enumerate(specs)
        ]

    def default_candidates(self):
        return self.candidates([
            (12.5, 45.0, "story", "I lost everything in one week", None),
            (120.0, 151.25, "hot take", "Nobody tells you this part", None),
            (300.0, 322.0, "tip", "Do this before you quit", None),
        ])


class TestEndToEnd(FakeRunCase):
    def test_full_batch_succeeds_and_writes_files(self):
        rc_code = rc.run(self.args(), self.default_candidates(), log=self.log)
        self.assertEqual(rc_code, 0)
        self.assertEqual(len(self.project.render_log), 3)
        for path in self.project.render_log:
            self.assertTrue(os.path.isfile(path))
            self.assertTrue(path.endswith(".mp4"))
            self.assertEqual(os.path.dirname(path), self.out_dir)

    def test_in_out_points_are_set_at_append_time(self):
        rc.run(self.args(), self.default_candidates(), log=self.log)
        appends = self.project.media_pool.appends
        self.assertEqual(len(appends), 3)
        # 12.5s-45.0s at 29.97 -> frames 375..1348 inclusive (974 frames)
        self.assertEqual(appends[0]["startFrame"], 375)
        self.assertEqual(appends[0]["endFrame"], 1348)
        self.assertEqual(appends[0]["endFrame"] - appends[0]["startFrame"] + 1, 974)
        for a in appends:
            self.assertIsNotNone(a["startFrame"])
            self.assertIsNotNone(a["endFrame"])

    def test_timeline_is_vertical_before_the_clip_lands(self):
        rc.run(self.args(), self.default_candidates(), log=self.log)
        for a in self.project.media_pool.appends:
            self.assertEqual(a["resolution"], ("1080", "1920"))
            self.assertEqual(a["frameRate"], "29.97")
        for tl in self.project.timelines:
            self.assertEqual(tl.GetSetting("useCustomSettings"), "1")
            self.assertEqual(tl.GetSetting("timelineOutputResolutionHeight"), "1920")

    def test_crop_is_applied_and_centred(self):
        rc.run(self.args(), self.default_candidates(), log=self.log)
        for tl in self.project.timelines:
            item = tl.items[0]
            self.assertAlmostEqual(item.GetProperty("ZoomX"), 3.16049, places=4)
            self.assertAlmostEqual(item.GetProperty("ZoomY"), 3.16049, places=4)
            self.assertEqual(item.GetProperty("Pan"), 0.0)
            self.assertEqual(item.GetProperty("Tilt"), 0.0)

    def test_timelines_named_from_index_category_hook(self):
        rc.run(self.args(), self.default_candidates(), log=self.log)
        names = [t.name for t in self.project.timelines]
        self.assertEqual(names[0], "00_story_i-lost-everything-in-one-week")
        self.assertEqual(names[1], "01_hot-take_nobody-tells-you-this-part")

    def test_output_filename_matches_timeline_slug(self):
        rc.run(self.args(), self.default_candidates(), log=self.log)
        for tl, path in zip(self.project.timelines, self.project.render_log):
            self.assertEqual(os.path.basename(path), tl.name + ".mp4")

    def test_candidates_are_processed_in_order(self):
        rc.run(self.args(), self.default_candidates(), log=self.log)
        starts = [a["startFrame"] for a in self.project.media_pool.appends]
        self.assertEqual(starts, sorted(starts))

    def test_h264_mp4_chosen_by_introspection_not_hardcoding(self):
        rc.run(self.args(), self.default_candidates(), log=self.log)
        self.assertEqual(self.project.fmt, "mp4")
        # "H.264" must win over the longer "H.264 NVIDIA" variant.
        self.assertEqual(self.project.codec, "H.264")

    def test_render_settings_carry_vertical_size_and_target(self):
        rc.run(self.args(), self.default_candidates(), log=self.log)
        rs = self.project.render_settings
        self.assertEqual(rs["FormatWidth"], 1080)
        self.assertEqual(rs["FormatHeight"], 1920)
        self.assertEqual(rs["TargetDir"], self.out_dir)
        self.assertEqual(rs["FrameRate"], "29.97")
        self.assertTrue(rs["SelectAllFrames"])

    def test_unsupported_render_setting_is_survivable(self):
        # FakeProject rejects AudioBitDepth; the batch must still finish.
        code = rc.run(self.args(), self.default_candidates(), log=self.log)
        self.assertEqual(code, 0)
        self.assertTrue(any("AudioBitDepth" in l for l in self.logs))

    def test_unsupported_render_setting_warns_once_not_per_clip(self):
        rc.run(self.args(), self.default_candidates(), log=self.log)
        self.assertEqual(sum("AudioBitDepth" in l for l in self.logs), 1)

    def test_jobs_render_one_at_a_time(self):
        # FakeProject.StartRendering raises if a job is already running, so a
        # green run here is the proof that the poll loop actually waits.
        rc.run(self.args(), self.default_candidates(), log=self.log)
        self.assertEqual(len(self.project.started), 3)
        self.assertEqual(len(set(self.project.started)), 3)

    def test_only_our_own_jobs_are_started(self):
        self.project.jobs.append({"JobId": "preexisting", "TargetDir": self.out_dir,
                                  "OutputFilename": "users_own_export"})
        rc.run(self.args(), self.default_candidates(), log=self.log)
        self.assertNotIn("preexisting", self.project.started)
        self.assertFalse(os.path.exists(os.path.join(self.out_dir, "users_own_export.mp4")))

    def test_output_folder_created_next_to_source(self):
        self.assertFalse(os.path.isdir(self.out_dir))
        rc.run(self.args(), self.default_candidates(), log=self.log)
        self.assertTrue(os.path.isdir(self.out_dir))
        self.assertEqual(os.path.dirname(self.out_dir), self.dir)

    def test_summary_reports_counts_and_location(self):
        rc.run(self.args(), self.default_candidates(), log=self.log)
        text = "\n".join(self.logs)
        self.assertIn("SUMMARY", text)
        self.assertIn("candidates processed : 3", text)
        self.assertIn("succeeded            : 3", text)
        self.assertIn("failed               : 0", text)
        self.assertIn(self.out_dir, text)


class TestSingleClipFirst(FakeRunCase):
    def test_one_candidate_runs_the_same_path(self):
        code = rc.run(self.args(), self.default_candidates()[:1], log=self.log)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.project.timelines), 1)
        self.assertEqual(len(self.project.render_log), 1)


class TestMediaPoolReuse(FakeRunCase):
    def test_imports_once(self):
        rc.run(self.args(), self.default_candidates(), log=self.log)
        self.assertEqual(len(self.project.media_pool.imports), 1)

    def test_existing_clip_is_not_reimported(self):
        self.project.media_pool.folder.clips.append(
            FakeMediaPoolItem(self.video, self.clip_props))
        rc.run(self.args(), self.default_candidates(), log=self.log)
        self.assertEqual(self.project.media_pool.imports, [])

    def test_matched_by_filename_when_path_differs(self):
        other = os.path.join(self.dir, "elsewhere", "vid1.mp4")
        os.makedirs(os.path.dirname(other))
        self.project.media_pool.folder.clips.append(
            FakeMediaPoolItem(other, self.clip_props))
        rc.run(self.args(), self.default_candidates(), log=self.log)
        self.assertEqual(self.project.media_pool.imports, [])


class TestFrameRateIsRead(FakeRunCase):
    clip_props = {"FPS": "23.976", "Resolution": "3840x2160", "Start": "0", "Frames": "50000"}

    def test_source_rate_drives_the_maths(self):
        rc.run(self.args(), self.default_candidates()[:1], log=self.log)
        a = self.project.media_pool.appends[0]
        # 12.5s at 23.976 -> frame 300; a hardcoded 30 would have said 375.
        self.assertEqual(a["startFrame"], 300)
        self.assertEqual(a["frameRate"], "23.976")
        self.assertNotEqual(a["startFrame"], 375)

    def test_4k_source_still_gets_the_same_zoom(self):
        rc.run(self.args(), self.default_candidates()[:1], log=self.log)
        self.assertAlmostEqual(
            self.project.timelines[0].items[0].GetProperty("ZoomX"), 3.16049, places=4)


class TestTimecodeOffsetSource(FakeRunCase):
    clip_props = {"FPS": "25", "Resolution": "1920x1080", "Start": "90000", "Frames": "25000"}

    def test_cut_points_are_offset_by_the_clips_own_start(self):
        rc.run(self.args(), self.candidates([(10.0, 20.0, "x", "y", None)]), log=self.log)
        a = self.project.media_pool.appends[0]
        self.assertEqual(a["startFrame"], 90250)
        self.assertEqual(a["endFrame"], 90499)


class TestReframeOptions(FakeRunCase):
    def test_zoom_override(self):
        rc.run(self.args(zoom=2.5), self.default_candidates()[:1], log=self.log)
        self.assertAlmostEqual(self.project.timelines[0].items[0].GetProperty("ZoomX"), 2.5)

    def test_fill_fit_mode_does_not_double_zoom(self):
        rc.run(self.args(fit_mode="fill"), self.default_candidates()[:1], log=self.log)
        self.assertAlmostEqual(self.project.timelines[0].items[0].GetProperty("ZoomX"), 1.0)

    def test_global_pan_applies(self):
        rc.run(self.args(pan=-150.0), self.default_candidates()[:1], log=self.log)
        self.assertAlmostEqual(self.project.timelines[0].items[0].GetProperty("Pan"), -150.0)

    def test_per_candidate_pan_overrides_global(self):
        cands = self.candidates([(10.0, 20.0, "x", "off centre speaker", 220.0)])
        rc.run(self.args(pan=-150.0), cands, log=self.log)
        self.assertAlmostEqual(self.project.timelines[0].items[0].GetProperty("Pan"), 220.0)


class TestCropIntrospection(FakeRunCase):
    def test_zoom_falls_back_to_single_zoom_property(self):
        class SingleZoom(FakeTimelineItem):
            PROPS = {"Zoom": 1.0, "Pan": 0.0, "Tilt": 0.0}

        item = SingleZoom(0, 100)
        applied, problems = rc.apply_fill_crop(item, 3.16049, log=self.log)
        self.assertIn("Zoom", applied)
        self.assertNotIn("ZoomX", applied)
        self.assertAlmostEqual(item.GetProperty("Zoom"), 3.16049, places=4)

    def test_missing_zoom_property_is_reported_not_silent(self):
        class NoZoom(FakeTimelineItem):
            PROPS = {"CropLeft": 0.0, "CropRight": 0.0}

        applied, problems = rc.apply_fill_crop(NoZoom(0, 100), 3.16, log=self.log)
        self.assertTrue(any("no usable zoom property" in p for p in problems))

    def test_a_lying_setproperty_is_caught_on_readback(self):
        class Liar(FakeTimelineItem):
            def SetProperty(self, key, value):
                return True  # claims success, changes nothing

        applied, problems = rc.apply_fill_crop(Liar(0, 100), 3.16, log=self.log)
        self.assertTrue(any("read back" in p for p in problems))
        self.assertNotIn("ZoomX", applied)

    def test_warnings_surface_in_the_summary(self):
        original = rc.apply_fill_crop
        rc.apply_fill_crop = lambda item, zoom, pan=0.0, tilt=0.0, log=print: ({}, ["crop did not apply"])
        try:
            rc.run(self.args(), self.default_candidates()[:1], log=self.log)
        finally:
            rc.apply_fill_crop = original
        self.assertIn("succeeded w/ warnings", "\n".join(self.logs))


class TestFailureHandling(FakeRunCase):
    def test_one_bad_candidate_does_not_sink_the_batch(self):
        cands = self.candidates([
            (12.5, 45.0, "story", "good one", None),
            (50.0, 40.0, "bad", "reversed range", None),   # end before start
            (300.0, 322.0, "tip", "another good one", None),
        ])
        code = rc.run(self.args(), cands, log=self.log)
        self.assertEqual(code, 1)                      # non-zero: something failed
        self.assertEqual(len(self.project.render_log), 2)  # the good ones still rendered
        text = "\n".join(self.logs)
        self.assertIn("succeeded            : 2", text)
        self.assertIn("failed               : 1", text)
        self.assertIn("reversed", text.lower())

    def test_duplicate_timeline_name_is_reported(self):
        self.project.timelines.append(FakeTimeline("00_story_i-lost-everything-in-one-week"))
        code = rc.run(self.args(), self.default_candidates()[:1], log=self.log)
        self.assertEqual(code, 1)
        self.assertIn("already exists", "\n".join(self.logs))

    def test_missing_output_file_is_a_failure_not_a_success(self):
        self.project.IsRenderingInProgress = lambda: False  # never writes anything
        code = rc.run(self.args(), self.default_candidates()[:1], log=self.log)
        self.assertEqual(code, 1)
        self.assertIn("no output file", "\n".join(self.logs))

    def test_render_timeout_stops_rather_than_hanging(self):
        self.project.IsRenderingInProgress = lambda: True  # never finishes
        code = rc.run(self.args(render_timeout=0.01, poll=0.0),
                      self.default_candidates()[:1], log=self.log)
        self.assertEqual(code, 1)
        self.assertIn("exceeded", "\n".join(self.logs))

    def test_no_project_open_fails_loudly_without_creating_one(self):
        self.resolve.pm.GetCurrentProject = lambda: None
        with self.assertRaises(rc.ResolveError) as ctx:
            rc.run(self.args(), self.default_candidates(), log=self.log)
        msg = str(ctx.exception)
        self.assertIn("No project is open", msg)
        self.assertEqual(self.project.timelines, [])

    def test_no_render_builds_timelines_only(self):
        code = rc.run(self.args(no_render=True), self.default_candidates(), log=self.log)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.project.timelines), 3)
        self.assertEqual(self.project.render_log, [])
        self.assertFalse(os.path.isdir(self.out_dir))


class TestConnectionErrors(unittest.TestCase):
    """The first thing a user with an unconfigured machine ever sees."""

    def test_missing_module_raises_a_readable_error_not_a_traceback(self):
        real_dirs = dict(rc._MODULE_DIRS)
        rc._MODULE_DIRS = {"win32": [], "darwin": [], "linux": ["/nonexistent/Modules"]}
        sys.modules.pop("DaVinciResolveScript", None)
        try:
            with self.assertRaises(rc.ResolveError) as ctx:
                rc.load_resolve_module()
        finally:
            rc._MODULE_DIRS = real_dirs
        msg = str(ctx.exception)
        self.assertIn("RESOLVE_SCRIPT_API", msg)
        self.assertIn("/nonexistent/Modules", msg)

    def test_scriptapp_none_explains_the_local_scripting_setting(self):
        class Stub:
            @staticmethod
            def scriptapp(name):
                return None

        real = rc.load_resolve_module
        rc.load_resolve_module = lambda: (Stub, [])
        try:
            with self.assertRaises(rc.ResolveError) as ctx:
                rc.connect()
        finally:
            rc.load_resolve_module = real
        msg = str(ctx.exception)
        self.assertIn("Local", msg)
        self.assertIn("running", msg)


class TestPreflight(FakeRunCase):
    def test_check_creates_nothing(self):
        args = self.args(check=True)
        code = rc.preflight(args, self.default_candidates(), log=self.log)
        self.assertEqual(code, 0)
        self.assertEqual(self.project.timelines, [])
        self.assertEqual(self.project.render_log, [])
        self.assertEqual(self.project.started, [])

    def test_check_reports_the_facts_that_matter(self):
        rc.preflight(self.args(check=True), self.default_candidates(), log=self.log)
        text = "\n".join(self.logs)
        for expected in ("29.97", "1920x1080", "3.1605", "mp4", "H.264",
                         "00:00:12:15", "endFrame is inclusive"):
            self.assertIn(expected, text)

    def test_check_flags_a_preexisting_render_queue(self):
        self.project.jobs.append({"JobId": "old", "TargetDir": "/x", "OutputFilename": "y"})
        rc.preflight(self.args(check=True), self.default_candidates(), log=self.log)
        self.assertIn("already in this project's queue", "\n".join(self.logs))

    def test_check_returns_nonzero_when_a_candidate_is_unusable(self):
        cands = self.candidates([(12.5, 45.0, "ok", "fine", None),
                                 (99999.0, 99999.5, "bad", "past the end", None)])
        self.assertEqual(rc.preflight(self.args(check=True), cands, log=self.log), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
