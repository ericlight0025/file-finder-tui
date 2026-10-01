"""驗證虛擬清單、誤開防護與啟動後索引核對。"""

import asyncio
from pathlib import Path
from threading import Event

from textual.widgets import Collapsible, Input, OptionList, Select, Static
from textual.color import Color

import backend
from backend import DirectoryIndex, Item
from main import ConfirmOpen, FileFinderApp, FileList, THEME_OPTIONS, View


async def wait_until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def test_status_update_after_widget_unmount_is_safe(project):
    """畫面已移除後，背景保存回報不可因找不到狀態元件而當機。"""
    _, file = project

    async def scenario():
        app = FileFinderApp(file)
        async with app.run_test() as pilot:
            await wait_until(lambda: app.config is not None)
            await app.query_one("#status", Static).remove()
            app.set_status("背景保存完成")
            await pilot.pause()

    asyncio.run(scenario())


def test_theme_dropdown_changes_palette_without_resetting_view(project, monkeypatch):
    """下拉切換三套主題，畫面內容與搜尋文字不重設。"""
    config, file = project
    # 模擬彩色終端啟動，避免 NO_COLOR 把主題轉成灰階。
    monkeypatch.delenv("NO_COLOR", raising=False)
    root = config.roots[0].path
    item = Item(root / "report.txt", False, modified_at=1700000000)

    async def scenario():
        app = FileFinderApp(file)
        assert not app.no_color
        async with app.run_test(size=(110, 40)) as pilot:
            await wait_until(lambda: app.config is not None)
            selector = app.query_one("#theme-select", Select)
            assert selector.value == THEME_OPTIONS[0][1].name
            app.set_input("report")
            app.navigation.view = View("browse", directory=root, items=(item,))
            app.render_view()
            results = app.query_one(FileList)
            for index in (1, 2, 0):
                assert await pilot.click("#theme-select SelectCurrent")
                await pilot.press("home", *(["down"] * index), "enter")
                await pilot.pause()
                theme = THEME_OPTIONS[index][1]
                assert app.theme == theme.name
                assert app.screen.styles.background == Color.parse(theme.background)
                assert results.styles.background == Color.parse(theme.surface)
                selected_style = results.get_visual_style("option-list--option", "option-list--option-highlighted").rich_style
                assert selected_style.bgcolor.get_truecolor() == tuple(Color.parse(theme.variables["block-cursor-background"]).rgb)
                assert not selected_style.bold
                assert app.query_one(Input).value == "report"
                assert results.items == (item,) and results.highlighted == 0
                assert app.navigation.view.directory == root
                assert app.query_one("#help", Collapsible).collapsed
            # 下拉選單可縮入較窄視窗，且不擋住分頁。
            await pilot.resize_terminal(70, 30)
            await pilot.pause()
            assert selector.region.right <= app.screen.size.width
            assert app.query_one("#menu").region.right <= selector.region.x

    asyncio.run(scenario())


def test_help_starts_collapsed_and_can_toggle(project):
    """說明預設隱藏，展開後顯示最新狀態，並可再次收合。"""
    _, file = project

    async def scenario():
        app = FileFinderApp(file)
        async with app.run_test(size=(110, 40)) as pilot:
            await wait_until(lambda: app.config is not None)
            help_panel = app.query_one("#help", Collapsible)
            await pilot.pause()
            assert help_panel.collapsed
            assert app.query_one("#keys").region.height == 0
            assert await pilot.click("#help > CollapsibleTitle")
            await pilot.pause()
            assert not help_panel.collapsed
            assert app.query_one("#status").region.height > 0
            assert app.query_one("#keys").region.height > 0
            assert await pilot.click("#help > CollapsibleTitle")
            await pilot.pause()
            assert help_panel.collapsed

    asyncio.run(scenario())


def test_name_and_modified_time_share_row_after_resize(project):
    """長中文檔名截斷後，時間緊接固定寬名稱欄，縮放時保持對齊。"""
    config, file = project
    root = config.roots[0].path
    item = Item(root / ("很長的中文檔案名稱" * 8 + ".txt"), False, modified_at=1700000000)

    async def scenario():
        app = FileFinderApp(file)
        async with app.run_test(size=(110, 40)) as pilot:
            await wait_until(lambda: app.config is not None)
            app.navigation.view = View("browse", directory=root, items=(item,))
            app.render_view()
            results = app.query_one(FileList)
            for width in (110, 70):
                await pilot.resize_terminal(width, 40)
                await pilot.pause()
                line = results.render_line(0).text
                assert "很長" in line and "…" in line
                assert line.rstrip().endswith(f"更新：{item.modified_text}")
                from rich.cells import cell_len
                available = results.scrollable_content_region.width - 2
                expected_name_width = min(36, available - cell_len(f"更新：{item.modified_text}") - 2)
                assert cell_len(line.split("更新：", 1)[0]) == 1 + expected_name_width + 2
                assert results.row_height == 2

    asyncio.run(scenario())


def test_large_list_formats_only_visible_items_and_keeps_last_item_reachable(project):
    config, file = project
    root = config.roots[0].path
    target = root / "last.txt"
    target.touch()
    items = tuple(Item(root / f"item{number:05}.txt", False) for number in range(20000)) + (Item(target, False),)
    opened = []

    async def scenario():
        app = FileFinderApp(file, opener=opened.append)
        async with app.run_test(size=(110, 40)) as pilot:
            await wait_until(lambda: app.config is not None)
            app.navigation.view = View("browse", directory=root, items=items)
            app.render_view()
            results = app.query_one(FileList)
            assert results.option_count == 20001
            await pilot.pause()
            assert len(results.option_cache) < 128
            assert results.virtual_size.height == 20001 * 2
            results.focus()
            await pilot.press("end")
            assert results.highlighted == 20000
            await pilot.pause()
            assert "last.txt" in "".join(results.render_line(y).text for y in range(results.scrollable_content_region.height))
            assert len(results.option_cache) < 128
            await pilot.press("pageup")
            assert results.highlighted < 20000
            await pilot.press("end", "enter")
            assert isinstance(app.screen, ConfirmOpen)
            assert not opened
            await pilot.click("#confirm")
            await wait_until(lambda: bool(opened))
            assert opened == [str(target)]
            await pilot.press("ctrl+l")
            assert isinstance(app.focused, Input)

    asyncio.run(scenario())


def test_single_click_does_not_execute_and_double_click_still_confirms(project):
    config, file = project
    root = config.roots[0].path
    executable = root / "run.exe"
    executable.touch()
    opened = []

    async def scenario():
        app = FileFinderApp(file, opener=opened.append)
        async with app.run_test(size=(110, 40)) as pilot:
            await wait_until(lambda: app.config is not None)
            await pilot.press("down", "enter")
            await wait_until(lambda: app.navigation.view.report is not None)
            assert await pilot.click("#results", offset=(5, 1))
            assert not isinstance(app.screen, ConfirmOpen) and not opened
            assert await pilot.double_click("#results", offset=(5, 1))
            assert isinstance(app.screen, ConfirmOpen)
            assert not opened
            await pilot.press("escape")
            await pilot.press("ctrl+o")
            await wait_until(lambda: bool(opened))
            assert opened == [str(root)]

    asyncio.run(scenario())


def test_f5_search_reuses_unchanged_directories_but_ctrl_f5_rebuilds(project, monkeypatch):
    config, file = project
    for root in config.roots:
        (root.path / "report.txt").touch()
    calls = []
    original = backend.os.scandir

    def counted(path):
        calls.append(Path(path))
        return original(path)

    monkeypatch.setattr(backend.os, "scandir", counted)

    async def scenario():
        app = FileFinderApp(file)
        async with app.run_test(size=(110, 40)) as pilot:
            await wait_until(lambda: app.config is not None)
            app.query_one(Input).value = "report"
            await wait_until(lambda: app.navigation.view.report is not None)
            calls.clear()
            await pilot.press("f5")
            await wait_until(lambda: app.navigation.view.report is not None)
            assert not calls
            (config.roots[1].path / "report-new.txt").touch()
            await pilot.press("f5")
            await wait_until(lambda: app.navigation.view.report is not None)
            assert app.navigation.view.report.total == 4
            assert calls == [config.roots[1].path]
            calls.clear()
            await pilot.press("ctrl+f5")
            await wait_until(lambda: app.navigation.view.report is not None)
            assert set(calls) == {root.path for root in config.roots}

    asyncio.run(scenario())


def test_restart_background_check_updates_current_query_after_nested_change(project, monkeypatch):
    config, file = project
    root = config.roots[0].path
    child = root / "child"
    child.mkdir()
    index = DirectoryIndex(file.with_name(".file-finder-index.sqlite3"))
    backend.scan_names(config, "new", index=index)
    index.flush()
    target = child / "new.txt"
    target.touch()
    started, release = Event(), Event()
    original = DirectoryIndex.check_changes

    def delayed(self, cancel, *args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(self, cancel, *args, **kwargs)

    monkeypatch.setattr(DirectoryIndex, "check_changes", delayed)

    async def scenario():
        app = FileFinderApp(file)
        try:
            async with app.run_test(size=(110, 40)) as pilot:
                await wait_until(started.is_set)
                assert app.index_checking
                assert "背景核對" in str(app.query_one("#status", Static).render())
                app.query_one(Input).value = "new"
                await wait_until(lambda: app.navigation.view.report is not None)
                assert not app.navigation.view.items
                release.set()
                await wait_until(lambda: not app.index_checking and app.navigation.view.report is not None and app.navigation.view.report.total == 1)
                assert app.navigation.view.items[0].path == target
        finally:
            release.set()

    asyncio.run(scenario())
