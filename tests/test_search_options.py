"""搜尋範圍、篩選排序及更新選取的回歸測試；只使用暫存資料。"""

import asyncio
import os
from dataclasses import replace
from threading import Event

import pytest
from textual.widgets import Input, Select, Static

from backend import DirectoryIndex, Favorites, Item, SearchOptions, browse_folder, scan_names, search_favorites, search_key
from main import FileFinderApp, FileList, View


async def wait_until(predicate):
    """等待真實背景工作回報，避免以固定睡眠判斷完成。"""
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("kind,names", [
    ("all", {"report-folder", "report.SQL", "report.docx", "report.XLSM", "report.txt"}),
    ("files", {"report.SQL", "report.docx", "report.XLSM", "report.txt"}),
    ("folders", {"report-folder"}),
    ("sql", {"report.SQL"}),
    ("word", {"report.docx"}),
    ("excel", {"report.XLSM"}),
])
def test_file_types_use_complete_index_and_case_insensitive_extensions(project, kind, names):
    """篩選不縮減快取，下一個條件仍能找到其他類型。"""
    config, _ = project
    root = config.roots[0].path
    (root / "report-folder").mkdir()
    for name in ("report.SQL", "report.docx", "report.XLSM", "report.txt"):
        (root / name).touch()
    index = DirectoryIndex()
    report = scan_names(config, "report", index=index, options=SearchOptions(kind))
    assert {item.name for item in report.items} == names
    assert report.total == len(names)
    assert browse_folder(root, index=index).total == 5


def test_filter_and_time_sort_happen_before_result_limit(project):
    """大量資料夾不能擠掉 SQL；最新項目必須在取前 200 筆前排序。"""
    config, _ = project
    root = config.roots[0].path
    for number in range(215):
        (root / f"report-folder{number:03}").mkdir()
        path = root / f"report{number:03}.sql"
        path.touch()
        os.utime(path, (1_700_000_000 + number, 1_700_000_000 + number))
    report = scan_names(config, "report", options=SearchOptions("sql", "modified"))
    assert report.complete and report.total == 215 and len(report.items) == 200
    assert report.items[0].name == "report214.sql"
    assert report.items[-1].name == "report015.sql"
    assert all(not item.is_dir for item in report.items)


def test_local_scope_does_not_visit_other_roots_and_recursion_reuses_cache(project, monkeypatch):
    """目前資料夾只查直接內容；遞迴才展開子資料夾，保留完整快取。"""
    config, _ = project
    directory = config.roots[0].path / "report-scope"
    child = directory / "child"
    child.mkdir(parents=True)
    direct, deep = directory / "report.sql", child / "report.sql"
    direct.touch()
    deep.touch()
    (config.roots[1].path / "report.sql").touch()
    import backend
    original = backend.os.scandir
    calls = []

    def counted(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(backend.os, "scandir", counted)
    index = DirectoryIndex()
    direct_report = scan_names(config, "report", index=index, directory=directory, recursive=False)
    assert {item.path for item in direct_report.items} == {direct}
    assert calls == [directory]
    calls.clear()
    recursive_report = scan_names(config, "report", index=index, directory=directory)
    assert {item.path for item in recursive_report.items} == {direct, deep}
    assert calls == [child]
    assert recursive_report.cached_directories == 1


def test_scoped_search_keeps_cancel_and_scan_limit(project):
    """局部搜尋仍遵守取消與掃描上限，不宣稱未掃完的結果完整。"""
    config, _ = project
    root = config.roots[0].path
    for number in range(5):
        (root / f"report{number}.sql").touch()
    report = scan_names(replace(config, max_scan_entries=2), "report", directory=root, recursive=False)
    assert report.limited and report.scanned == 2 and not report.complete
    cancel = Event()
    cancel.set()
    assert scan_names(config, "report", cancel, directory=root).cancelled


def test_time_sort_keeps_folders_first_and_unknown_times_last(project):
    """資料夾優先不因時間排序改變，未知時間不被誤認為最新。"""
    config, _ = project
    root = config.roots[0].path
    items = (Item(root / "old-dir", True, modified_at=1),
             Item(root / "new.sql", False, modified_at=100),
             Item(root / "unknown.sql", False),
             Item(root / "old.sql", False, modified_at=2))
    ordered = sorted(items, key=lambda item: search_key("", item, "modified"))
    assert [item.name for item in ordered] == ["old-dir", "new.sql", "old.sql", "unknown.sql"]


def test_favorites_use_same_filter_and_sort(project):
    """最愛名稱搜尋同樣支援類型，不擴充成收藏資料夾內的全域搜尋。"""
    config, file = project
    favorites = Favorites(file.with_name("favorites.json"))
    root = config.roots[0].path
    for index, name in enumerate(("report.sql", "report-new.sql", "report.docx")):
        path = root / name
        path.touch()
        os.utime(path, (100 + index, 100 + index))
        favorites.toggle(path)
    report = search_favorites(favorites, "report", Event(), options=SearchOptions("sql", "modified"))
    assert [item.name for item in report.items] == ["report-new.sql", "report.sql"]


def test_ui_refresh_retains_path_after_insert_and_reports_removed_selection(project):
    """真實 F5 重排後保持同一檔案；原檔案消失則明確提示。"""
    config, file = project
    root = config.roots[0].path
    target = root / "z-report.sql"
    target.touch()

    async def scenario():
        app = FileFinderApp(file)
        async with app.run_test(size=(80, 30)) as pilot:
            await wait_until(lambda: app.config is not None)
            await pilot.press("down", "enter")
            await wait_until(lambda: app.navigation.view.report is not None)
            assert app.navigation.view.items[app.navigation.view.selected].path == target
            (root / "a-report.sql").touch()
            await pilot.press("f5")
            await wait_until(lambda: len(app.navigation.view.items) == 2 and app.navigation.view.report is not None)
            assert app.navigation.view.selected == 1
            assert app.navigation.view.items[1].path == target
            target.unlink()
            await pilot.press("f5")
            await wait_until(lambda: len(app.navigation.view.items) == 1 and app.navigation.view.report is not None)
            assert "原選取項目已不在清單" in app.navigation.view.status

    asyncio.run(scenario())


def test_changing_filter_discards_running_result_for_same_query(project, monkeypatch):
    """即使文字未改，切換類型也不能被上一個慢查詢覆蓋。"""
    import backend
    config, file = project
    target = config.roots[0].path / "report.sql"
    target.touch()
    started, release, finished = Event(), Event(), Event()
    original = backend.scan_names

    def slow_scan(config, query, cancel, progress=None, index=None, **kwargs):
        if kwargs.get("options", SearchOptions()).kind == "sql":
            return original(config, query, cancel, progress, index, **kwargs)
        started.set()
        release.wait(5)
        finished.set()
        return backend.ScanReport((Item(target.with_suffix(".docx"), False),), 1)

    monkeypatch.setattr(backend, "scan_names", slow_scan)

    async def scenario():
        app = FileFinderApp(file)
        try:
            async with app.run_test(size=(80, 30)) as pilot:
                await wait_until(lambda: app.config is not None)
                app.query_one(Input).value = "report"
                await wait_until(started.is_set)
                app.query_one("#kind-select", Select).value = "sql"
                await wait_until(lambda: app.navigation.view.report is not None)
                assert [item.path for item in app.navigation.view.items] == [target]
                release.set()
                await wait_until(finished.is_set)
                await pilot.pause()
                assert [item.path for item in app.navigation.view.items] == [target]
        finally:
            release.set()

    asyncio.run(scenario())


def test_empty_favorites_filter_does_not_claim_favorites_are_missing(project):
    """篩選無命中時保留收藏，避免誤導使用者重新收藏。"""
    config, file = project
    path = config.roots[0].path / "report.docx"
    path.touch()
    favorites = Favorites(file.with_name("favorites.json"))
    favorites.toggle(path)

    async def scenario():
        app = FileFinderApp(file)
        async with app.run_test(size=(80, 30)) as pilot:
            await wait_until(lambda: app.config is not None)
            await pilot.press("alt+2")
            await wait_until(lambda: len(app.navigation.view.items) == 1)
            app.query_one("#kind-select", Select).value = "sql"
            await wait_until(lambda: app.navigation.view.options.kind == "sql" and not app.navigation.view.items)
            assert "目前類型篩選沒有符合的最愛" in app.navigation.view.status
            assert app.favorites.contains(path)

    asyncio.run(scenario())


def test_ui_local_scope_clear_escape_and_global_switch(project):
    """選單限制範圍；清空及單次 Esc 回資料夾，切全域後仍能找其他根目錄。"""
    config, file = project
    root = config.roots[0].path
    child = root / "child"
    child.mkdir()
    (root / "report.sql").touch()
    (child / "report-child.sql").touch()
    (config.roots[1].path / "report-other.sql").touch()

    async def scenario():
        app = FileFinderApp(file)
        async with app.run_test(size=(80, 30)) as pilot:
            await wait_until(lambda: app.config is not None)
            assert app.query_one("#scope-select", Select).disabled
            await pilot.press("down", "enter")
            await wait_until(lambda: app.navigation.view.report is not None)
            app.query_one("#scope-select", Select).value = "current"
            await pilot.pause()
            app.query_one(Input).value = "report"
            await wait_until(lambda: app.navigation.view.kind == "search" and app.navigation.view.report is not None)
            assert [item.name for item in app.navigation.view.items] == ["report.sql"]
            app.query_one("#scope-select", Select).value = "recursive"
            await wait_until(lambda: app.navigation.view.report is not None and len(app.navigation.view.items) == 2)
            app.query_one(Input).value = ""
            await wait_until(lambda: app.navigation.view.kind == "browse" and app.navigation.view.report is not None)
            assert app.navigation.view.directory == root
            app.query_one(Input).value = "report"
            await wait_until(lambda: app.navigation.view.kind == "search" and app.navigation.view.report is not None)
            await pilot.press("escape")
            await wait_until(lambda: app.navigation.view.kind == "browse")
            app.query_one(Input).value = "report"
            await wait_until(lambda: app.navigation.view.kind == "search" and app.navigation.view.report is not None)
            app.query_one("#scope-select", Select).value = "all"
            await wait_until(lambda: app.navigation.view.report is not None and len(app.navigation.view.items) == 3)
            assert "設定的根目錄" in str(app.query_one("#location", Static).render())

    asyncio.run(scenario())


def test_ui_filter_sort_controls_tab_isolation_and_keyboard_layout(project):
    """下拉操作不把按鍵送進搜尋；條件在兩分頁各自保存，窄視窗可閱讀。"""
    config, file = project
    root = config.roots[0].path
    for index, name in enumerate(("report-old.sql", "report-new.sql", "report.docx")):
        path = root / name
        path.touch()
        os.utime(path, (100 + index, 100 + index))

    async def scenario():
        app = FileFinderApp(file)
        async with app.run_test(size=(80, 30)) as pilot:
            await wait_until(lambda: app.config is not None)
            app.query_one(Input).value = "report"
            await wait_until(lambda: app.navigation.view.report is not None)
            assert await pilot.click("#kind-select SelectCurrent")
            await pilot.press("home", "down", "down", "down", "enter")
            await wait_until(lambda: app.navigation.view.options.kind == "sql" and app.navigation.view.report is not None)
            assert app.query_one(Input).value == "report"
            app.query_one("#sort-select", Select).value = "modified"
            await wait_until(lambda: app.navigation.view.options.sort == "modified" and app.navigation.view.report is not None and app.navigation.view.items[0].name == "report-new.sql")
            await pilot.press("alt+2")
            await pilot.pause()
            assert app.query_one("#kind-select", Select).value == "all"
            await pilot.press("alt+1")
            await pilot.pause()
            assert app.query_one("#kind-select", Select).value == "sql"
            assert app.query_one("#sort-select", Select).value == "modified"
            for width, height in ((80, 30), (70, 24)):
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                for selector in app.query("#search-tools Select"):
                    assert selector.region.right <= width
                assert app.query_one(FileList).scrollable_content_region.height >= 4

    asyncio.run(scenario())
