"""todo.json — the self-edited To Do list.

Dashboard-owned: the widget reads and writes this file directly, no collector
involved. Every mutation is a load → change → atomic save, so an Emacs-side or
hand edit between two dashboard actions is picked up rather than clobbered by
a stale in-memory copy.

Item shape on disk:

    {"items": [
       {"id": "...", "text": "buy milk", "priority": 0,
        "done": false, "order": 3}
    ]}

`priority` is 0–3 (blank / ! / !! / !!!) and `order` is the manual sort
position *within that priority tier* — tiers are re-numbered 0..n-1 on every
save, so the numbers never drift.
"""

import uuid
from dataclasses import dataclass, asdict
from pathlib import Path

from paths import TODO_PATH
from storage import load_json, save_json

MAX_PRIORITY = 3
PRIORITY_MARKS = {0: "", 1: "!", 2: "!!", 3: "!!!"}
PRIORITY_NAMES = {0: "no priority", 1: "!", 2: "!!", 3: "!!!"}


@dataclass
class TodoItem:
    id: str
    text: str
    priority: int = 0
    done: bool = False
    order: int = 0

    @property
    def mark(self) -> str:
        return PRIORITY_MARKS.get(self.priority, "")


def _clamp_priority(value) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(MAX_PRIORITY, value))


class TodoStore:
    def __init__(self, path: Path = TODO_PATH):
        self.path = Path(path)

    # ── read ─────────────────────────────────────────────────────────
    def load(self) -> list:
        raw = load_json(self.path, {})
        items = []
        for i, rec in enumerate(raw.get("items", []) if isinstance(raw, dict) else []):
            if not isinstance(rec, dict):
                continue
            text = str(rec.get("text", "")).strip()
            if not text:
                continue
            items.append(TodoItem(
                id=str(rec.get("id") or uuid.uuid4().hex),
                text=text,
                priority=_clamp_priority(rec.get("priority", 0)),
                done=bool(rec.get("done", False)),
                order=int(rec.get("order", i)) if str(rec.get("order", i)).lstrip("-").isdigit() else i,
            ))
        return self._normalize(items)

    def grouped(self) -> list:
        """[(priority, [items…]), …] — highest tier first, ordered within tier."""
        items = self.load()
        out = []
        for priority in range(MAX_PRIORITY, -1, -1):
            tier = [i for i in items if i.priority == priority]
            if tier:
                out.append((priority, tier))
        return out

    # ── write ────────────────────────────────────────────────────────
    def save(self, items: list) -> list:
        items = self._normalize(items)
        save_json(self.path, {"items": [asdict(i) for i in items]})
        return items

    def add(self, text: str, priority: int = 0) -> list:
        text = str(text).strip()
        if not text:
            return self.load()
        items = self.load()
        priority = _clamp_priority(priority)
        tail = max((i.order for i in items if i.priority == priority), default=-1) + 1
        items.append(TodoItem(id=uuid.uuid4().hex, text=text,
                              priority=priority, done=False, order=tail))
        return self.save(items)

    def delete(self, item_id: str) -> list:
        """Immediate and permanent — no confirmation, no undo, by design."""
        return self.save([i for i in self.load() if i.id != item_id])

    def set_done(self, item_id: str, done: bool) -> list:
        items = self.load()
        for item in items:
            if item.id == item_id:
                item.done = bool(done)
        return self.save(items)

    def set_priority(self, item_id: str, priority: int) -> list:
        items = self.load()
        priority = _clamp_priority(priority)
        for item in items:
            if item.id == item_id and item.priority != priority:
                item.priority = priority
                # Land at the bottom of the tier it moved into.
                item.order = max((i.order for i in items
                                  if i.priority == priority and i.id != item_id),
                                 default=-1) + 1
        return self.save(items)

    def reorder(self, priority: int, ordered_ids: list) -> list:
        """Apply a drag-reorder: `ordered_ids` is the new top-to-bottom order
        of one tier. Ids not in the tier are ignored; tier members missing
        from the list keep their relative order at the bottom."""
        items = self.load()
        priority = _clamp_priority(priority)
        tier = {i.id: i for i in items if i.priority == priority}
        position = 0
        for item_id in ordered_ids:
            item = tier.pop(item_id, None)
            if item is not None:
                item.order = position
                position += 1
        for item in tier.values():
            item.order = position
            position += 1
        return self.save(items)

    # ── internals ────────────────────────────────────────────────────
    @staticmethod
    def _normalize(items: list) -> list:
        """Sort by tier (highest first) then manual order, and renumber each
        tier 0..n-1 so `order` stays dense."""
        items = sorted(items, key=lambda i: (-i.priority, i.order))
        counters = {}
        for item in items:
            item.order = counters.get(item.priority, 0)
            counters[item.priority] = item.order + 1
        return items
