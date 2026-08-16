import pytest

from todo_store import TodoStore


@pytest.fixture
def store(tmp_path):
    return TodoStore(tmp_path / "todo.json")


def texts(items):
    return [i.text for i in items]


def test_empty_file_reads_as_no_items(store):
    assert store.load() == []
    assert store.grouped() == []


def test_add_defaults_to_blank_priority(store):
    items = store.add("buy milk")
    assert texts(items) == ["buy milk"]
    assert items[0].priority == 0
    assert items[0].mark == ""


def test_items_persist_across_store_instances(store):
    store.add("buy milk")
    assert texts(TodoStore(store.path).load()) == ["buy milk"]


def test_blank_text_is_ignored(store):
    store.add("   ")
    assert store.load() == []


def test_grouped_puts_highest_tier_first(store):
    store.add("plain")
    store.add("urgent", priority=3)
    store.add("soon", priority=1)

    assert [(p, texts(items)) for p, items in store.grouped()] == [
        (3, ["urgent"]), (1, ["soon"]), (0, ["plain"]),
    ]


def test_order_is_scoped_within_a_tier(store):
    store.add("a", priority=2)
    store.add("b", priority=2)
    store.add("c", priority=0)

    by_text = {i.text: i for i in store.load()}
    assert (by_text["a"].order, by_text["b"].order) == (0, 1)
    assert by_text["c"].order == 0        # its own tier restarts at 0


def test_completion_is_separate_from_deletion(store):
    items = store.add("write spec")
    item_id = items[0].id

    items = store.set_done(item_id, True)
    assert items[0].done is True
    assert len(store.load()) == 1         # still visible, never auto-archived

    items = store.delete(item_id)
    assert items == []


def test_delete_only_removes_the_named_item(store):
    store.add("keep")
    target = store.add("drop")[-1]
    assert texts(store.delete(target.id)) == ["keep"]


def test_reorder_within_a_tier(store):
    for text in ("a", "b", "c"):
        store.add(text, priority=1)
    ids = {i.text: i.id for i in store.load()}

    items = store.reorder(1, [ids["c"], ids["a"], ids["b"]])
    assert texts(items) == ["c", "a", "b"]
    assert [i.order for i in items] == [0, 1, 2]


def test_reorder_ignores_ids_from_other_tiers(store):
    store.add("high", priority=3)
    store.add("low a")
    store.add("low b")
    ids = {i.text: i.id for i in store.load()}

    items = store.reorder(0, [ids["low b"], ids["high"], ids["low a"]])
    assert texts(items) == ["high", "low b", "low a"]


def test_changing_priority_moves_to_the_bottom_of_the_new_tier(store):
    store.add("first", priority=2)
    store.add("second", priority=2)
    mover = store.add("mover")[-1]

    items = store.set_priority(mover.id, 2)
    assert texts([i for i in items if i.priority == 2]) == ["first", "second", "mover"]


def test_priority_is_clamped(store):
    items = store.add("wild", priority=99)
    assert items[0].priority == 3


def test_corrupt_file_does_not_raise(tmp_path):
    path = tmp_path / "todo.json"
    path.write_text("{not json", encoding="utf-8")
    store = TodoStore(path)

    assert store.load() == []
    assert texts(store.add("recovered")) == ["recovered"]


def test_hand_edited_file_without_ids_still_loads(tmp_path):
    path = tmp_path / "todo.json"
    path.write_text('{"items": [{"text": "typed by hand"}]}', encoding="utf-8")

    items = TodoStore(path).load()
    assert texts(items) == ["typed by hand"]
    assert items[0].id
