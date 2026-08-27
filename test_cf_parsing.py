#!/usr/bin/env python3
"""
Parser regression tests for cf.py.

These feed `find_moments` the kinds of output a local thinking model actually
produces - <think> blocks, markdown fences, objects where an array was asked
for, truncated JSON, "01:23" timestamps - and assert that clips come out and
that anything dropped is explained in the notes.

Run:  py -m unittest test_cf_parsing -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cf


CLIPS = [
    {"start": 120.5, "end": 155.0, "category": "numbers", "strength": 9,
     "hook_line": "I lost four hundred grand in a weekend.",
     "why": "Big number, said flat, high replay value."},
    {"start": 402.0, "end": 441.0, "category": "hot_take", "strength": 8,
     "hook_line": "Ninety percent of this industry is a marketing funnel.",
     "why": "Debate bait, clean standalone quote."},
]

# A synthetic chunk: one word every 0.5s from 0s to 600s, so word "wN" sits at
# N*0.5 seconds. That makes an excerpt's contents assertable by index.
def make_chunk(start=0.0, end=600.0, step=0.5):
    n = int((end - start) / step)
    return [{"word": f" w{i}", "start": round(start + i * step, 2),
             "end": round(start + i * step + step * 0.8, 2)} for i in range(n)]


CHUNK = make_chunk()


class FakeResponse(dict):
    """Mimics ollama's subscriptable response object."""


def fake_resp(response="", thinking="", done_reason="stop", **extra):
    d = FakeResponse({
        "response": response,
        "thinking": thinking,
        "done_reason": done_reason,
        "prompt_eval_count": 3200,
        "eval_count": 240,
    })
    d.update(extra)
    return d


class FakeClient:
    def __init__(self, response="", **kw):
        self._resp = fake_resp(response, **kw)
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return self._resp


class Args:
    """Stand-in for the argparse namespace."""
    model = "qwen3:8b"
    num_ctx = 8192
    num_predict = 2048
    temperature = 0.2
    think = False
    schema = False
    per_chunk = cf.MAX_CANDIDATES_PER_CHUNK
    min_strength = cf.MIN_STRENGTH
    top = 0


def score(raw, chunk=None, args=None, **respkw):
    """Run one chunk through the real pipeline with a stubbed Ollama."""
    client = FakeClient(raw, **respkw)
    cf._client = client
    cf._think_supported = True
    clips, diag = cf.find_moments(chunk or CHUNK, 1, args or Args(), None)
    return clips, diag, client


class TestHostileModelOutput(unittest.TestCase):

    def tearDown(self):
        cf._client = None

    # ── the happy paths ─────────────────────────────────────────────────

    def test_bare_array(self):
        clips, diag, _ = score(json.dumps(CLIPS))
        self.assertEqual(len(clips), 2)
        self.assertEqual(diag["status"], "ok")
        self.assertEqual(clips[0]["hook_line"], CLIPS[0]["hook_line"])

    def test_clips_wrapper(self):
        clips, _, _ = score(json.dumps({"clips": CLIPS}))
        self.assertEqual(len(clips), 2)

    # ── suspect #1: thinking-tag pollution ──────────────────────────────

    def test_think_block_before_json(self):
        raw = ("<think>\nOkay, the user wants clip candidates. Let me scan "
               "for big numbers...\n</think>\n" + json.dumps(CLIPS))
        clips, diag, _ = score(raw)
        self.assertEqual(len(clips), 2)
        self.assertTrue(any("stripped 1 <think>" in n for n in diag["notes"]))

    def test_think_block_containing_braces_defeats_naive_slicing(self):
        """The reason we scan for balanced JSON instead of find('{')/rfind('}')."""
        raw = ('<think>I could return {"clips": []} but there are real moments '
               'here, e.g. {start: 120}. Let me build the array.</think>\n'
               + json.dumps(CLIPS))
        # A naive first-brace/last-brace slice grabs the reasoning too:
        naive = raw[raw.find("{"):raw.rfind("}") + 1]
        with self.assertRaises(json.JSONDecodeError):
            json.loads(naive)
        # The real parser handles it.
        clips, _, _ = score(raw)
        self.assertEqual(len(clips), 2)

    def test_unclosed_think_tag_is_flagged(self):
        raw = "<think>Let me work through the transcript. First I need to"
        clips, diag, _ = score(raw, done_reason="length")
        self.assertEqual(clips, [])
        self.assertTrue(any("unclosed <think>" in n for n in diag["notes"]))
        self.assertTrue(any("num_predict" in n for n in diag["notes"]))
        self.assertEqual(diag["status"], "parse_failed")

    def test_thinking_arrives_in_separate_field(self):
        clips, diag, _ = score(json.dumps({"clips": CLIPS}),
                               thinking="Scanning for six-figure numbers...")
        self.assertEqual(len(clips), 2)
        self.assertGreater(diag["thinking_chars"], 0)
        self.assertTrue(any("separate thinking" in n for n in diag["notes"]))

    def test_think_disabled_by_default(self):
        _, _, client = score(json.dumps(CLIPS))
        self.assertIs(client.calls[0].get("think"), False)

    def test_old_client_without_think_kwarg_still_works(self):
        class OldClient(FakeClient):
            def generate(self, **kwargs):
                if "think" in kwargs:
                    raise TypeError("generate() got an unexpected keyword "
                                    "argument 'think'")
                return super().generate(**kwargs)

        cf._client = OldClient(json.dumps(CLIPS))
        cf._think_supported = True
        clips, diag = cf.find_moments(CHUNK, 1, Args(), None)
        self.assertEqual(len(clips), 2)
        self.assertTrue(any("too old for think=False" in n for n in diag["notes"]))

    def test_connection_errors_are_not_swallowed_as_think_failures(self):
        class DeadClient(FakeClient):
            def generate(self, **kwargs):
                raise ConnectionError("connection refused")

        cf._client = DeadClient("")
        cf._think_supported = True
        clips, diag = cf.find_moments(CHUNK, 1, Args(), None)
        self.assertEqual(clips, [])
        self.assertEqual(diag["status"], "call_failed")
        self.assertTrue(any("connection refused" in n for n in diag["notes"]))

    # ── suspect #2: valid JSON, wrong shape ─────────────────────────────

    def test_empty_object_is_explained_not_silent(self):
        clips, diag, _ = score("{}")
        self.assertEqual(clips, [])
        self.assertTrue(any("empty object" in n for n in diag["notes"]))

    def test_unrecognized_wrapper_key_warns_and_recovers(self):
        clips, diag, _ = score(json.dumps({"clip_moments": CLIPS}))
        self.assertEqual(len(clips), 2)
        self.assertTrue(any("unrecognized key" in n for n in diag["notes"]))

    def test_dict_with_no_list_anywhere_names_its_keys(self):
        clips, diag, _ = score(json.dumps({"summary": "nothing spicy",
                                           "confidence": 0.4}))
        self.assertEqual(clips, [])
        joined = " ".join(diag["notes"])
        self.assertIn("no recognized wrapper key", joined)
        self.assertIn("summary", joined)

    def test_single_clip_object_gets_wrapped(self):
        clips, diag, _ = score(json.dumps(CLIPS[0]))
        self.assertEqual(len(clips), 1)
        self.assertTrue(any("single clip object" in n for n in diag["notes"]))

    def test_numeric_keyed_map_of_clips(self):
        clips, diag, _ = score(json.dumps({"0": CLIPS[0], "1": CLIPS[1]}))
        self.assertEqual(len(clips), 2)
        self.assertTrue(any("map of clips" in n for n in diag["notes"]))

    # ── formatting noise ────────────────────────────────────────────────

    def test_markdown_fence(self):
        clips, diag, _ = score("```json\n" + json.dumps(CLIPS) + "\n```")
        self.assertEqual(len(clips), 2)
        self.assertTrue(any("fence" in n for n in diag["notes"]))

    def test_prose_preamble(self):
        clips, _, _ = score("Here are the clip candidates I found:\n\n"
                            + json.dumps(CLIPS))
        self.assertEqual(len(clips), 2)

    def test_truncated_mid_array_is_repaired(self):
        full = json.dumps(CLIPS)
        raw = full[:full.rindex("}, {") + 2]      # cut in the middle of item 2
        clips, diag, _ = score(raw, done_reason="length")
        self.assertEqual(len(clips), 1)
        self.assertTrue(any("truncated" in n for n in diag["notes"]))

    def test_empty_response_string(self):
        clips, diag, _ = score("")
        self.assertEqual(clips, [])
        self.assertTrue(any("empty response" in n for n in diag["notes"]))

    def test_unparseable_garbage(self):
        clips, diag, _ = score("I could not find any clips, sorry!")
        self.assertEqual(clips, [])
        self.assertEqual(diag["status"], "parse_failed")
        self.assertTrue(any("no parseable JSON" in n for n in diag["notes"]))
        self.assertIn("could not find", diag["raw_preview"])

    # ── item-level coercion ─────────────────────────────────────────────

    def test_hhmmss_timestamps(self):
        self.assertEqual(cf.to_seconds("02:00"), 120.0)
        self.assertEqual(cf.to_seconds("01:02:30"), 3750.0)
        clips, diag, _ = score(json.dumps([{
            "start": "02:00", "end": "01:02:30", "category": "numbers",
            "hook_line": "x", "why": "y", "strength": 8}]))
        self.assertEqual(clips[0]["start"], 120.0)
        # 3750s parsed fine, then met the ceiling.
        self.assertEqual(clips[0]["end"], 120.0 + cf.MAX_CLIP_SECONDS)
        self.assertTrue(any("ceiling" in n for n in diag["notes"]))

    def test_bracketed_second_strings(self):
        clips, _, _ = score(json.dumps([{
            "start": "[120.5s]", "end": "155s", "category": "numbers",
            "hook_line": "x", "why": "y"}]))
        self.assertEqual(clips[0]["start"], 120.5)
        self.assertEqual(clips[0]["end"], 155.0)

    def test_missing_end_defaults_and_is_noted(self):
        clips, diag, _ = score(json.dumps([{
            "start": 10, "category": "hot_take", "hook_line": "x", "why": "y",
            "strength": 8}]))
        self.assertEqual(clips[0]["end"], 10 + cf.TARGET_CLIP_SECONDS[0])
        self.assertTrue(any("no usable end" in n for n in diag["notes"]))

    def test_alternate_field_names(self):
        clips, _, _ = score(json.dumps([{
            "start_time": 10, "end_time": 40, "type": "numbers",
            "quote": "half a million", "reason": "big number"}]))
        self.assertEqual(clips[0]["category"], "numbers")
        self.assertEqual(clips[0]["hook_line"], "half a million")

    def test_junk_items_dropped_with_a_reason(self):
        clips, diag, _ = score(json.dumps(["just a string", CLIPS[0],
                                           {"why": "no timestamps here"}]))
        self.assertEqual(len(clips), 1)
        joined = " ".join(diag["notes"])
        self.assertIn("dropped a str item", joined)
        self.assertIn("no usable start", joined)

    def test_sort_survives_string_timestamps(self):
        """The old code did m.get('start', 0) then sorted - strings crashed it."""
        clips, _, _ = score(json.dumps([
            {"start": "02:00", "end": "02:30", "hook_line": "b", "category": "x", "why": ""},
            {"start": 5, "end": 30, "hook_line": "a", "category": "x", "why": ""}]))
        clips.sort(key=lambda m: m["start"])
        self.assertEqual([c["hook_line"] for c in clips], ["a", "b"])

    # ── a genuinely empty chunk stays distinguishable from a failure ────

    def test_genuine_empty_is_status_empty_not_parse_failed(self):
        clips, diag, _ = score('{"clips": []}')
        self.assertEqual(clips, [])
        self.assertEqual(diag["status"], "empty")

    def test_context_overflow_is_warned_before_the_call(self):
        class Small(Args):
            num_ctx = 256
        cf._client = FakeClient(json.dumps(CLIPS))
        cf._think_supported = True
        _, diag = cf.find_moments(CHUNK, 1, Small(), None)
        self.assertTrue(any("truncates the FRONT" in n for n in diag["notes"]))


class TestClipWindows(unittest.TestCase):
    """Problem 1: a window must carry setup and payoff, not just the line."""

    def tearDown(self):
        cf._client = None

    def clip(self, start, end, strength=9):
        return {"start": start, "end": end, "category": "numbers",
                "hook_line": "x", "why": "y", "strength": strength}

    def test_short_window_is_padded_to_the_floor(self):
        clips, diag, _ = score(json.dumps([self.clip(100, 105)]))
        self.assertGreaterEqual(clips[0]["duration"], cf.MIN_CLIP_SECONDS)
        self.assertLess(clips[0]["start"], 100)     # gained lead-in
        self.assertGreater(clips[0]["end"], 105)    # gained payoff
        self.assertTrue(any("padded clip" in n for n in diag["notes"]))

    def test_padding_favours_lead_in(self):
        """start = where the setup begins, so the back gets the bigger share."""
        clips, _, _ = score(json.dumps([self.clip(100, 105)]))
        gained_back = 100 - clips[0]["start"]
        gained_forward = clips[0]["end"] - 105
        self.assertGreater(gained_back, gained_forward)

    def test_padding_stops_at_the_chunk_start(self):
        clips, _, _ = score(json.dumps([self.clip(0, 5)]))
        self.assertEqual(clips[0]["start"], 0.0)
        self.assertGreaterEqual(clips[0]["duration"], cf.MIN_CLIP_SECONDS)

    def test_padding_stops_at_the_chunk_end(self):
        chunk = make_chunk(0, 600)
        end = chunk[-1]["end"]
        clips, _, _ = score(json.dumps([self.clip(end - 5, end)]), chunk=chunk)
        self.assertLessEqual(clips[0]["end"], end)
        self.assertGreaterEqual(clips[0]["duration"], cf.MIN_CLIP_SECONDS)

    def test_padding_never_reaches_into_a_neighbour(self):
        clips, _, _ = score(json.dumps([self.clip(100, 105),
                                        self.clip(130, 135, strength=8)]))
        clips.sort(key=lambda c: c["start"])
        self.assertEqual(len(clips), 2)
        self.assertLessEqual(clips[0]["end"], clips[1]["start"])
        for c in clips:
            self.assertGreaterEqual(c["duration"], cf.MIN_CLIP_SECONDS)

    def test_close_neighbours_still_both_reach_the_floor(self):
        """Padding takes more lead-in when the forward side is blocked, so a
        tight pair can still both clear the floor without colliding."""
        clips, _, _ = score(json.dumps([self.clip(100, 103),
                                        self.clip(108, 111, strength=8)]))
        clips.sort(key=lambda c: c["start"])
        self.assertLessEqual(clips[0]["end"], clips[1]["start"])
        for c in clips:
            self.assertGreaterEqual(c["duration"], cf.MIN_CLIP_SECONDS)

    def test_window_pinned_by_the_chunk_reports_what_it_could_not_reach(self):
        """A chunk shorter than the floor cannot produce a full window. The
        clip is still returned - but it says so rather than lying."""
        short = make_chunk(0, 12)
        clips, diag, _ = score(json.dumps([self.clip(2, 5)]), chunk=short)
        self.assertLess(clips[0]["duration"], cf.MIN_CLIP_SECONDS)
        self.assertLessEqual(clips[0]["end"], short[-1]["end"])
        self.assertEqual(clips[0]["start"], 0.0)
        self.assertTrue(any("could only reach" in n for n in diag["notes"]))

    def test_window_never_crosses_into_the_next_chunk(self):
        chunk = make_chunk(600, 1200)
        clips, _, _ = score(json.dumps([self.clip(1195, 1198)]), chunk=chunk)
        self.assertLessEqual(clips[0]["end"], chunk[-1]["end"])
        self.assertGreaterEqual(clips[0]["start"], chunk[0]["start"])

    def test_overlong_window_is_trimmed_to_the_ceiling(self):
        clips, diag, _ = score(json.dumps([self.clip(100, 400)]))
        self.assertEqual(clips[0]["duration"], float(cf.MAX_CLIP_SECONDS))
        self.assertEqual(clips[0]["start"], 100)   # lead-in kept, tail cut
        self.assertTrue(any("trimmed" in n for n in diag["notes"]))

    def test_windows_already_in_target_range_are_left_alone(self):
        clips, _, _ = score(json.dumps([self.clip(100, 135)]))
        self.assertEqual((clips[0]["start"], clips[0]["end"]), (100, 135))

    def test_excerpt_comes_from_the_word_timings(self):
        clips, _, _ = score(json.dumps([self.clip(120, 150)]))
        excerpt = clips[0]["transcript_excerpt"]
        # CHUNK puts word wN at N*0.5s, so [120, 150) is w240..w299.
        self.assertTrue(excerpt.startswith("w240 w241"))
        self.assertTrue(excerpt.endswith("w299"))
        self.assertNotIn("w239", excerpt.split())
        self.assertNotIn("w300", excerpt.split())

    def test_excerpt_tracks_the_padded_window_not_the_original(self):
        clips, _, _ = score(json.dumps([self.clip(100, 105)]))
        words = clips[0]["transcript_excerpt"].split()
        first = int(words[0][1:]) * 0.5
        last = int(words[-1][1:]) * 0.5
        self.assertLessEqual(first, clips[0]["start"] + 0.5)
        self.assertGreaterEqual(last, clips[0]["end"] - 1.0)

    def test_timestamp_outside_the_chunk_is_flagged(self):
        clips, diag, _ = score(json.dumps([self.clip(5000, 5030)]))
        self.assertEqual(clips[0]["transcript_excerpt"], "")
        self.assertTrue(any("covers no transcript words" in n
                            for n in diag["notes"]))

    def test_excerpt_is_never_asked_of_the_model(self):
        """It is reconstructed locally, so a hallucinated one is overwritten."""
        item = self.clip(120, 150)
        item["transcript_excerpt"] = "text the model made up"
        clips, _, _ = score(json.dumps([item]))
        self.assertNotIn("made up", clips[0]["transcript_excerpt"])
        self.assertTrue(clips[0]["transcript_excerpt"].startswith("w240"))


class TestSelectivity(unittest.TestCase):
    """Problem 2: fewer, stronger candidates."""

    def tearDown(self):
        cf._client = None

    def test_strength_is_carried_through(self):
        clips, _, _ = score(json.dumps(CLIPS))
        self.assertEqual([c["strength"] for c in clips], [9, 8])

    def test_per_chunk_cap_keeps_the_strongest(self):
        items = [{"start": 100 + i * 60, "end": 130 + i * 60, "strength": s,
                  "category": "numbers", "hook_line": f"h{s}", "why": "y"}
                 for i, s in enumerate([4, 9, 6, 8, 2])]
        clips, diag, _ = score(json.dumps(items))
        self.assertEqual(len(clips), cf.MAX_CANDIDATES_PER_CHUNK)
        self.assertEqual(sorted((c["strength"] for c in clips), reverse=True),
                         [9, 8, 6])
        self.assertTrue(any("kept the 3 strongest" in n for n in diag["notes"]))

    def test_missing_strength_defaults_and_says_so(self):
        clips, diag, _ = score(json.dumps([{
            "start": 100, "end": 130, "category": "numbers",
            "hook_line": "x", "why": "y"}]))
        self.assertEqual(clips[0]["strength"], cf.DEFAULT_STRENGTH)
        self.assertTrue(any("no strength rating" in n for n in diag["notes"]))

    def test_strength_as_fraction_string(self):
        notes = []
        self.assertEqual(cf.to_strength("8/10", notes), 8)

    def test_strength_as_zero_to_one_confidence(self):
        notes = []
        self.assertEqual(cf.to_strength(0.9, notes), 9)
        self.assertTrue(any("0-1 confidence" in n for n in notes))

    def test_strength_out_of_range_is_clamped(self):
        notes = []
        self.assertEqual(cf.to_strength(47, notes), 10)
        self.assertTrue(any("clamped" in n for n in notes))

    def test_unreadable_strength_is_reported(self):
        notes = []
        self.assertIsNone(cf.to_strength("very high", notes))
        self.assertTrue(any("unreadable strength" in n for n in notes))

    def test_prompt_states_the_window_and_selection_rules(self):
        prompt = cf.build_prompt("[0.0s] hello")
        for expected in ("SETUP begins", "after the PAYOFF",
                         f"{cf.TARGET_CLIP_SECONDS[0]}-{cf.TARGET_CLIP_SECONDS[1]} seconds",
                         f"never shorter than\n  {cf.MIN_CLIP_SECONDS} seconds",
                         f"never longer than {cf.MAX_CLIP_SECONDS}",
                         "When in doubt, leave it out",
                         f"at most {cf.MAX_CANDIDATES_PER_CHUNK}".lower()):
            self.assertIn(expected.lower(), prompt.lower(), expected)

    def test_criteria_names_concrete_exclusions(self):
        for expected in ("REJECT", "routine business detail", "already hold",
                         "administrative", "Generic advice"):
            self.assertIn(expected, cf.CLIP_CRITERIA)

    def test_schema_requires_strength_but_not_the_excerpt(self):
        item = cf.CLIP_SCHEMA["properties"]["clips"]["items"]
        self.assertIn("strength", item["required"])
        self.assertNotIn("transcript_excerpt", item["properties"])


class TestChunkingAndTranscript(unittest.TestCase):

    def test_words_to_timestamped_text_uses_real_line_starts(self):
        words = [{"word": f" w{i}", "start": float(i), "end": i + 0.5}
                 for i in range(30)]
        out = cf.words_to_timestamped_text(words, words_per_line=15)
        lines = out.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("[0.0s]"))
        self.assertTrue(lines[1].startswith("[15.0s]"))

    def test_chunking_respects_the_time_box(self):
        words = [{"word": " w", "start": float(i * 10), "end": i * 10 + 1}
                 for i in range(200)]
        chunks = cf.chunk_words(words, chunk_seconds=600)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(c[-1]["start"] - c[0]["start"], 610)

    def test_to_seconds_rejects_nonsense(self):
        for bad in (None, "", "soon", "abc", True, {}):
            self.assertIsNone(cf.to_seconds(bad))


class SequenceClient:
    """Returns a different response per call, cycling if it runs out."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        raw = self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]
        return fake_resp(raw)


def chunk_clips(offset, strengths):
    """Clips sitting inside the chunk that starts at `offset`."""
    return json.dumps({"clips": [
        {"start": offset + 60 + i * 120, "end": offset + 95 + i * 120,
         "category": "numbers", "hook_line": f"line {offset}-{s}",
         "why": "y", "strength": s} for i, s in enumerate(strengths)]})


class TestEndToEnd(unittest.TestCase):
    """Full main() over a cached transcript with Ollama stubbed out."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.video = os.path.join(self.dir, "vid1.mp4")
        open(self.video, "wb").close()
        base = os.path.splitext(self.video)[0]
        words = [{"word": f" w{i}", "start": float(i * 2), "end": i * 2 + 1.5}
                 for i in range(900)]                      # 30 min of "speech"
        segments = [{"start": float(i * 20), "end": i * 20 + 19.0,
                     "text": f"segment {i} text"} for i in range(90)]
        with open(base + "_transcript.json", "w", encoding="utf-8") as f:
            json.dump({"source": self.video, "meta": {"duration": 1800,
                       "language": "en"}, "segments": segments,
                       "words": words}, f)
        self.base = base

    def tearDown(self):
        cf._client = None
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_full_run_writes_every_output(self):
        cf._client = SequenceClient([
            "<think>scanning...</think>\n" + chunk_clips(0, [9, 8]),
            chunk_clips(600, [10]),
            chunk_clips(1200, [7]),
        ])
        cf._think_supported = True
        rc = cf.main([self.video, "--debug"])
        self.assertEqual(rc, 0)

        with open(self.base + "_clip_candidates.json", encoding="utf-8") as f:
            cands = json.load(f)
        self.assertEqual(len(cands), 4)
        # Strongest first, so a manual read-through starts with the best.
        self.assertEqual([c["strength"] for c in cands], [10, 9, 8, 7])
        for c in cands:
            self.assertGreaterEqual(c["duration"], cf.MIN_CLIP_SECONDS)
            self.assertLessEqual(c["duration"], cf.MAX_CLIP_SECONDS)
            self.assertTrue(c["transcript_excerpt"],
                            "every kept clip needs a readable excerpt")

        with open(self.base + "_clip_candidates_all.json", encoding="utf-8") as f:
            everything = json.load(f)
        self.assertEqual(len(everything), 4)

        with open(self.base + "_clip_report.json", encoding="utf-8") as f:
            report = json.load(f)
        self.assertEqual(report["total_candidates"], 4)
        self.assertEqual(report["scored_before_filter"], 4)
        self.assertEqual(report["strength_histogram"],
                         {"10": 1, "9": 1, "8": 1, "7": 1})
        self.assertEqual([c["status"] for c in report["chunks"]], ["ok"] * 3)

        with open(self.base + "_transcript.txt", encoding="utf-8") as f:
            txt = f.read()
        self.assertIn("[00:00:00] segment 0 text", txt)
        self.assertIn("[00:29:40] segment 89 text", txt)

        # Raw dumps exist for every chunk, written before any parsing.
        dumps = {}
        for i in (1, 2, 3):
            with open(os.path.join(self.base + "_debug", f"chunk_{i:02d}_raw.txt"),
                      encoding="utf-8") as f:
                dumps[i] = f.read()
            self.assertIn("response field (RAW, pre-parse)", dumps[i])
            self.assertTrue(os.path.exists(os.path.join(
                self.base + "_debug", f"chunk_{i:02d}_prompt.txt")))
        # Chunk 1's reasoning is preserved verbatim, un-stripped, in the dump.
        self.assertIn("<think>scanning...</think>", dumps[1])

    def test_debug_dump_happens_even_when_parsing_succeeds(self):
        cf._client = FakeClient(json.dumps(CLIPS))
        cf._think_supported = True
        cf.main([self.video, "--debug", "--limit-chunks", "1"])
        self.assertTrue(os.path.exists(os.path.join(
            self.base + "_debug", "chunk_01_raw.txt")))

    def test_all_chunks_empty_still_reports_why(self):
        cf._client = FakeClient("{}")
        cf._think_supported = True
        rc = cf.main([self.video])
        self.assertEqual(rc, 0)
        with open(self.base + "_clip_report.json", encoding="utf-8") as f:
            report = json.load(f)
        self.assertEqual(report["total_candidates"], 0)
        for chunk in report["chunks"]:
            self.assertEqual(chunk["status"], "empty")
            self.assertTrue(any("empty object" in n for n in chunk["notes"]))

    def test_transcript_only_skips_scoring(self):
        cf._client = FakeClient("should not be called")
        cf._think_supported = True
        rc = cf.main([self.video, "--transcript-only"])
        self.assertEqual(rc, 0)
        self.assertEqual(cf._client.calls, [])
        self.assertTrue(os.path.exists(self.base + "_transcript.txt"))
        self.assertFalse(os.path.exists(self.base + "_clip_candidates.json"))

    def test_weak_candidates_are_filtered_out_but_still_recorded(self):
        cf._client = SequenceClient([chunk_clips(0, [9, 5, 3]),
                                     chunk_clips(600, [4]),
                                     chunk_clips(1200, [8])])
        cf._think_supported = True
        cf.main([self.video])

        with open(self.base + "_clip_candidates.json", encoding="utf-8") as f:
            kept = json.load(f)
        self.assertEqual([c["strength"] for c in kept], [9, 8])

        # Nothing is lost - the bar can be retuned against this file.
        with open(self.base + "_clip_candidates_all.json", encoding="utf-8") as f:
            everything = json.load(f)
        self.assertEqual([c["strength"] for c in everything], [9, 8, 5, 4, 3])

        with open(self.base + "_clip_report.json", encoding="utf-8") as f:
            report = json.load(f)
        self.assertEqual(report["total_candidates"], 2)
        self.assertEqual(report["scored_before_filter"], 5)
        self.assertEqual(report["dropped_below_min_strength"], 3)
        self.assertEqual(report["options"]["min_strength"], cf.MIN_STRENGTH)

    def test_min_strength_is_tunable_from_the_command_line(self):
        responses = [chunk_clips(0, [9, 5, 3]), chunk_clips(600, [4]),
                     chunk_clips(1200, [8])]
        cf._client = SequenceClient(responses)
        cf._think_supported = True
        cf.main([self.video, "--min-strength", "4"])
        with open(self.base + "_clip_candidates.json", encoding="utf-8") as f:
            self.assertEqual([c["strength"] for c in json.load(f)], [9, 8, 5, 4])

    def test_top_caps_the_shortlist(self):
        cf._client = SequenceClient([chunk_clips(0, [9, 8]),
                                     chunk_clips(600, [10]),
                                     chunk_clips(1200, [7])])
        cf._think_supported = True
        cf.main([self.video, "--top", "2"])
        with open(self.base + "_clip_candidates.json", encoding="utf-8") as f:
            self.assertEqual([c["strength"] for c in json.load(f)], [10, 9])
        with open(self.base + "_clip_report.json", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["dropped_by_top"], 2)

    def test_per_chunk_cap_applies_across_the_whole_run(self):
        cf._client = SequenceClient([chunk_clips(0, [9, 8, 7, 6, 10])])
        cf._think_supported = True
        cf.main([self.video, "--limit-chunks", "1"])
        with open(self.base + "_clip_candidates.json", encoding="utf-8") as f:
            self.assertEqual([c["strength"] for c in json.load(f)], [10, 9, 8])

    def test_everything_filtered_out_is_reported_not_silent(self):
        cf._client = FakeClient(chunk_clips(0, [2]))
        cf._think_supported = True
        cf.main([self.video, "--limit-chunks", "1"])
        with open(self.base + "_clip_candidates.json", encoding="utf-8") as f:
            self.assertEqual(json.load(f), [])
        with open(self.base + "_clip_report.json", encoding="utf-8") as f:
            report = json.load(f)
        self.assertEqual(report["scored_before_filter"], 1)
        self.assertEqual(report["dropped_below_min_strength"], 1)

    def test_missing_file_exits_nonzero(self):
        self.assertEqual(cf.main([os.path.join(self.dir, "nope.mp4")]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
