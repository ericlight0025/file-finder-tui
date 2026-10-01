"""驗證搜尋與瀏覽共用索引、取消／錯誤保護及並行讀取。"""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

import backend
from backend import DirectoryIndex, Favorites, browse_folder, list_home_items, scan_names


def record_scans(monkeypatch):
    """計算真正的資料夾枚舉次數，而非查詢函式呼叫次數。"""
    calls = Counter()
    original = backend.os.scandir

    def counted(directory):
        calls[Path(directory)] += 1
        return original(directory)

    monkeypatch.setattr(backend.os, "scandir", counted)
    return calls


def test_queries_browse_and_home_reuse_full_index_beyond_result_limit(project, monkeypatch):
    config, config_file = project
    root = config.roots[0].path
    for number in range(230):
        (root / f"item{number:03}.txt").touch()
    needle = root / "needle.xlsx"
    needle.touch()
    calls = record_scans(monkeypatch)
    index = DirectoryIndex()
    first = scan_names(config, "item", index=index)
    assert first.total == 230 and len(first.items) == 200
    before = calls.copy()
    second = scan_names(config, "needle", index=index)
    assert second.total == 1 and second.items[0].path == needle
    assert second.read_directories == 0 and second.cached_directories == 3
    assert browse_folder(root, index=index).total == 231
    list_home_items(config, Favorites(config_file.with_name("favorites.json")), Event(), index=index)
    assert calls == before
    assert all(count == 1 for count in calls.values())


def test_browse_first_then_search_only_reads_unseen_directories(project, monkeypatch):
    config, _ = project
    root = config.roots[0].path
    child = root / "child"
    child.mkdir()
    (child / "report.xlsx").touch()
    calls = record_scans(monkeypatch)
    index = DirectoryIndex()
    browse_folder(root, index=index)
    report = scan_names(config, "report", index=index)
    assert report.total == 1
    assert report.cached_directories == 1 and report.read_directories == 3
    assert calls[root] == 1 and calls[child] == 1
    before = calls.copy()
    browse_folder(child, index=index)
    assert calls == before


def test_cancelled_directory_is_retried_but_completed_directories_are_reused(project, monkeypatch):
    config, _ = project
    for root in config.roots:
        (root.path / "report.txt").touch()
    calls = record_scans(monkeypatch)
    original = backend.entry_item
    cancel = Event()

    def cancel_second_root(entry):
        item = original(entry)
        if Path(entry.path).parent == config.roots[1].path:
            cancel.set()
        return item

    monkeypatch.setattr(backend, "entry_item", cancel_second_root)
    index = DirectoryIndex()
    assert scan_names(config, "report", cancel, index=index).cancelled
    monkeypatch.setattr(backend, "entry_item", original)
    report = scan_names(config, "report", index=index)
    assert report.complete and report.total == 3
    assert calls[config.roots[0].path] == 1
    assert calls[config.roots[1].path] == 2
    assert report.cached_directories == 1


def test_entry_error_is_not_saved_as_a_complete_index(project, monkeypatch):
    config, _ = project
    target = config.roots[0].path / "report.txt"
    target.touch()
    calls = record_scans(monkeypatch)
    original = backend.entry_item

    def denied(entry):
        if Path(entry.path) == target:
            raise PermissionError("模擬項目讀取失敗")
        return original(entry)

    monkeypatch.setattr(backend, "entry_item", denied)
    index = DirectoryIndex()
    assert scan_names(config, "report", index=index).errors
    monkeypatch.setattr(backend, "entry_item", original)
    report = scan_names(config, "report", index=index)
    assert report.complete and report.total == 1
    assert calls[config.roots[0].path] == 2
    assert calls[config.roots[1].path] == calls[config.roots[2].path] == 1


def test_scan_limit_does_not_save_partial_snapshot_or_destroy_full_snapshot(project, monkeypatch):
    config, _ = project
    root = config.roots[0].path
    for number in range(6):
        (root / f"report{number}.txt").touch()
    calls = record_scans(monkeypatch)
    index = DirectoryIndex()
    limited = replace(config, max_scan_entries=3)
    report = scan_names(limited, "report", index=index)
    assert report.limited and report.scanned == 3
    assert scan_names(config, "report", index=index).total == 6
    assert calls[root] == 2
    before = calls.copy()
    report = scan_names(limited, "report", index=index)
    assert report.limited and report.scanned == 3
    assert browse_folder(root, index=index).total == 6
    assert calls == before


def test_refresh_invalidates_only_selected_directory(project, monkeypatch):
    config, _ = project
    root = config.roots[0].path
    calls = record_scans(monkeypatch)
    index = DirectoryIndex()
    scan_names(config, "new", index=index)
    target = root / "new.xlsx"
    target.touch()
    assert scan_names(config, "new", index=index).total == 0
    index.invalidate(root)
    report = scan_names(config, "new", index=index)
    assert report.total == 1 and report.items[0].path == target
    assert calls[root] == 2
    assert calls[config.roots[1].path] == calls[config.roots[2].path] == 1


@pytest.mark.parametrize("refresh", [False, True])
def test_concurrent_reads_share_snapshot_and_refresh_blocks_old_cache_write(project, monkeypatch, refresh):
    config, _ = project
    root = config.roots[0].path
    (root / "report.txt").touch()
    calls = record_scans(monkeypatch)
    index = DirectoryIndex()
    reading, release, queued = Event(), Event(), Event()
    original = backend.entry_item

    def blocked(entry):
        if not reading.is_set():
            reading.set()
            assert release.wait(5)
        return original(entry)

    def second_read():
        queued.set()
        return browse_folder(root, index=index)

    monkeypatch.setattr(backend, "entry_item", blocked)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(browse_folder, root, index=index)
        try:
            assert reading.wait(5)
            if refresh:
                index.invalidate(root)
            second = executor.submit(second_read)
            assert queued.wait(5)
        finally:
            release.set()
        assert first.result(timeout=5).complete
        report = second.result(timeout=5)
        assert report.complete
        assert calls[root] == (2 if refresh else 1)
        assert report.cached_directories == (0 if refresh else 1)
