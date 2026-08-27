#!/usr/bin/env python3
"""
Clip Moment Finder - FOMO x ClipFarm brief (local Ollama version)
------------------------------------------------------------------
Transcribes a podcast/interview file locally with faster-whisper, then asks
a local Ollama model to flag clip-worthy moments matching the campaign's
"what to clip" criteria, with timestamps ready to hand off to a cutting step.

Setup:
    py -m pip install ollama faster-whisper
    ollama pull qwen3:8b        (skip if already pulled)
    (make sure Ollama is running - it usually auto-starts on Windows)

Usage:
    py cf.py path\\to\\episode.mp4
    py cf.py path\\to\\episode.mp4 --debug          (dump raw model output)
    py cf.py path\\to\\episode.mp4 --limit-chunks 2 (score only 2 chunks)
    py cf.py path\\to\\episode.mp4 --transcript-only

Outputs:
    <episode>_clip_candidates.json - sorted list of candidate clips:
        {"start": sec, "end": sec, "category": ..., "hook_line": ..., "why": ...}
    <episode>_transcript.txt       - full readable transcript, [HH:MM:SS] per line
    <episode>_transcript.json      - full transcript: segments + word timings
    <episode>_clip_report.json     - per-chunk diagnostics (why a chunk was empty)
    <episode>_debug/               - raw prompts + raw model responses (--debug)

The transcript JSON doubles as a cache: later runs reuse it instead of
re-transcribing, so you can iterate on the scoring step cheaply. Pass
--retranscribe to force whisper to run again.
"""

import argparse
import gc
import json
import os
import re
import sys

# ── CONFIG ───────────────────────────────────────────────────────────────

OLLAMA_MODEL = "qwen3:8b"        # or "llama3.1:8b"
# Explicit host, bypassing OLLAMA_HOST env var (yours is set to 0.0.0.0,
# which is a bind address, not a valid address to connect *to*).
OLLAMA_HOST = "http://127.0.0.1:11434"
WHISPER_SIZE = "medium"          # tiny/base/small/medium/large-v3
WHISPER_DEVICE = "cuda"          # switch to "cpu" if CUDA/cuDNN errors out
WHISPER_COMPUTE = "float16"      # use "int8" if WHISPER_DEVICE == "cpu"
CHUNK_SECONDS = 600              # ~10 min per model call, keeps prompts sane

# A 10-minute chunk of speech is roughly 1,500 words; with the [X.Xs] tags
# that lands around 3-4k tokens. Ollama's default num_ctx is 4096 and it
# silently truncates the *front* of an over-long prompt - which would eat the
# criteria and most of the transcript, leaving the model nothing to score.
# Set it explicitly rather than inheriting the default.
NUM_CTX = 8192
NUM_PREDICT = 2048

CLIP_CRITERIA = """
You are screening a podcast transcript for short-form clip candidates for a
trading/crypto founder-interview campaign. Flag moments that match ANY of:

1. WILD TRADING STORIES - biggest wins, worst losses, moments that make
   someone rewind and replay.
2. THE NUMBERS - six/seven-figure amounts said out loud, with reaction.
3. HOT TAKES - spicy opinions on crypto/trading/the industry, debate bait.
4. FOUNDER STORY - how the company got built, behind-the-scenes, vision.

Each candidate clip should run 15-60 seconds and needs a punchy, quotable
line right at the start that could work as a 2-second hook.
Skip anything bland, generic, or that doesn't clearly fit one category.
"""

CATEGORIES = ["trading_story", "numbers", "hot_take", "founder_story"]

# Used with --schema. Ollama's structured-output mode constrains the *shape*,
# not just the syntax; plain format="json" only guarantees parseable JSON.
CLIP_SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "hook_line": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["start", "end", "category", "hook_line", "why"],
            },
        }
    },
    "required": ["clips"],
}

WRAPPER_KEYS = ("clips", "moments", "results", "candidates", "items",
                "clip_candidates", "output", "data")

_client = None
_think_supported = True          # degrades to False if client/server rejects it


# ── SMALL UTILITIES ──────────────────────────────────────────────────────

def say(msg=""):
    """print() that survives a cp1252 Windows console meeting whisper output."""
    try:
        print(msg)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        print(str(msg).encode(enc, errors="replace").decode(enc))


def hms(seconds):
    seconds = max(0.0, float(seconds))
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def resp_get(resp, key, default=None):
    """Ollama's client returns a dict on old versions, a pydantic model on new."""
    if isinstance(resp, dict):
        return resp.get(key, default)
    try:
        return resp[key]
    except Exception:
        return getattr(resp, key, default)


def est_tokens(text):
    """Rough token estimate. Timestamped transcript is punctuation-heavy, so
    ~3.2 chars/token is closer than the usual 4."""
    return int(len(text) / 3.2)


def get_client():
    global _client
    if _client is None:
        from ollama import Client
        _client = Client(host=OLLAMA_HOST)
    return _client


# ── TRANSCRIPTION ────────────────────────────────────────────────────────

def transcribe(path: str):
    """Return (words, segments, info) - words carry per-word timings, segments
    carry the readable text."""
    from faster_whisper import WhisperModel

    say(f"Transcribing {path} (this can take a while for long episodes)...")
    model = WhisperModel(WHISPER_SIZE, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
    seg_iter, info = model.transcribe(path, word_timestamps=True)

    words = []
    segments = []
    for seg in seg_iter:
        segments.append({
            "start": round(seg.start, 2),
            "end": round(seg.end, 2),
            "text": (seg.text or "").strip(),
        })
        # word_timestamps can come back None for a segment (music, silence).
        for w in (seg.words or []):
            words.append({
                "word": w.word,
                "start": round(w.start, 2),
                "end": round(w.end, 2),
            })
        if len(segments) % 25 == 0:
            say(f"  ...{hms(seg.end)} transcribed")

    meta = {
        "language": getattr(info, "language", None),
        "language_probability": getattr(info, "language_probability", None),
        "duration": getattr(info, "duration", None),
        "whisper_size": WHISPER_SIZE,
        "whisper_device": WHISPER_DEVICE,
        "whisper_compute": WHISPER_COMPUTE,
    }

    # Whisper holds a chunk of VRAM; hand it back before Ollama loads an 8B.
    del model
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass

    return words, segments, meta


def write_transcript(path, words, segments, meta):
    """Write the full transcript as both a readable .txt and a machine .json.
    The .json is also the cache that lets later runs skip whisper."""
    base = os.path.splitext(path)[0]
    txt_path = base + "_transcript.txt"
    json_path = base + "_transcript.json"

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"# Transcript: {os.path.basename(path)}\n")
        if meta.get("duration"):
            f.write(f"# Duration: {hms(meta['duration'])}\n")
        if meta.get("language"):
            f.write(f"# Language: {meta['language']}\n")
        f.write("\n")
        for seg in segments:
            f.write(f"[{hms(seg['start'])}] {seg['text']}\n")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "source": os.path.abspath(path),
            "meta": meta,
            "segments": segments,
            "words": words,
        }, f, indent=2, ensure_ascii=False)

    return txt_path, json_path


def load_cached_transcript(path):
    json_path = os.path.splitext(path)[0] + "_transcript.json"
    if not os.path.exists(json_path):
        return None
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        words = data.get("words") or []
        if not words:
            return None
        return words, data.get("segments") or [], data.get("meta") or {}
    except (OSError, json.JSONDecodeError) as e:
        say(f"  (ignoring unreadable transcript cache: {e})")
        return None


def chunk_words(words, chunk_seconds=CHUNK_SECONDS):
    """Break the word list into time-boxed chunks so each model call stays small."""
    chunks = []
    current = []
    chunk_start = words[0]["start"] if words else 0
    for w in words:
        if w["start"] - chunk_start > chunk_seconds and current:
            chunks.append(current)
            current = []
            chunk_start = w["start"]
        current.append(w)
    if current:
        chunks.append(current)
    return chunks


def words_to_timestamped_text(words, words_per_line=15):
    """Render a word list as readable lines, each tagged with its start time."""
    lines = []
    line = []
    line_start = words[0]["start"] if words else 0
    for w in words:
        if not line:
            line_start = w["start"]
        line.append(w["word"])
        if len(line) >= words_per_line:
            lines.append(f"[{line_start:.1f}s] " + "".join(line).strip())
            line = []
    if line:
        lines.append(f"[{line_start:.1f}s] " + "".join(line).strip())
    return "\n".join(lines)


# ── RESPONSE PARSING ─────────────────────────────────────────────────────
#
# Everything below assumes the model output is hostile: thinking tags, markdown
# fences, prose preambles, an object where an array was asked for, timestamps
# as "01:23" strings, a response cut off mid-array. Each rescue is recorded in
# `notes` so an empty chunk can always be explained.

THINK_BLOCK = re.compile(r"<(think|thinking)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
THINK_OPEN = re.compile(r"<(think|thinking)\b[^>]*>", re.IGNORECASE)
FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def strip_wrappers(text, notes):
    """Remove <think> blocks and markdown fences. Returns cleaned text."""
    cleaned, n = THINK_BLOCK.subn("", text)
    if n:
        notes.append(f"stripped {n} <think> block(s)")

    open_tag = THINK_OPEN.search(cleaned)
    if open_tag:
        # An unclosed <think> means generation ran out of budget mid-reasoning.
        notes.append("unclosed <think> tag - model never finished reasoning "
                     "(raise NUM_PREDICT or disable thinking)")
        cleaned = cleaned[:open_tag.start()] + cleaned[open_tag.end():]

    fenced = FENCE.search(cleaned)
    if fenced:
        notes.append("unwrapped ```json fence")
        cleaned = fenced.group(1)

    return cleaned.strip()


def iter_json_candidates(text):
    """Yield every balanced {...} / [...] span in `text`, outermost first.

    Scans with a depth counter that respects string literals, so braces inside
    quoted prose (or inside leftover reasoning) don't derail it. This is why we
    don't just do find('{') / rfind('}') - reasoning text is full of braces.
    """
    i, n = 0, len(text)
    while i < n:
        if text[i] not in "{[":
            i += 1
            continue
        opener = text[i]
        closer = "}" if opener == "{" else "]"
        depth, in_str, esc = 0, False, False
        j = i
        while j < n:
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    yield text[i:j + 1]
                    break
            j += 1
        else:
            yield text[i:]          # unbalanced: ran off the end (truncated)
            return
        i = j + 1


def repair_truncated(fragment):
    """Best-effort close of a response that was cut off mid-array."""
    if not fragment:
        return None
    # Drop the trailing partial item, then close whatever is still open.
    for cut in (fragment.rfind("}"), fragment.rfind("]"), fragment.rfind('"')):
        if cut == -1:
            continue
        head = fragment[:cut + 1]
        stack, in_str, esc = [], False, False
        for c in head:
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c in "{[":
                stack.append("}" if c == "{" else "]")
            elif c in "}]" and stack:
                stack.pop()
        if in_str:
            continue
        patched = head.rstrip().rstrip(",") + "".join(reversed(stack))
        try:
            return json.loads(patched)
        except json.JSONDecodeError:
            continue
    return None


def parse_json_payload(text, notes):
    """Turn raw model text into a Python object, or None."""
    if not text.strip():
        notes.append("model returned an empty response")
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    best = None
    for cand in iter_json_candidates(text):
        try:
            parsed = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if "extracted embedded JSON" not in notes:
            notes.append("extracted embedded JSON from surrounding text")
        # Prefer a non-empty payload over the first empty {} we stumble across.
        if isinstance(parsed, list) and parsed:
            return parsed
        if isinstance(parsed, dict) and parsed and best is None:
            best = parsed
        elif best is None:
            best = parsed
    if best is not None:
        return best

    repaired = repair_truncated(text)
    if repaired is not None:
        notes.append("response was truncated mid-JSON; repaired by closing "
                     "open brackets (raise NUM_PREDICT)")
        return repaired

    notes.append("no parseable JSON anywhere in the response")
    return None


def looks_like_clip(d):
    if not isinstance(d, dict):
        return False
    keys = {k.lower() for k in d.keys()}
    return bool(keys & {"start", "start_time", "start_sec", "hook_line", "hook", "quote"})


def normalize_payload(data, notes):
    """Coerce whatever came back into a list of clip dicts."""
    if data is None:
        return []

    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        notes.append(f"top-level JSON was {type(data).__name__}, expected array or object")
        return []

    if not data:
        notes.append("model returned an empty object {} - it produced no clips "
                     "(check the debug dump: prompt may have been truncated)")
        return []

    for key in WRAPPER_KEYS:
        if isinstance(data.get(key), list):
            if key != "clips":
                notes.append(f"array was wrapped under key '{key}'")
            return data[key]

    if looks_like_clip(data):
        notes.append("model returned a single clip object, not an array; wrapped it")
        return [data]

    values = list(data.values())
    if values and all(looks_like_clip(v) for v in values):
        notes.append(f"model returned a map of clips keyed by {list(data)[:3]}; "
                     "used the values")
        return values

    list_values = [(k, v) for k, v in data.items() if isinstance(v, list)]
    if len(list_values) == 1:
        key, val = list_values[0]
        notes.append(f"array was under unrecognized key '{key}'; used it anyway")
        return val

    notes.append("dict came back with no recognized wrapper key - keys were "
                 f"{sorted(data)[:8]} (nothing dropped silently; see debug dump)")
    return []


TIME_RE = re.compile(r"^\s*\[?\s*(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)\s*\]?\s*$")


def to_seconds(value):
    """Accept 12.5, "12.5", "12.5s", "[12.5s]", "1:23", "01:02:03"."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None

    text = value.strip()
    m = TIME_RE.match(text)
    if m:
        h, mm, ss = m.group(1), m.group(2), m.group(3)
        return int(h or 0) * 3600 + int(mm) * 60 + float(ss)

    text = text.strip("[]").strip()
    if text.endswith("s"):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return None


def first_key(d, *names):
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return None


def coerce_clip(item, notes):
    """Validate one candidate. Returns a clean dict, or None with a note."""
    if not isinstance(item, dict):
        notes.append(f"dropped a {type(item).__name__} item (expected object)")
        return None

    start = to_seconds(first_key(item, "start", "start_time", "start_sec", "from"))
    end = to_seconds(first_key(item, "end", "end_time", "end_sec", "to"))

    if start is None:
        notes.append(f"dropped an item with no usable start: {sorted(item)[:6]}")
        return None
    if end is None or end <= start:
        end = start + 30.0
        notes.append("item had no usable end; defaulted to start + 30s")

    duration = end - start
    if duration > 180:
        notes.append(f"item ran {duration:.0f}s - kept, but it needs trimming")

    category = str(first_key(item, "category", "type", "tag") or "?").strip()
    return {
        "start": round(start, 2),
        "end": round(end, 2),
        "duration": round(end - start, 2),
        "category": category,
        "hook_line": str(first_key(item, "hook_line", "hook", "quote", "line") or "").strip(),
        "why": str(first_key(item, "why", "reason", "rationale") or "").strip(),
    }


# ── OLLAMA SCORING ───────────────────────────────────────────────────────

def build_prompt(transcript_text):
    return f"""{CLIP_CRITERIA}

Transcript (timestamps in seconds, [X.Xs] marks the start of each line):

{transcript_text}

Return ONLY a JSON object of the form {{"clips": [...]}}, no other text, no
markdown fences. Each item in "clips":
{{"start": <seconds, number>, "end": <seconds, number>,
  "category": "trading_story|numbers|hot_take|founder_story",
  "hook_line": "<the most quotable line, verbatim from the transcript>",
  "why": "<one sentence on why this clips well>"}}

If nothing in this chunk qualifies, return {{"clips": []}}.
"""


def call_model(prompt, args, notes):
    """One Ollama call. Disables thinking where supported, degrading quietly
    on older clients/servers that don't know the flag."""
    global _think_supported

    kwargs = {
        "model": args.model,
        "prompt": prompt,
        "format": CLIP_SCHEMA if args.schema else "json",
        "options": {
            "temperature": args.temperature,
            "num_ctx": args.num_ctx,
            "num_predict": args.num_predict,
        },
    }

    want_think_off = not args.think
    if want_think_off and _think_supported:
        try:
            return get_client().generate(think=False, **kwargs)
        except TypeError:
            _think_supported = False
            notes.append("installed ollama client is too old for think=False; "
                         "relying on <think> stripping instead")
        except Exception as e:
            if "think" in str(e).lower():
                _think_supported = False
                notes.append(f"server rejected think=False ({e}); relying on "
                             "<think> stripping instead")
            else:
                raise

    return get_client().generate(**kwargs)


def dump_debug(debug_dir, index, prompt, resp, raw, notes):
    """Write the raw, unparsed response BEFORE anything touches it."""
    os.makedirs(debug_dir, exist_ok=True)
    thinking = resp_get(resp, "thinking") or ""
    done_reason = resp_get(resp, "done_reason")

    with open(os.path.join(debug_dir, f"chunk_{index:02d}_prompt.txt"), "w",
              encoding="utf-8") as f:
        f.write(prompt)

    with open(os.path.join(debug_dir, f"chunk_{index:02d}_raw.txt"), "w",
              encoding="utf-8") as f:
        f.write(f"=== chunk {index} ===\n")
        f.write(f"prompt chars      : {len(prompt)}\n")
        f.write(f"prompt tokens ~   : {est_tokens(prompt)}\n")
        f.write(f"prompt_eval_count : {resp_get(resp, 'prompt_eval_count')}\n")
        f.write(f"eval_count        : {resp_get(resp, 'eval_count')}\n")
        f.write(f"done_reason       : {done_reason}\n")
        f.write(f"separate thinking : {len(thinking)} chars\n")
        f.write(f"response chars    : {len(raw)}\n")
        f.write(f"notes so far      : {notes}\n")
        f.write("\n--- thinking field (Ollama-parsed) ---\n")
        f.write(thinking)
        f.write("\n\n--- response field (RAW, pre-parse) ---\n")
        f.write(raw)
        f.write("\n")


def find_moments(transcript_text, index, args, debug_dir):
    """Score one chunk. Returns (clips, diagnostics)."""
    notes = []
    prompt = build_prompt(transcript_text)

    tokens = est_tokens(prompt)
    if tokens > args.num_ctx * 0.85:
        notes.append(f"prompt is ~{tokens} tokens against num_ctx={args.num_ctx}; "
                     "Ollama truncates the FRONT of an over-long prompt, which "
                     "eats the criteria - raise NUM_CTX or lower CHUNK_SECONDS")
        say(f"    ! prompt ~{tokens} tok vs num_ctx {args.num_ctx} - risk of truncation")

    try:
        resp = call_model(prompt, args, notes)
    except Exception as e:
        notes.append(f"Ollama call failed: {type(e).__name__}: {e}")
        say(f"    ! Ollama call failed: {e}")
        return [], {"chunk": index, "status": "call_failed", "notes": notes}

    raw = resp_get(resp, "response", "") or ""
    thinking = resp_get(resp, "thinking") or ""
    done_reason = resp_get(resp, "done_reason")

    if debug_dir:
        dump_debug(debug_dir, index, prompt, resp, raw, notes)

    if thinking:
        notes.append(f"model emitted {len(thinking)} chars of separate thinking "
                     "(Ollama kept it out of `response`)")
    if done_reason == "length":
        notes.append(f"generation hit the num_predict={args.num_predict} ceiling "
                     "and was cut off mid-output")
        say(f"    ! chunk {index} output was truncated at num_predict")

    cleaned = strip_wrappers(raw, notes)
    data = parse_json_payload(cleaned, notes)
    items = normalize_payload(data, notes)

    clips = []
    for item in items:
        clip = coerce_clip(item, notes)
        if clip:
            clips.append(clip)

    status = "ok" if clips else ("empty" if data is not None else "parse_failed")
    diag = {
        "chunk": index,
        "status": status,
        "clips": len(clips),
        "prompt_chars": len(prompt),
        "prompt_tokens_est": tokens,
        "prompt_eval_count": resp_get(resp, "prompt_eval_count"),
        "eval_count": resp_get(resp, "eval_count"),
        "done_reason": done_reason,
        "response_chars": len(raw),
        "thinking_chars": len(thinking),
        "raw_preview": raw[:300],
        "notes": notes,
    }
    return clips, diag


# ── MAIN ──────────────────────────────────────────────────────────────────

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Find clip-worthy moments in a podcast/interview video.")
    p.add_argument("video", help="path to the episode (mp4, mp3, wav, ...)")
    p.add_argument("--debug", action="store_true",
                   default=os.environ.get("CF_DEBUG", "") not in ("", "0"),
                   help="dump each chunk's raw prompt + unparsed response "
                        "to <episode>_debug/ (or set CF_DEBUG=1)")
    p.add_argument("--model", default=OLLAMA_MODEL)
    p.add_argument("--host", default=OLLAMA_HOST)
    p.add_argument("--chunk-seconds", type=int, default=CHUNK_SECONDS)
    p.add_argument("--num-ctx", type=int, default=NUM_CTX)
    p.add_argument("--num-predict", type=int, default=NUM_PREDICT)
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--think", action="store_true",
                   help="leave the model's thinking mode ON (default: off). "
                        "Use to A/B whether <think> output is the problem.")
    p.add_argument("--schema", action="store_true",
                   help="constrain output with a JSON schema instead of plain "
                        "format=json (needs Ollama >= 0.5)")
    p.add_argument("--limit-chunks", type=int, default=0,
                   help="score only the first N chunks (fast debug loop)")
    p.add_argument("--transcript-only", action="store_true",
                   help="transcribe and write the transcript, skip scoring")
    p.add_argument("--retranscribe", action="store_true",
                   help="ignore the cached transcript and re-run whisper")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    global OLLAMA_HOST
    OLLAMA_HOST = args.host

    path = args.video
    if not os.path.exists(path):
        say(f"File not found: {path}")
        return 1

    base = os.path.splitext(path)[0]

    cached = None if args.retranscribe else load_cached_transcript(path)
    if cached:
        words, segments, meta = cached
        say(f"Reusing cached transcript ({len(words)} words). "
            "Pass --retranscribe to redo it.")
    else:
        words, segments, meta = transcribe(path)

    if not words:
        say("No speech detected - check the file.")
        return 1

    txt_path, json_path = write_transcript(path, words, segments, meta)
    say(f"Transcript: {txt_path}")
    say(f"Transcript: {json_path}")

    if args.transcript_only:
        return 0

    chunks = chunk_words(words, args.chunk_seconds)
    if args.limit_chunks:
        chunks = chunks[:args.limit_chunks]
        say(f"(--limit-chunks {args.limit_chunks}: scoring a subset)")

    debug_dir = base + "_debug" if args.debug else None
    if debug_dir:
        say(f"Debug mode ON - raw model output goes to {debug_dir}")

    say(f"Transcript split into {len(chunks)} chunk(s). Scoring with {args.model}...")

    all_moments = []
    report = []
    for i, chunk in enumerate(chunks, 1):
        say(f"  Chunk {i}/{len(chunks)} ({hms(chunk[0]['start'])}-{hms(chunk[-1]['end'])})...")
        text = words_to_timestamped_text(chunk)
        moments, diag = find_moments(text, i, args, debug_dir)
        all_moments.extend(moments)
        report.append(diag)
        say(f"    -> {len(moments)} candidate(s) [{diag['status']}]")
        for note in diag["notes"]:
            say(f"       note: {note}")

    all_moments.sort(key=lambda m: m["start"])

    out_path = base + "_clip_candidates.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_moments, f, indent=2, ensure_ascii=False)

    report_path = base + "_clip_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({
            "source": os.path.abspath(path),
            "model": args.model,
            "options": {
                "num_ctx": args.num_ctx,
                "num_predict": args.num_predict,
                "temperature": args.temperature,
                "thinking": args.think,
                "schema": args.schema,
                "chunk_seconds": args.chunk_seconds,
            },
            "total_candidates": len(all_moments),
            "chunks": report,
        }, f, indent=2, ensure_ascii=False)

    # An empty result must never again be indistinguishable from a silent failure.
    ok = sum(1 for d in report if d["status"] == "ok")
    empty = sum(1 for d in report if d["status"] == "empty")
    failed = sum(1 for d in report if d["status"] in ("parse_failed", "call_failed"))

    say(f"\nFound {len(all_moments)} candidate moment(s) "
        f"across {len(report)} chunk(s): {ok} produced clips, "
        f"{empty} returned nothing, {failed} failed.")
    if failed:
        say("  ! Some chunks FAILED rather than found nothing. "
            f"Re-run with --debug and read {base}_debug/.")
    elif not all_moments:
        say("  ! Every chunk parsed cleanly and still returned nothing. "
            "That is a model/prompt result, not a parsing bug - "
            f"check {report_path}, then try --schema or a bigger --num-ctx.")
    say(f"Saved: {out_path}")
    say(f"Saved: {report_path}\n")

    for m in all_moments:
        say(f"  [{m['start']:7.1f}s - {m['end']:7.1f}s] "
            f"({m['category']}) {m['hook_line']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
