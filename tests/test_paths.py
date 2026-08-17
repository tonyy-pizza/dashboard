"""Where files live, and moving the ones an older layout left behind."""

import importlib
import os

import pytest


@pytest.fixture
def paths_in(tmp_path, monkeypatch):
    """A fresh `paths` module rooted at tmp_path."""
    def load(root=tmp_path):
        monkeypatch.setenv("DASHBOARD_DATA_DIR", str(root))
        monkeypatch.setenv("DASHBOARD_NOTES_DIR", str(root / "notes"))
        import paths
        return importlib.reload(paths)
    yield load
    # Put the module back the way the rest of the suite expects it.
    monkeypatch.undo()
    import paths
    importlib.reload(paths)


# ── layout ───────────────────────────────────────────────────────────

def test_generated_files_live_in_cache(paths_in):
    paths = paths_in()
    assert paths.CACHE_PATH.parent == paths.CACHE_DIR
    assert paths.SYNC_STATUS_PATH.parent == paths.CACHE_DIR
    assert paths.CACHE_DIR.name == "cache"


def test_your_content_lives_in_data(paths_in):
    paths = paths_in()
    for path in (paths.TODO_PATH, paths.HABITS_PATH, paths.ROUGH_NOTES_PATH):
        assert path.parent == paths.USER_DATA_DIR
    assert paths.USER_DATA_DIR.name == "data"


def test_credentials_live_in_secrets(paths_in):
    paths = paths_in()
    for path in (paths.ICAL_URL_PATH, paths.GOOGLE_SYNC_CREDENTIALS_PATH,
                 paths.GOOGLE_SYNC_TOKEN_PATH, paths.GRAPH_APP_PATH,
                 paths.GRAPH_TOKEN_CACHE_PATH):
        assert path.parent == paths.SECRETS_DIR
    assert paths.SECRETS_DIR.name == "secrets"


def test_logs_live_in_logs(paths_in):
    paths = paths_in()
    assert paths.SYNC_LOG_PATH.parent == paths.LOGS_DIR
    assert paths.CRASH_LOG_PATH.parent == paths.LOGS_DIR


def test_the_journal_stays_with_the_org_notes(paths_in):
    paths = paths_in()
    assert paths.JOURNAL_PATH.parent == paths.DOOMNOTES_DIR
    assert paths.DOOMNOTES_DIR != paths.PROJECT_DIR


def test_scripts_stay_in_the_project_root(paths_in):
    paths = paths_in()
    assert paths.COLLECTOR_SCRIPT.parent == paths.PROJECT_DIR


# ── creating the folders ─────────────────────────────────────────────

def test_prepare_creates_every_folder(paths_in, tmp_path):
    paths = paths_in()
    assert not paths.CACHE_DIR.exists()

    paths.prepare()

    for directory in paths.MANAGED_DIRS:
        assert directory.is_dir()


def test_prepare_is_safe_to_call_repeatedly(paths_in):
    paths = paths_in()
    paths.prepare()
    assert paths.prepare() == []          # nothing left to move


# ── migrating the old flat layout ────────────────────────────────────

def test_files_from_the_flat_layout_are_rehomed(paths_in, tmp_path):
    paths = paths_in()
    (tmp_path / "todo.json").write_text('{"items": []}', encoding="utf-8")
    (tmp_path / "cache.json").write_text("{}", encoding="utf-8")
    (tmp_path / "calendar_url.txt").write_text("https://example.com/f.ics",
                                               encoding="utf-8")

    moved = dict(paths.prepare())

    assert paths.TODO_PATH.read_text(encoding="utf-8") == '{"items": []}'
    assert paths.CACHE_PATH.exists()
    assert paths.ICAL_URL_PATH.read_text(encoding="utf-8").startswith("https://")
    assert not (tmp_path / "todo.json").exists()        # moved, not copied
    assert set(moved) == {"todo.json", "cache.json", "calendar_url.txt"}


def test_migration_never_overwrites_newer_data(paths_in, tmp_path):
    paths = paths_in()
    paths.ensure_dirs()
    paths.TODO_PATH.write_text("the current list", encoding="utf-8")
    (tmp_path / "todo.json").write_text("a stale leftover", encoding="utf-8")

    paths.prepare()

    assert paths.TODO_PATH.read_text(encoding="utf-8") == "the current list"
    assert (tmp_path / "todo.json").exists()            # left alone


def test_migration_leaves_the_scripts_alone(paths_in, tmp_path):
    paths = paths_in()
    (tmp_path / "collector.py").write_text("print('hi')", encoding="utf-8")

    paths.prepare()

    assert (tmp_path / "collector.py").exists()


def test_a_directory_in_the_way_is_not_moved(paths_in, tmp_path):
    """A folder named like a legacy file shouldn't be dragged anywhere."""
    paths = paths_in()
    (tmp_path / "logs").mkdir()
    (tmp_path / "cache.json").mkdir()

    moved = paths.prepare()

    assert moved == []
    assert (tmp_path / "cache.json").is_dir()


def test_secrets_are_rehomed_together(paths_in, tmp_path):
    paths = paths_in()
    for name in ("gcal_sync_credentials.json", "gcal_sync_token.json",
                 "graph_app.json", "graph_token_cache.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")

    paths.prepare()

    assert sorted(p.name for p in paths.SECRETS_DIR.iterdir()) == [
        "gcal_sync_credentials.json", "gcal_sync_token.json",
        "graph_app.json", "graph_token_cache.json"]


def test_the_environment_override_still_moves_the_whole_tree(paths_in, tmp_path):
    elsewhere = tmp_path / "somewhere else"
    elsewhere.mkdir()
    paths = paths_in(elsewhere)

    paths.prepare()

    assert (elsewhere / "cache").is_dir()
    assert os.environ["DASHBOARD_DATA_DIR"] == str(elsewhere)
