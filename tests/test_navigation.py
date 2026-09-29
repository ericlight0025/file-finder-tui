"""驗證 Lazy Loading、歷史與 Textual 真實事件迴圈操作。"""

import asyncio
from pathlib import Path
from threading import Event

import pytest
from textual.widgets import Input, OptionList

import main
from main import Config, ConfirmOpen, Favorites, FileFinderApp, IOResult, Item, Navigation, Root, ScanReport, View, browse_folder


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

    monkeypatch.setattr(main.os, "scandir", denied)
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
            app.query_one(Input).value = "rpt"
            await wait_until(lambda: app.navigation.view.report is not None)
            assert app.navigation.view.items[0].path == deep
            app.query_one(OptionList).focus()
            await pilot.press("enter")
            await wait_until(lambda: app.navigation.view.kind == "browse" and app.navigation.view.report is not None)
            assert app.query_one(Input).value == ""
            assert app.navigation.view.items[0].name == "child.txt"
            await pilot.press("backspace")
            assert app.navigation.view.kind == "search"
            assert app.query_one(Input).value == "rpt"
            assert app.navigation.view.items[0].path == deep
            await pilot.press("alt+right")
            await wait_until(lambda: app.navigation.view.kind == "browse" and app.navigation.view.directory == deep)
            assert app.query_one(Input).value == ""
            await pilot.press("alt+left")
            await wait_until(lambda: app.navigation.view.kind == "search")
            await pilot.press("ctrl+l", "backspace")
            await pilot.pause()
            assert app.query_one(Input).value == "rp"
            await pilot.press("escape")
            await wait_until(lambda: app.navigation.view.kind == "home" and len(app.navigation.view.items) > 0)
            assert app.query_one(Input).value == ""
            assert app.navigation.history == []

    asyncio.run(scenario())


def test_tui_debounce_responsiveness_and_stale_result_guard(project, monkeypatch):
    _, file = project
    started, release, old_cancelled = Event(), Event(), Event()
    calls = []

    def slow_scan(config, query, cancel):
        calls.append(query)
        if query == "old":
            started.set()
            release.wait(5)
            if cancel.is_set():
                old_cancelled.set()
            return ScanReport((Item(config.roots[0].path / "OLD.txt", False),), 1)
        return ScanReport((Item(config.roots[0].path / "NEW.txt", False),), 1)

    monkeypatch.setattr(main, "scan_names", slow_scan)

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
            assert app.navigation.view.items[1].path == deep
            results = app.query_one(OptionList)
            results.focus()
            results.highlighted = 1
            await pilot.press("enter")
            await wait_until(lambda: app.navigation.view.report is not None)
            await pilot.press("backspace")
            await wait_until(lambda: app.navigation.view.kind == "home" and bool(app.navigation.view.items))
            assert app.navigation.view.selected == 1
            results.highlighted = 1
            await pilot.press("f")
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
            await pilot.press("escape")
            assert opened == []
            await pilot.press("enter")
            await pilot.click("#confirm")
            await wait_until(lambda: len(opened) == 1)
            assert opened[0] == str(executable)
            results.highlighted = 1
            await pilot.press("enter")
            await wait_until(lambda: len(opened) == 2)
            assert opened[1] == str(ordinary)

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


def test_tui_mouse_click_enters_folder(project):
    config, file = project

    async def scenario():
        app = FileFinderApp(file)
        async with app.run_test(size=(110, 40)) as pilot:
            await wait_until(lambda: app.config is not None)
            await pilot.pause()
            # 前兩列是最愛與根目錄標題；每個根目錄使用兩行。
            clicked = await pilot.click("#results", offset=(5, 3))
            assert clicked
            await wait_until(lambda: app.navigation.view.kind == "browse" and app.navigation.view.report is not None)
            assert app.navigation.view.directory == config.roots[0].path
            await pilot.press("backspace")
            await wait_until(lambda: app.navigation.view.kind == "home" and bool(app.navigation.view.items))

    asyncio.run(scenario())


def test_startup_input_and_escape_do_not_cancel_initialization(project, monkeypatch):
    config, file = project
    (config.roots[0].path / "report.txt").touch()
    release = Event()
    original = main.load_config

    def delayed(path):
        release.wait(5)
        return original(path)

    monkeypatch.setattr(main, "load_config", delayed)

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


def test_tui_returns_to_folder_by_reading_current_children(project):
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
            await wait_until(lambda: any(item.name == "new.txt" for item in app.navigation.view.items))
            assert app.navigation.view.directory == root

    asyncio.run(scenario())


def test_tui_open_failure_and_file_favorite_rejection(project):
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
            await pilot.press("f")
            assert "不能收藏單一檔案" in app.navigation.view.status
            await pilot.press("enter")
            await wait_until(lambda: "無法讀取" in app.navigation.view.status)
            assert app.favorites.paths == []

    asyncio.run(scenario())


def test_tui_invalid_root_and_invalid_favorite_are_retained(project):
    config, file = project
    config.roots[0].path.rmdir()
    lost = config.roots[0].path / "lost"
    file.with_name("favorites.json").write_text(main.json.dumps([str(lost)]), encoding="utf-8")

    async def scenario():
        app = FileFinderApp(file)
        async with app.run_test(size=(110, 40)) as pilot:
            await wait_until(lambda: app.config is not None)
            assert app.favorites.paths == [lost]
            assert app.navigation.view.items[1].error
            assert "無法讀取" in app.navigation.view.status
            app.query_one(OptionList).focus()
            await pilot.press("enter")
            await wait_until(lambda: app.navigation.view.report is not None)
            assert app.navigation.view.report.errors

    asyncio.run(scenario())
