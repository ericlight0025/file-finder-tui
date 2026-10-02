"""驗證 Lazy Loading、歷史與 Textual 真實事件迴圈操作。"""

import asyncio
import json
from pathlib import Path
from threading import Event

import pytest
from textual import events
from textual.widgets import Input, OptionList, Static, Tabs

import backend
import main
from backend import Config, Favorites, Item, Root, ScanReport, SearchProgress, browse_folder
from main import ConfirmOpen, ErrorDetails, FileFinderApp, IOResult, Navigation, View


async def wait_until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def test_lazy_loading_lists_all_direct_children(project):
    config, _ = project
    root = config.roots[0].path
    child = root / "zFolder"
    child.mkdir()
    (child / "grandchild.txt").touch()
    for number in range(220):
        (root / f"file{number:03}.txt").touch()
    report = browse_folder(root)
    assert report.total == 221
    assert report.items[0].path == child
    assert not any(item.name == "grandchild.txt" for item in report.items)


def test_empty_missing_file_and_cancelled_folder(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert browse_folder(empty).items == ()
    assert FileFinderApp.browse_status(browse_folder(empty)) == "空資料夾"
    assert browse_folder(tmp_path / "missing").errors
    file = tmp_path / "file.txt"
    file.touch()
    assert browse_folder(file).errors
    cancel = Event()
    cancel.set()
    assert browse_folder(empty, cancel).cancelled


@pytest.mark.parametrize("error", [PermissionError(), FileNotFoundError(), OSError(53, "網路斷線")])
def test_browse_io_failure_is_clear(tmp_path, monkeypatch, error):
    def denied(path):
        raise error

    monkeypatch.setattr(backend.os, "scandir", denied)
    report = browse_folder(tmp_path)
    assert report.errors and not report.complete
    assert str(tmp_path) in FileFinderApp.browse_status(report)


def test_deep_folder_returns_to_original_search():
    navigation = Navigation()
    navigation.search("rpt")
    navigation.view.items = (Item(Path("C:/deep/report"), True),)
    navigation.view.selected = 0
    before = navigation.view
    navigation.enter(Path("C:/deep/report"))
    navigation.enter(Path("C:/deep/report/child"))
    assert navigation.back()
    assert navigation.view.directory == Path("C:/deep/report")
    assert navigation.back()
    assert navigation.view == before
    assert navigation.view.query == "rpt"
    assert not navigation.back()


def test_forward_navigation_and_new_route_clears_forward_history():
    navigation = Navigation()
    root = Path("C:/deep/report")
    child = root / "child"
    other = Path("C:/deep/other")
    navigation.enter(root)
    navigation.enter(child)
    assert navigation.back()
    assert navigation.view.directory == root
    assert navigation.forward()
    assert navigation.view.directory == child
    assert navigation.back()
    navigation.enter(other)
    assert navigation.view.directory == other
    assert not navigation.forward()
    navigation.home()
    assert not navigation.history
    assert not navigation.forward_history


def test_favorite_deep_folder_returns_home_and_escape_clears_history():
    navigation = Navigation()
    navigation.enter(Path("U:/SQL/deep"))
    assert navigation.back()
    assert navigation.view.kind == "home"
    navigation.enter(Path("U:/SQL"))
    navigation.search("new")
    assert navigation.history == []
    navigation.home()
    assert navigation.view == View()


def test_tui_search_enter_back_escape_and_input_backspace(project):
    config, file = project
    deep = config.roots[0].path / "first" / "report"
    deep.mkdir(parents=True)
    (deep / "child.txt").touch()

    async def scenario():
        app = FileFinderApp(file, opener=lambda path: None)
        async with app.run_test(size=(110, 40)) as pilot:
            await wait_until(lambda: app.config is not None)
            app.query_one(Input).value = "report"
            await wait_until(lambda: app.navigation.view.report is not None)
            assert app.navigation.view.items[0].path == deep
            app.query_one(OptionList).focus()
            await pilot.press("enter")
            await wait_until(lambda: app.navigation.view.kind == "browse" and app.navigation.view.report is not None)
            assert app.query_one(Input).value == ""
            assert app.navigation.view.items[0].name == "child.txt"
            await pilot.press("backspace")
            assert app.navigation.view.kind == "search"
            assert app.query_one(Input).value == "report"
            assert app.navigation.view.items[0].path == deep
            await pilot.press("ctrl+l")
            assert isinstance(app.focused, Input)
            await pilot.press("alt+right")
            await wait_until(lambda: app.navigation.view.kind == "browse" and app.navigation.view.directory == deep)
            assert app.query_one(Input).value == ""
            await pilot.press("alt+left")
            await wait_until(lambda: app.navigation.view.kind == "search")
            await pilot.press("ctrl+l", "backspace")
            await pilot.pause()
            assert app.query_one(Input).value == "repor"
            await pilot.press("escape")
            await wait_until(lambda: app.navigation.view.kind == "home" and len(app.navigation.view.items) > 0)
            assert app.query_one(Input).value == ""
            assert app.navigation.history == []

    asyncio.run(scenario())


def test_tui_debounce_responsiveness_and_stale_result_guard(project, monkeypatch):
    _, file = project
    started, release, old_cancelled = Event(), Event(), Event()
    calls = []

    def slow_scan(config, query, cancel, progress=None, index=None):
        calls.append(query)
        if query == "old":
            started.set()
            release.wait(5)
            if cancel.is_set():
                old_cancelled.set()
            return ScanReport((Item(config.roots[0].path / "OLD.txt", False),), 1)
        return ScanReport((Item(config.roots[0].path / "NEW.txt", False),), 1)

    monkeypatch.setattr(backend, "scan_names", slow_scan)

    async def scenario():
        app = FileFinderApp(file)
        try:
            async with app.run_test(size=(110, 40)) as pilot:
                await wait_until(lambda: app.config is not None)
                search = app.query_one(Input)
                search.value = "o"
                await asyncio.sleep(0.04)
                search.value = "ol"
                await asyncio.sleep(0.04)
                assert calls == []
                search.value = "old"
                await wait_until(started.is_set)
                # 掃描執行緒刻意卡住，鍵盤事件仍必須立即生效。
                await pilot.press("ctrl+l")
                assert app.focused is search
                search.value = "new"
                await wait_until(lambda: app.navigation.view.report is not None)
                generation = app.gate.generation
                assert app.navigation.view.items[0].name == "NEW.txt"
                app.post_message(IOResult(generation - 1, "search", ScanReport((Item(Path("OLD.txt"), False),), 1)))
                release.set()
                await wait_until(old_cancelled.is_set)
                await pilot.pause()
                assert app.navigation.view.items[0].name == "NEW.txt"
                assert calls == ["old", "new"]
                await pilot.press("escape")
                await wait_until(lambda: app.navigation.view.kind == "home" and bool(app.navigation.view.items))
                assert app.navigation.view.kind == "home"
        finally:
            release.set()

    asyncio.run(scenario())


def test_live_progress_elapsed_time_stale_messages_and_final_status(project, monkeypatch):
    config, file = project
    old_started, new_started = Event(), Event()
    release_old, release_new = Event(), Event()
    callbacks = {}

    def controlled_scan(config, query, cancel, progress, index=None):
        callbacks[query] = progress
        if query == "old":
            progress(SearchProgress(3, 2, config.roots[0].path, 1))
            old_started.set()
            release_old.wait(5)
            progress(SearchProgress(999, 999, config.roots[0].path))
            return ScanReport(total=999)
        progress(SearchProgress(8, 4, config.roots[1].path))
        new_started.set()
        release_new.wait(5)
        return ScanReport((Item(config.roots[1].path / "new.txt", False),), total=4, scanned=8)

    monkeypatch.setattr(backend, "scan_names", controlled_scan)

    async def scenario():
        app = FileFinderApp(file)
        try:
            async with app.run_test(size=(110, 40)) as pilot:
                await wait_until(lambda: app.config is not None)
                app.query_one(Input).value = "old"
                await wait_until(lambda: old_started.is_set() and app.navigation.view.progress is not None and app.navigation.view.progress.scanned == 3)
                old_generation = app.gate.generation
                status = app.navigation.view.status
                assert "已掃描 3" in status and "至少 2" in status
                assert "尚未完成" in status and "命中總數" not in status
                assert str(config.roots[0].path) in status
                assert "讀取錯誤 1" in status
                await asyncio.sleep(0.25)
                assert app.navigation.view.status != status
                await pilot.press("ctrl+l")
                assert isinstance(app.focused, Input)
                app.query_one(Input).value = "new"
                await wait_until(lambda: new_started.is_set() and app.navigation.view.progress.found == 4)
                release_old.set()
                app.post_message(IOResult(old_generation, "progress", SearchProgress(999, 999)))
                await pilot.pause()
                assert app.navigation.view.progress.found == 4
                assert "999" not in app.navigation.view.status
                release_new.set()
                await wait_until(lambda: app.navigation.view.report is not None)
                finished = app.navigation.view.status
                assert "命中總數 4" in finished
                callbacks["new"](SearchProgress(999, 999))
                await asyncio.sleep(0.25)
                await pilot.pause()
                assert app.navigation.view.status == finished
                assert app.navigation.view.items[0].name == "new.txt"
                await pilot.press("escape")
                await wait_until(lambda: app.navigation.view.kind == "home" and bool(app.navigation.view.items))
                callbacks["new"](SearchProgress(999, 999))
                await pilot.pause()
                assert app.navigation.view.kind == "home"
                assert "999" not in app.navigation.view.status
        finally:
            release_old.set()
            release_new.set()

    asyncio.run(scenario())


def test_escape_during_progress_keeps_homepage(project, monkeypatch):
    _, file = project
    started, release = Event(), Event()

    def controlled_scan(config, query, cancel, progress, index=None):
        progress(SearchProgress(3, 2, config.roots[0].path))
        started.set()
        release.wait(5)
        progress(SearchProgress(999, 999))
        return ScanReport(total=999, cancelled=cancel.is_set())

    monkeypatch.setattr(backend, "scan_names", controlled_scan)

    async def scenario():
        app = FileFinderApp(file)
        try:
            async with app.run_test(size=(110, 40)) as pilot:
                await wait_until(lambda: app.config is not None)
                app.query_one(Input).value = "report"
                await wait_until(lambda: started.is_set() and app.navigation.view.progress is not None)
                await pilot.press("escape")
                await wait_until(lambda: app.navigation.view.kind == "home" and bool(app.navigation.view.items))
                release.set()
                await pilot.pause()
                assert app.navigation.view.kind == "home"
                assert app.query_one(Input).value == ""
                assert "999" not in app.navigation.view.status
        finally:
            release.set()

    asyncio.run(scenario())


def test_tui_favorites_focus_persistence_and_return_home(project):
    config, file = project
    deep = config.roots[0].path / "deep"
    deep.mkdir()
    favorites_file = file.with_name("favorites.json")
    Favorites(favorites_file).toggle(deep)

    async def scenario():
        app = FileFinderApp(file)
        async with app.run_test(size=(110, 40)) as pilot:
            await wait_until(lambda: app.config is not None)
            assert [item.path for item in app.navigation.view.items] == [root.path for root in config.roots]
            await pilot.press("alt+2")
            await wait_until(lambda: app.active_menu == "favorites" and bool(app.navigation.view.items))
            assert app.navigation.view.items[0].path == deep
            results = app.query_one(OptionList)
            for item, option in zip(app.navigation.view.items, results.options):
                if item is not None and item.label == config.roots[0].name:
                    assert str(config.roots[0].path) == str(option.prompt).rstrip().splitlines()[-1].strip()
                elif item is not None and item.path == deep:
                    assert str(deep.parent) == str(option.prompt).rstrip().splitlines()[-1].strip()
            results.focus()
            results.highlighted = 0
            await pilot.press("enter")
            await wait_until(lambda: app.navigation.view.report is not None)
            await pilot.press("backspace")
            await wait_until(lambda: app.navigation.view.kind == "home" and bool(app.navigation.view.items))
            assert app.navigation.view.selected == 0
            results.highlighted = 0
            await pilot.press("ctrl+f")
            await wait_until(lambda: not app.favorites.contains(deep))
            assert Favorites(favorites_file).paths == []
            await pilot.press("ctrl+l", "f")
            assert app.query_one(Input).value == "f"

    asyncio.run(scenario())


def test_tui_executable_confirmation_and_normal_open(project):
    config, file = project
    root = config.roots[0].path
    executable, ordinary = root / "test.ps1", root / "test.txt"
    executable.touch()
    ordinary.touch()
    opened = []

    async def scenario():
        app = FileFinderApp(file, opener=opened.append)
        async with app.run_test(size=(110, 40)) as pilot:
            await wait_until(lambda: app.config is not None)
            app.navigation.enter(root)
            app.navigation.view.items = browse_folder(root).items
            app.render_view()
            results = app.query_one(OptionList)
            results.focus()
            results.highlighted = 0
            await pilot.press("enter")
            assert isinstance(app.screen, ConfirmOpen)
            assert opened == []
            await pilot.press("alt+left", "alt+right", "ctrl+e", "ctrl+f", "alt+2", "ctrl+tab")
            assert isinstance(app.screen, ConfirmOpen)
            assert app.navigation.view.directory == root
            assert len(app.navigation.history) == 1
            assert app.favorites.paths == []
            await pilot.press("escape")
            assert opened == []
            await pilot.press("enter")
            await pilot.click("#confirm")
            await wait_until(lambda: len(opened) == 1)
            assert opened[0] == str(executable)
            results.highlighted = 1
            await pilot.press("enter")
            assert isinstance(app.screen, ConfirmOpen)
            assert len(opened) == 1
            await pilot.click("#confirm")
            await wait_until(lambda: len(opened) == 2)
            assert opened[1] == str(ordinary)

    asyncio.run(scenario())


def test_home_hides_header_and_config_path(project, monkeypatch):
    _, file = project
    monkeypatch.chdir(file.parent)

    async def scenario():
        app = FileFinderApp(Path("config.json"))
        async with app.run_test(size=(110, 40)):
            await wait_until(lambda: app.config is not None)
            assert app.config_path == file
            assert not app.query("#title")
            assert not app.query_one("#location", Static).display
            assert str(file) not in str(app.query_one("#location", Static).render())

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["home", "search", "browse"])
def test_all_errors_modal_preserves_navigation_and_favorites(project, kind):
    config, file = project
    missing = config.roots[0].path / "missing"
    errors = tuple(f"{missing / str(index)}：沒有讀取權限" for index in range(30))
    opened = []

    async def scenario():
        app = FileFinderApp(file, opener=opened.append)
        async with app.run_test(size=(110, 40)) as pilot:
            await wait_until(lambda: app.config is not None)
            app.navigation.enter(config.roots[0].path)
            view = app.navigation.view
            view.kind = kind
            view.items = tuple(Item(missing / str(index), True, error=error) for index, error in enumerate(errors))
            view.report = None if kind == "home" else ScanReport(errors=errors)
            app.render_view()
            app.query_one("#results", OptionList).focus()
            selected = app.navigation.view.selected
            await pilot.press("ctrl+e")
            assert isinstance(app.screen, ErrorDetails)
            error_list = app.screen.query_one("#error-list", OptionList)
            assert error_list.option_count == len(errors)
            assert errors[-1] in str(error_list.get_option_at_index(29).prompt)
            await pilot.press("end", "enter", "f", "ctrl+f", "backspace", "alt+left", "alt+right", "ctrl+l", "alt+2")
            assert isinstance(app.screen, ErrorDetails)
            assert app.navigation.view is view
            assert app.navigation.view.selected == selected
            assert app.favorites.paths == []
            assert not opened
            await pilot.press("escape")
            assert not isinstance(app.screen, ErrorDetails)
            assert app.navigation.view is view

    asyncio.run(scenario())


def test_missing_config_error_details_and_guidance(tmp_path):
    async def scenario():
        app = FileFinderApp(tmp_path / "config.json")
        async with app.run_test(size=(110, 40)) as pilot:
            await wait_until(lambda: app.query_one(Input).disabled)
            assert "config.example.json" in app.navigation.view.status
            await pilot.press("ctrl+e")
            assert isinstance(app.screen, ErrorDetails)
            assert str(tmp_path / "config.json") in app.screen.errors[0]
            await pilot.click("#error-close")
            assert not isinstance(app.screen, ErrorDetails)

    asyncio.run(scenario())


def test_tui_invalid_config_is_displayed_without_crash(tmp_path):
    async def scenario():
        app = FileFinderApp(tmp_path / "missing.json")
        async with app.run_test(size=(100, 30)) as pilot:
            await wait_until(lambda: app.query_one(Input).disabled)
            assert "config.json" in app.navigation.view.status
            await pilot.press("escape")
            assert "config.json" in app.navigation.view.status
            await pilot.press("ctrl+q")

    asyncio.run(scenario())


def test_tui_mouse_single_click_selects_and_double_click_enters_folder(project):
    config, file = project

    async def scenario():
        app = FileFinderApp(file)
        async with app.run_test(size=(110, 40)) as pilot:
            await wait_until(lambda: app.config is not None)
            await pilot.pause()
            # 根目錄直接列在資料夾分頁，每個項目顯示名稱、更新時間與路徑。
            clicked = await pilot.click("#results", offset=(5, 1))
            assert clicked
            await pilot.pause()
            assert app.navigation.view.kind == "home"
            assert app.query_one(OptionList).highlighted == 0
            assert await pilot.double_click("#results", offset=(5, 1))
            await wait_until(lambda: app.navigation.view.kind == "browse" and app.navigation.view.report is not None)
            assert app.navigation.view.directory == config.roots[0].path
            await pilot.press("backspace")
            await wait_until(lambda: app.navigation.view.kind == "home" and bool(app.navigation.view.items))

    asyncio.run(scenario())


def test_startup_input_and_escape_do_not_cancel_initialization(project, monkeypatch):
    config, file = project
    (config.roots[0].path / "report.txt").touch()
    release = Event()
    original = backend.load_config

    def delayed(path):
        release.wait(5)
        return original(path)

    monkeypatch.setattr(backend, "load_config", delayed)

    async def scenario():
        app = FileFinderApp(file)
        try:
            async with app.run_test(size=(110, 40)) as pilot:
                await pilot.press("escape")
                app.query_one(Input).value = "report"
                release.set()
                await wait_until(lambda: app.navigation.view.report is not None)
                assert app.navigation.view.items[0].name == "report.txt"
                await pilot.press("down")
                assert isinstance(app.focused, OptionList)
        finally:
            release.set()

    asyncio.run(scenario())


def test_tui_returns_to_cached_folder_and_f5_updates_children(project):
    config, file = project
    root = config.roots[0].path
    child = root / "child"
    child.mkdir()

    async def scenario():
        app = FileFinderApp(file)
        async with app.run_test(size=(110, 40)) as pilot:
            await wait_until(lambda: app.config is not None)
            results = app.query_one(OptionList)
            results.focus()
            await pilot.press("enter")
            await wait_until(lambda: app.navigation.view.report is not None)
            await pilot.press("enter")
            await wait_until(lambda: app.navigation.view.directory == child and app.navigation.view.report is not None)
            (root / "new.txt").touch()
            await pilot.press("backspace")
            await wait_until(lambda: app.navigation.view.directory == root and app.navigation.view.report is not None)
            assert not any(item.name == "new.txt" for item in app.navigation.view.items)
            assert app.navigation.view.report.cached_directories == 1
            await pilot.press("f5")
            await wait_until(lambda: any(item.name == "new.txt" for item in app.navigation.view.items))
            assert app.navigation.view.report.read_directories == 1

    asyncio.run(scenario())


def test_tui_open_failure_and_file_favorite_persistence(project):
    config, file = project
    root = config.roots[0].path
    (root / "report.txt").touch()

    def failed_open(path):
        raise OSError(1155, "沒有關聯程式")

    async def scenario():
        app = FileFinderApp(file, opener=failed_open)
        async with app.run_test(size=(110, 40)) as pilot:
            await wait_until(lambda: app.config is not None)
            app.query_one(OptionList).focus()
            await pilot.press("enter")
            await wait_until(lambda: app.navigation.view.report is not None)
            await pilot.press("ctrl+f")
            await wait_until(lambda: app.favorites.contains(root / "report.txt"))
            assert Favorites(file.with_name("favorites.json")).contains(root / "report.txt")
            await pilot.press("enter")
            await pilot.click("#confirm")
            await wait_until(lambda: "無法讀取" in app.navigation.view.status)
            assert app.favorites.paths == [root / "report.txt"]

    asyncio.run(scenario())


def test_tui_invalid_root_and_invalid_favorite_are_retained(project):
    config, file = project
    config.roots[0].path.rmdir()
    lost = config.roots[0].path / "lost"
    file.with_name("favorites.json").write_text(json.dumps([str(lost)]), encoding="utf-8")

    async def scenario():
        app = FileFinderApp(file)
        async with app.run_test(size=(110, 40)) as pilot:
            await wait_until(lambda: app.config is not None)
            assert app.favorites.paths == [lost]
            assert app.navigation.view.items[0].error
            assert "無法讀取" in app.navigation.view.status
            app.query_one(OptionList).focus()
            await pilot.press("enter")
            await wait_until(lambda: app.navigation.view.report is not None)
            assert app.navigation.view.report.errors
            await pilot.press("alt+2")
            await wait_until(lambda: bool(app.navigation.view.items))
            assert app.navigation.view.items[0].path == lost
            assert app.navigation.view.items[0].error
            assert app.navigation.view.items[0].modified_text == "無法取得"
            app.query_one(OptionList).focus()
            await pilot.press("ctrl+f")
            await wait_until(lambda: not app.favorites.contains(lost))

    asyncio.run(scenario())


def test_tui_type_and_paste_from_list_or_menu(project):
    """不先點輸入框也能搜尋；第一個字、空白與 f 都必須保留。"""
    config, file = project
    target = config.roots[0].path / "file report.xlsx"
    target.touch()

    async def scenario():
        app = FileFinderApp(file)
        async with app.run_test(size=(100, 30)) as pilot:
            await wait_until(lambda: app.config is not None)
            app.query_one(OptionList).focus()
            await pilot.press("f", "i", "l", "e", "space", "r", "e", "p", "o", "r", "t")
            assert app.query_one(Input).value == "file report"
            assert isinstance(app.focused, Input)
            assert app.favorites.paths == []
            await wait_until(lambda: app.navigation.view.report is not None)
            assert [item.path for item in app.navigation.view.items] == [target]
            await pilot.press("escape")
            app.query_one(Tabs).focus()
            await pilot.press("f")
            assert app.query_one(Input).value == "f"
            await pilot.press("escape")
            app.query_one(OptionList).focus()
            # 貼上由終端送到 App，再交給目前焦點元件處理。
            app.post_message(events.Paste("file\nreport"))
            await wait_until(lambda: app.query_one(Input).value == "file report")
            await wait_until(lambda: app.navigation.view.report is not None)
            assert app.navigation.view.items[0].path == target

    asyncio.run(scenario())


def test_tabs_keep_separate_search_history_and_ignore_old_results(project, monkeypatch):
    """切分頁須取消舊搜尋，各分頁只還原自己的查詢及瀏覽歷史。"""
    config, file = project
    folder = config.roots[0].path / "2026_契變"
    folder.mkdir()
    started, release = Event(), Event()
    Favorites(file.with_name("favorites.json")).toggle(folder)
    original = backend.scan_names

    def controlled_scan(config, query, cancel, progress=None, index=None):
        if query == "old":
            started.set()
            release.wait(5)
            return ScanReport((Item(config.roots[0].path / "OLD.txt", False),), 1)
        return original(config, query, cancel, progress, index)

    monkeypatch.setattr(backend, "scan_names", controlled_scan)

    async def scenario():
        app = FileFinderApp(file)
        try:
            async with app.run_test(size=(110, 40)) as pilot:
                await wait_until(lambda: app.config is not None)
                app.query_one(Input).value = "old"
                await wait_until(started.is_set)
                old_generation = app.gate.generation
                old_cancel = app.gate.cancel
                await pilot.click("#favorites")
                await wait_until(lambda: app.active_menu == "favorites" and bool(app.navigation.view.items))
                assert old_cancel.is_set()
                app.query_one(Input).value = "契變 2026"
                await wait_until(lambda: app.navigation.view.report is not None)
                assert app.navigation.view.items[0].path == folder
                release.set()
                app.post_message(IOResult(old_generation, "search", ScanReport((Item(Path("OLD.txt"), False),), 1)))
                await pilot.pause()
                assert app.navigation.view.items[0].path == folder
                app.query_one(OptionList).focus()
                await pilot.press("enter")
                await wait_until(lambda: app.navigation.view.kind == "browse" and app.navigation.view.report is not None)
                await pilot.press("alt+1")
                await wait_until(lambda: app.active_menu == "folders" and app.navigation.view.report is not None)
                assert app.query_one(Input).value == "old"
                assert app.navigation.history == []
                await pilot.press("ctrl+tab")
                await wait_until(lambda: app.active_menu == "favorites" and app.navigation.view.report is not None)
                assert app.navigation.view.directory == folder
                assert len(app.navigation.history) == 1
                await pilot.press("alt+left")
                await wait_until(lambda: app.navigation.view.kind == "search")
                assert app.query_one(Input).value == "契變 2026"
        finally:
            release.set()

    asyncio.run(scenario())


def test_favorites_tab_filters_saved_names_opens_file_and_removes_match(project):
    """收藏檔案可直接開啟；收藏搜尋不掃描資料夾內未收藏的檔案。"""
    config, file = project
    root = config.roots[0].path
    folder = root / "2026_契變"
    folder.mkdir()
    child = folder / "2026_契變_未收藏.xlsx"
    child.touch()
    saved = root / "契變 2026.xlsx"
    saved.touch()
    unrelated = root / "契變.xlsx"
    unrelated.touch()
    store = Favorites(file.with_name("favorites.json"))
    for path in (saved, folder, unrelated):
        store.toggle(path)
    opened = []

    async def scenario():
        app = FileFinderApp(file, opener=opened.append)
        async with app.run_test(size=(110, 40)) as pilot:
            await wait_until(lambda: app.config is not None)
            await pilot.press("alt+2")
            await wait_until(lambda: len(app.navigation.view.items) == 3)
            assert app.navigation.view.items[0].path == folder
            assert all(item.modified_at is not None for item in app.navigation.view.items)
            for item, option in zip(app.navigation.view.items, app.query_one(OptionList).options):
                assert f"更新：{item.modified_text}" in str(option.prompt)
            app.query_one(Input).value = "契變 2026"
            await wait_until(lambda: app.navigation.view.report is not None)
            assert [item.path for item in app.navigation.view.items] == [folder, saved]
            results = app.query_one(OptionList)
            results.focus()
            results.highlighted = 1
            await pilot.press("enter")
            assert isinstance(app.screen, ConfirmOpen)
            assert not opened
            await pilot.click("#confirm")
            await wait_until(lambda: opened == [str(saved)])
            await pilot.press("ctrl+f")
            await wait_until(lambda: app.navigation.view.report is not None and len(app.navigation.view.items) == 1)
            assert app.navigation.view.items[0].path == folder
            assert not Favorites(store.file).contains(saved)

    asyncio.run(scenario())


def test_click_favorites_then_down_focuses_file_list(project):
    """滑鼠選取分頁後，向下鍵直接進清單；再次向下才移到下一筆。"""
    config, file = project
    store = Favorites(file.with_name("favorites.json"))
    for name in ("a.xlsx", "b.xlsx"):
        path = config.roots[0].path / name
        path.touch()
        store.toggle(path)

    async def scenario():
        app = FileFinderApp(file)
        async with app.run_test(size=(110, 40)) as pilot:
            await wait_until(lambda: app.config is not None)
            await pilot.click("#favorites")
            await wait_until(lambda: app.active_menu == "favorites" and len(app.navigation.view.items) == 2)
            await pilot.press("down")
            results = app.query_one(OptionList)
            assert app.focused is results
            assert results.highlighted == 0
            await pilot.press("down")
            assert results.highlighted == 1
            # 分頁本身保有焦點時也適用，不依賴點擊後焦點剛好位於 Input。
            app.query_one(Tabs).focus()
            await pilot.press("down")
            assert app.focused is results
            assert results.highlighted == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("menu", ["folders", "favorites"])
def test_escape_returns_one_level_and_double_escape_returns_tab_home(project, monkeypatch, menu):
    """慢速單按 Esc 逐層退回；快速連按兩次則直接回目前分頁首頁。"""
    config, file = project
    root = config.roots[0].path
    first, second = root / "first", root / "first" / "second"
    second.mkdir(parents=True)
    Favorites(file.with_name("favorites.json")).toggle(root)
    clock = [10.0]
    monkeypatch.setattr(main, "monotonic", lambda: clock[0])

    async def scenario():
        app = FileFinderApp(file)
        async with app.run_test(size=(110, 40)) as pilot:
            await wait_until(lambda: app.config is not None)
            if menu == "favorites":
                await pilot.click("#favorites")
                await wait_until(lambda: app.active_menu == menu and bool(app.navigation.view.items))
            await pilot.press("down")
            for folder in (root, first, second):
                await pilot.press("enter")
                await wait_until(lambda: app.navigation.view.directory == folder and app.navigation.view.report is not None)
            await pilot.press("escape")
            await wait_until(lambda: app.navigation.view.directory == first and app.navigation.view.report is not None)
            assert app.active_menu == menu
            assert len(app.navigation.history) == 2
            assert app.navigation.forward_history[-1].directory == second
            clock[0] += 1.0
            await pilot.press("escape")
            await wait_until(lambda: app.navigation.view.directory == root and app.navigation.view.report is not None)
            assert app.navigation.view.kind == "browse"
            # 重新深入；其他按鍵會重設連按判定。
            for folder in (first, second):
                await pilot.press("enter")
                await wait_until(lambda: app.navigation.view.directory == folder and app.navigation.view.report is not None)
            await pilot.press("escape")
            await wait_until(lambda: app.navigation.view.directory == first)
            clock[0] += 0.1
            await pilot.press("escape")
            await wait_until(lambda: app.navigation.view.kind == "home" and bool(app.navigation.view.items))
            assert app.active_menu == menu
            assert app.query_one(Tabs).active == menu
            assert app.query_one(Input).value == ""
            assert not app.navigation.history
            assert not app.navigation.forward_history

    asyncio.run(scenario())


def test_escape_restores_search_and_intervening_key_cancels_double_escape(project, monkeypatch):
    """單次返回保留搜尋與選取；兩次 Esc 中有其他按鍵不算連按。"""
    config, file = project
    parent = config.roots[0].path / "report"
    child = parent / "child"
    child.mkdir(parents=True)
    clock = [10.0]
    monkeypatch.setattr(main, "monotonic", lambda: clock[0])

    async def scenario():
        app = FileFinderApp(file)
        async with app.run_test(size=(110, 40)) as pilot:
            await wait_until(lambda: app.config is not None)
            app.query_one(Input).value = "report"
            await wait_until(lambda: app.navigation.view.report is not None)
            await pilot.press("down", "enter")
            await wait_until(lambda: app.navigation.view.directory == parent and app.navigation.view.report is not None)
            await pilot.press("enter")
            await wait_until(lambda: app.navigation.view.directory == child and app.navigation.view.report is not None)
            await pilot.press("escape")
            await wait_until(lambda: app.navigation.view.directory == parent)
            await pilot.press("ctrl+l")
            clock[0] += 0.1
            await pilot.press("escape")
            assert app.navigation.view.kind == "search"
            assert app.query_one(Input).value == "report"
            assert app.navigation.view.items[0].path == parent
            assert app.navigation.view.selected == 0
            assert app.navigation.forward_history
            clock[0] += 1.0
            await pilot.press("escape")
            await wait_until(lambda: app.navigation.view.kind == "home" and bool(app.navigation.view.items))

    asyncio.run(scenario())


def test_folder_path_only_in_header_and_search_paths_still_distinguish_names(project):
    """資料夾內省略重複路徑；跨目錄搜尋仍可區分相同檔名。"""
    config, file = project
    for root in config.roots[:2]:
        (root.path / "report.xlsx").touch()

    async def scenario():
        app = FileFinderApp(file)
        async with app.run_test(size=(110, 40)) as pilot:
            await wait_until(lambda: app.config is not None)
            await pilot.press("down", "enter")
            await wait_until(lambda: app.navigation.view.kind == "browse" and app.navigation.view.report is not None)
            root = config.roots[0].path
            assert str(root) in str(app.query_one("#location", Static).render())
            prompt = str(app.query_one(OptionList).get_option_at_index(0).prompt)
            assert "report.xlsx" in prompt and "更新：" in prompt
            assert str(root) not in prompt
            app.query_one(Input).value = "report"
            await wait_until(lambda: app.navigation.view.kind == "search" and app.navigation.view.report is not None)
            assert len(app.navigation.view.items) == 2
            for item, option in zip(app.navigation.view.items, app.query_one(OptionList).options):
                assert str(item.path.parent) in str(option.prompt)

    asyncio.run(scenario())
