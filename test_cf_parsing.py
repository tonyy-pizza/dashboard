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
    {"start": 120.5, "end": 155.0, "category": "numbers",
     "hook_line": "I lost four hundred grand in a weekend.",
     "why": "Big number, said flat, high replay value."},
    {"start": 402.0, "end": 441.0, "category": "hot_take",
     "hook_line": "Ninety percent of this industry is a marketing funnel.",
     "why": "Debate bait, clean standalone quote."},
]


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


def score(raw, **respkw):
    """Run one chunk through the real pipeline with a stubbed Ollama."""
    client = FakeClient(raw, **respkw)
    cf._client = client
    cf._think_supported = True
    clips, diag = cf.find_moments("[0.0s] pretend transcript", 1, Args(), None)
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
        clips, diag = cf.find_moments("[0.0s] x", 1, Args(), None)
        self.assertEqual(len(clips), 2)
        self.assertTrue(any("too old for think=False" in n for n in diag["notes"]))

    def test_connection_errors_are_not_swallowed_as_think_failures(self):
        class DeadClient(FakeClient):
            def generate(self, **kwargs):
                raise ConnectionError("connection refused")

        cf._client = DeadClient("")
        cf._think_supported = True
        clips, diag = cf.find_moments("[0.0s] x", 1, Args(), None)
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
        clips, _, _ = score(json.dumps([{
            "start": "02:00", "end": "01:02:30", "category": "numbers",
            "hook_line": "x", "why": "y"}]))
        self.assertEqual(clips[0]["start"], 120.0)
        self.assertEqual(clips[0]["end"], 3750.0)

    def test_bracketed_second_strings(self):
        clips, _, _ = score(json.dumps([{
            "start": "[120.5s]", "end": "155s", "category": "numbers",
            "hook_line": "x", "why": "y"}]))
        self.assertEqual(clips[0]["start"], 120.5)
        self.assertEqual(clips[0]["end"], 155.0)

    def test_missing_end_defaults_and_is_noted(self):
        clips, diag, _ = score(json.dumps([{
            "start": 10, "category": "hot_take", "hook_line": "x", "why": "y"}]))
        self.assertEqual(clips[0]["end"], 40.0)
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
        long_text = "[0.0s] " + ("word " * 2000)
        _, diag = cf.find_moments(long_text, 1, Small(), None)
        self.assertTrue(any("truncates the FRONT" in n for n in diag["notes"]))


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

    def test_full_run_writes_all_four_outputs(self):
        cf._client = FakeClient("<think>scanning...</think>\n"
                                + json.dumps({"clips": CLIPS}))
        cf._think_supported = True
        rc = cf.main([self.video, "--debug"])
        self.assertEqual(rc, 0)

        with open(self.base + "_clip_candidates.json", encoding="utf-8") as f:
            cands = json.load(f)
        self.assertEqual(len(cands), 6)                    # 3 chunks x 2 clips
        self.assertEqual(cands, sorted(cands, key=lambda c: c["start"]))

        with open(self.base + "_clip_report.json", encoding="utf-8") as f:
            report = json.load(f)
        self.assertEqual(report["total_candidates"], 6)
        self.assertEqual([c["status"] for c in report["chunks"]], ["ok"] * 3)

        with open(self.base + "_transcript.txt", encoding="utf-8") as f:
            txt = f.read()
        self.assertIn("[00:00:00] segment 0 text", txt)
        self.assertIn("[00:29:40] segment 89 text", txt)

        # Raw dumps exist for every chunk, written before any parsing.
        for i in (1, 2, 3):
            with open(os.path.join(self.base + "_debug", f"chunk_{i:02d}_raw.txt"),
                      encoding="utf-8") as f:
                raw = f.read()
            self.assertIn("<think>scanning...</think>", raw)
            self.assertIn("response field (RAW, pre-parse)", raw)
            self.assertTrue(os.path.exists(os.path.join(
                self.base + "_debug", f"chunk_{i:02d}_prompt.txt")))

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

    def test_missing_file_exits_nonzero(self):
        self.assertEqual(cf.main([os.path.join(self.dir, "nope.mp4")]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
