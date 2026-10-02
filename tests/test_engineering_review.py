"""工程審查回歸：確定性競態、過期 UI 事件與原始檔案保護。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Event

import pytest
from textual.widgets import Input, OptionList, Select

import backend
import launch
from backend import DirectoryIndex, Favorites, Item, browse_folder, scan_names
from main import ConfirmOpen, FileFinderApp, FileList, View


async def wait_until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("specific", [False, True])
def test_invalidation_during_restore_cannot_publish_old_snapshot(project, monkeypatch, specific):
    config, file = project
    root = config.roots[0].path
    index = DirectoryIndex(file.with_name("cache.sqlite3"))
    browse_folder(root, index=index)
    index.flush()
    restored = DirectoryIndex(index.file)
    started, release = Event(), Event()
    original = restored._connect

    def delayed():
        started.set()
        assert release.wait(5)
        return original()

    monkeypatch.setattr(restored, "_connect", delayed)
    with ThreadPoolExecutor(max_workers=1) as executor:
        loading = executor.submit(restored.load, (root,), Event())
        try:
            assert started.wait(5)
            restored.invalidate(root if specific else None)
        finally:
            release.set()
        assert loading.result(timeout=5) == 0
    assert restored.directory_item(root) is None
    assert restored.unverified_count == 0


def test_restore_cannot_replace_a_newer_memory_snapshot(project, monkeypatch):
    config, file = project
    root = config.roots[0].path
    index = DirectoryIndex(file.with_name("cache.sqlite3"))
    browse_folder(root, index=index)
    index.flush()
    restored = DirectoryIndex(index.file)
    started, release = Event(), Event()
    original = restored._connect

    def delayed():
        started.set()
        assert release.wait(5)
        return original()

    monkeypatch.setattr(restored, "_connect", delayed)
    with ThreadPoolExecutor(max_workers=1) as executor:
        loading = executor.submit(restored.load, (root,), Event())
        try:
            assert started.wait(5)
            target = root / "new.txt"
            target.touch()
            assert browse_folder(root, index=restored).total == 1
        finally:
            release.set()
        assert loading.result(timeout=5) == 0
    assert browse_folder(root, index=restored).items[0].path == target


def test_cancellation_at_last_restore_record_does_not_publish(project, monkeypatch):
    config, file = project
    root = config.roots[0].path
    (root / "report.txt").touch()
    index = DirectoryIndex(file.with_name("cache.sqlite3"))
    browse_folder(root, index=index)
    index.flush()
    restored = DirectoryIndex(index.file)
    cancel = Event()
    original = restored._timestamp
    calls = 0

    def last_record(value):
        nonlocal calls
        calls += 1
        if calls == 2:
            cancel.set()
        return original(value)

    monkeypatch.setattr(restored, "_timestamp", last_record)
    assert restored.load((root,), cancel) == 0
    assert restored.directory_item(root) is None


@pytest.mark.parametrize("folders", [False, True])
def test_parallel_favorite_toggles_do_not_lose_saved_paths(tmp_path, monkeypatch, folders):
    first, second = tmp_path / "first", tmp_path / "second"
    for path in (first, second):
        path.mkdir() if folders else path.touch()
    store = Favorites(tmp_path / "favorites.json")
    started, release, attempted, second_done = Event(), Event(), Event(), Event()
    original = backend.os.replace

    def delayed(source, destination):
        if json.loads(Path(source).read_text(encoding="utf-8")) == [str(first)]:
            started.set()
            assert release.wait(5)
        return original(source, destination)

    def toggle_second():
        attempted.set()
        try:
            return store.toggle(second)
        finally:
            second_done.set()

    monkeypatch.setattr(backend.os, "replace", delayed)
    with ThreadPoolExecutor(max_workers=2) as executor:
        saving = executor.submit(store.toggle, first)
        try:
            assert started.wait(5)
            other = executor.submit(toggle_second)
            assert attempted.wait(5)
            # 舊程式第二個保存可越過第一個；修正後會等待同一實例的鎖。
            second_done.wait(0.2)
        finally:
            release.set()
        assert saving.result(timeout=5) and other.result(timeout=5)
    assert set(store.paths) == {first, second}
    assert set(Favorites(store.file).paths) == {first, second}
    assert not list(tmp_path.glob(".favorites-*.tmp"))


@pytest.mark.parametrize("replacement", ["valid", "invalid", "deleted", "created", "null"])
def test_external_favorite_changes_are_preserved(tmp_path, replacement):
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    first.touch()
    second.touch()
    store = Favorites(tmp_path / "favorites.json")
    if replacement not in {"created", "null"}:
        store.toggle(first)
    original_paths = store.paths.copy()
    if replacement == "deleted":
        store.file.unlink()
    else:
        contents = "null" if replacement == "null" else ("{broken" if replacement == "invalid" else json.dumps([str(second)]))
        store.file.write_text(contents, encoding="utf-8")
    external = store.file.read_bytes() if store.file.exists() else None
    with pytest.raises(ValueError, match="變更"):
        store.toggle(second)
    assert store.paths == original_paths
    assert (store.file.read_bytes() if store.file.exists() else None) == external
    assert not list(tmp_path.glob(".favorites-*.tmp"))


@pytest.mark.parametrize("event_type", [OptionList.OptionSelected, OptionList.OptionHighlighted])
def test_old_list_event_cannot_select_a_different_path(project, event_type):
    config, file = project
    root = config.roots[0].path
    old, new = root / "old.txt", root / "new.txt"
    old.touch()
    new.touch()

    async def scenario():
        app = FileFinderApp(file, opener=lambda _: pytest.fail("不得開啟舊事件對應的新檔案"))
        async with app.run_test() as pilot:
            await wait_until(lambda: app.config is not None)
            results = app.query_one(FileList)
            app.navigation.view = View("browse", directory=root, items=(Item(old, False),))
            app.render_view()
            event = event_type(results, results.get_option_at_index(0), 0)
            app.navigation.view = View("browse", directory=root, items=(Item(new, False), Item(old, False)), selected=1)
            app.render_view()
            await pilot.pause()
            app.post_message(event)
            await pilot.pause()
            assert not isinstance(app.screen, ConfirmOpen)
            assert app.navigation.view.selected == 1

    asyncio.run(scenario())


def test_favorite_completion_retains_selection_moved_while_saving(project, monkeypatch):
    config, file = project
    root = config.roots[0].path
    paths = [root / f"report-{letter}.txt" for letter in "abc"]
    store = Favorites(file.with_name("favorites.json"))
    for path in paths:
        path.touch()
        store.toggle(path)
    started, release = Event(), Event()
    original = Favorites.toggle

    def delayed(self, path):
        started.set()
        assert release.wait(5)
        return original(self, path)

    monkeypatch.setattr(Favorites, "toggle", delayed)

    async def scenario():
        app = FileFinderApp(file)
        try:
            async with app.run_test() as pilot:
                await wait_until(lambda: app.config is not None)
                await pilot.press("alt+2")
                app.query_one(Input).value = "report"
                await wait_until(lambda: app.navigation.view.report is not None)
                await pilot.press("down", "ctrl+f")
                await wait_until(started.is_set)
                await pilot.press("down", "down")
                assert app.navigation.view.items[app.navigation.view.selected].path == paths[2]
                release.set()
                await wait_until(lambda: app.navigation.view.report is not None and len(app.navigation.view.items) == 2)
                assert app.navigation.view.items[app.navigation.view.selected].path == paths[2]
        finally:
            release.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("menu", ["folders", "favorites"])
def test_empty_list_arrows_enter_and_dropdown_escape_are_safe(project, menu):
    config, file = project

    async def scenario():
        app = FileFinderApp(file, opener=lambda _: pytest.fail("空清單不應開啟檔案"))
        async with app.run_test(size=(80, 30)) as pilot:
            await wait_until(lambda: app.config is not None)
            if menu == "folders":
                await pilot.press("down", "enter")
                await wait_until(lambda: app.navigation.view.report is not None)
            else:
                await pilot.press("alt+2")
                await wait_until(lambda: not app.navigation.view.items)
            view = app.navigation.view
            await pilot.press("ctrl+l", "up", "down", "enter")
            assert app.query_one(FileList).highlighted is None
            assert app.navigation.view is view
            assert await pilot.click("#kind-select SelectCurrent")
            await pilot.press("escape")
            assert not app.query_one("#kind-select", Select).expanded
            assert app.navigation.view is view

    asyncio.run(scenario())


def test_all_backend_operations_preserve_source_file_contents(project):
    config, file = project
    root = config.roots[0].path
    child = root / "folder"
    child.mkdir()
    target = child / "report.sql"
    contents = b"SELECT 1;\n\x00sentinel"
    target.write_bytes(contents)
    index = DirectoryIndex(file.with_name("cache.sqlite3"))
    store = Favorites(file.with_name("favorites.json"))
    for path in (target, child):
        store.toggle(path)
    assert scan_names(config, "report", index=index).total == 1
    assert browse_folder(child, index=index).total == 1
    index.flush()
    index.check_changes(Event(), refresh_times=True)
    index.invalidate()
    index.flush()
    opened = []
    assert "已要求" in backend.open_file(target, opened.append)
    assert "已要求" in backend.open_containing_folder(target, opened.append)
    store.toggle(target)
    store.toggle(child)
    assert opened == [str(target), str(child)]
    assert target.read_bytes() == contents and child.is_dir()


def test_non_windows_tool_lookup_reports_supported_platform(monkeypatch):
    monkeypatch.delattr(launch.ctypes, "windll", raising=False)
    with pytest.raises(RuntimeError, match="Windows"):
        launch.trusted_windows_tools()


def test_invalid_probe_output_does_not_escape_as_decode_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "is_file", lambda _: True)

    def invalid(*args, **kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid probe output")

    monkeypatch.setattr(launch.subprocess, "run", invalid)
    with pytest.raises(RuntimeError, match="找不到 Python"):
        launch.find_python(tmp_path)


def test_external_favorite_change_during_temp_write_is_preserved(tmp_path, monkeypatch):
    target, external = tmp_path / "target.txt", tmp_path / "external.txt"
    target.touch()
    external.touch()
    store = Favorites(tmp_path / "favorites.json")
    original = backend.os.fsync

    def change_during_fsync(fd):
        original(fd)
        store.file.write_text(json.dumps([str(external)]), encoding="utf-8")

    monkeypatch.setattr(backend.os, "fsync", change_during_fsync)
    with pytest.raises(ValueError, match="變更"):
        store.toggle(target)
    assert Favorites(store.file).paths == [external]
    assert store.paths == [] and not list(tmp_path.glob(".favorites-*.tmp"))


@pytest.mark.parametrize("kind", ["extra-table", "trigger", "view", "multiple-versions", "empty-version"])
def test_unexpected_database_objects_are_preserved(project, kind):
    config, file = project
    database = file.with_name("cache.sqlite3")
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE finder_meta(version INTEGER NOT NULL)")
        connection.execute("INSERT INTO finder_meta VALUES (1)")
        connection.execute("CREATE TABLE finder_directories(key TEXT PRIMARY KEY,payload TEXT NOT NULL)")
        if kind == "extra-table":
            connection.execute("CREATE TABLE private_data(value TEXT)")
            connection.execute("INSERT INTO private_data VALUES ('preserve')")
        elif kind == "trigger":
            connection.execute("CREATE TRIGGER unexpected AFTER INSERT ON finder_directories BEGIN DELETE FROM finder_meta; END")
        elif kind == "view":
            connection.execute("CREATE VIEW unexpected AS SELECT * FROM finder_meta")
        elif kind == "multiple-versions":
            connection.execute("INSERT INTO finder_meta VALUES (99)")
        else:
            connection.execute("DELETE FROM finder_meta")
    original = database.read_bytes()
    index = DirectoryIndex(database)
    assert index.load(tuple(root.path for root in config.roots), Event()) == 0
    assert index.error
    browse_folder(config.roots[0].path, index=index)
    index.invalidate()
    index.flush()
    assert database.read_bytes() == original


def test_empty_directory_restore_has_its_own_resource_limit(project, monkeypatch):
    config, file = project
    index = DirectoryIndex(file.with_name("cache.sqlite3"))
    scan_names(config, "nothing", index=index)
    index.flush()
    monkeypatch.setattr(backend, "MAX_RESTORED_DIRECTORIES", 1, raising=False)
    restored = DirectoryIndex(index.file)
    assert restored.load(tuple(root.path for root in config.roots), Event()) == 1
    # 略過的目錄按需正常讀取，不能把未還原的資料視為完整快取。
    report = scan_names(config, "nothing", index=restored)
    assert report.cached_directories == 1 and report.read_directories == 2


@pytest.mark.parametrize("operation", ["browse", "restore"])
def test_pre_cancelled_request_does_not_start_filesystem_io(project, monkeypatch, operation):
    config, file = project
    root = config.roots[0].path
    cancel = Event()
    cancel.set()
    index = DirectoryIndex(file.with_name("cache.sqlite3"))

    def forbidden(*args):
        pytest.fail("已取消的請求不應開始新的檔案系統呼叫")

    monkeypatch.setattr(backend, "plain_metadata", forbidden)
    monkeypatch.setattr(index, "_connect", forbidden)
    if operation == "browse":
        assert browse_folder(root, cancel, index=index).cancelled
    else:
        assert index.load((root,), cancel) == 0
        assert not index.file.exists()


def test_other_process_cannot_pass_favorite_save_lock(tmp_path, monkeypatch):
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    first.touch()
    second.touch()
    store = Favorites(tmp_path / "favorites.json")
    started, release = Event(), Event()
    original = backend.os.replace

    def delayed(source, destination):
        started.set()
        assert release.wait(5)
        return original(source, destination)

    monkeypatch.setattr(backend.os, "replace", delayed)
    script = (
        "import sys; from pathlib import Path; from backend import Favorites; "
        "store=Favorites(Path(sys.argv[1])); print('ready',flush=True); "
        "\ntry: store.toggle(Path(sys.argv[2])); print('saved')"
        "\nexcept ValueError: print('conflict')"
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        saving = executor.submit(store.toggle, first)
        process = None
        try:
            assert started.wait(5)
            process = subprocess.Popen([sys.executable, "-c", script, str(store.file), str(second)],
                                       cwd=Path(backend.__file__).parent, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            assert process.stdout.readline().strip() == "ready"
            with pytest.raises(subprocess.TimeoutExpired):
                process.communicate(timeout=0.2)
        finally:
            release.set()
            if process is not None:
                stdout, stderr = process.communicate(timeout=5)
        assert saving.result(timeout=5)
    assert process.returncode == 0, stderr
    assert stdout.strip() == "conflict"
    assert Favorites(store.file).paths == [first]


def test_favorite_lock_timeout_preserves_file_and_memory(tmp_path, monkeypatch):
    target = tmp_path / "report.txt"
    target.touch()
    store = Favorites(tmp_path / "favorites.json")
    monkeypatch.setattr(backend, "FAVORITE_LOCK_TIMEOUT_SECONDS", 0.02)
    with backend.favorite_file_lock(store.file):
        with pytest.raises(ValueError, match="鎖定"):
            store.toggle(target)
    assert not store.file.exists() and not store.paths
    assert store.toggle(target)


def test_restore_uses_one_payload_query_per_scope(project, monkeypatch):
    config, file = project
    root = config.roots[0].path
    for number in range(6):
        (root / f"child{number}").mkdir()
    index = DirectoryIndex(file.with_name("cache.sqlite3"))
    scan_names(config, "nothing", index=index)
    index.flush()
    restored = DirectoryIndex(index.file)
    original = restored._connect
    queries = []

    def counted():
        connection = original()
        connection.set_trace_callback(queries.append)
        return connection

    monkeypatch.setattr(restored, "_connect", counted)
    assert restored.load((root,), Event()) == 7
    assert len([query for query in queries if "FROM finder_directories" in query]) == 1


def test_restore_scope_treats_sql_wildcards_as_literal(tmp_path):
    wanted, other = tmp_path / "scope%_", tmp_path / "scopeOTHER"
    for root in (wanted, other):
        root.mkdir()
        (root / "report.txt").touch()
    index = DirectoryIndex(tmp_path / "cache.sqlite3")
    browse_folder(wanted, index=index)
    browse_folder(other, index=index)
    index.flush()
    restored = DirectoryIndex(index.file)
    assert restored.load((wanted,), Event()) == 1
    assert restored.directory_item(other) is None


def test_restore_deduplicates_many_overlapping_scopes(project):
    config, file = project
    root = config.roots[0].path
    index = DirectoryIndex(file.with_name("cache.sqlite3"))
    browse_folder(root, index=index)
    index.flush()
    restored = DirectoryIndex(index.file)
    assert restored.load((root,) * 205, Event()) == 1
