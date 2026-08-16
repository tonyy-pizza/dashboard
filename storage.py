"""Atomic file writes shared by the dashboard-owned stores.

Same tmp-file + os.replace dance collector.py uses for cache.json: a reader
(or a crash mid-write) never sees a half-written file. The stores below are
written by the widget itself rather than the collector, so nothing watches
them — but a truncated todo.json would still lose the list.
"""

import json
import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_json(path: Path, default):
    """Parse `path`, falling back to `default` for a missing or corrupt file.

    A corrupt file is left on disk untouched — the caller carries on with the
    default, and the next save overwrites it.
    """
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
