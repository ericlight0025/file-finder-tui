"""驗證虛擬清單、誤開防護與啟動後索引核對。"""

import asyncio
from pathlib import Path
from threading import Event

from textual.widgets import Input, OptionList, Static

import backend
from backend import DirectoryIndex, Item
from main import ConfirmOpen, FileFinderApp, FileList, View


async def wait_until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


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
            assert results.virtual_size.height == 20001 * 3
            results.focus()
            await pilot.press("end")
            assert results.highlighted == 20000
            await pilot.pause()
            assert "last.txt" in "".join(results.render_line(y).text for y in range(results.scrollable_content_region.height))
            assert len(results.option_cache) < 128
            await pilot.press("pageup")
            assert results.highlighted < 20000
            await pilot.press("end", "enter")
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
