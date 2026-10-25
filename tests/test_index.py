"""驗證搜尋與瀏覽共用索引、取消／錯誤保護及並行讀取。"""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

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
    target = root / "new.xlsx"
    target.touch()
    calls = record_scans(monkeypatch)
    index = DirectoryIndex()
    scan_names(config, "new", index=index)
    index.invalidate(root)
    report = scan_names(config, "new", index=index)
    assert report.total == 1 and report.items[0].path == target
    assert report.read_directories == 1 and report.cached_directories == 2
    assert calls[root] == 2
    assert calls[config.roots[1].path] == calls[config.roots[2].path] == 1


def test_created_and_deleted_files_in_scanned_folder_are_seen_without_f5(project, monkeypatch):
    """同一份索引再次搜尋時核對目錄本身；只重讀變動的資料夾，結果不因舊快取而少列。"""
    config, _ = project
    root = config.roots[0].path
    child = root / "child"
    child.mkdir()
    (child / "old.txt").touch()
    calls = record_scans(monkeypatch)
    index = DirectoryIndex()
    assert scan_names(config, "report", index=index).total == 0
    target = child / "report.txt"
    target.touch()
    report = scan_names(config, "report", index=index)
    assert report.complete and report.total == 1 and report.items[0].path == target
    assert "命中總數 1 筆" in report.status()
    assert calls[child] == 2 and calls[root] == 1
    assert report.read_directories == 1 and report.cached_directories == 3
    target.unlink()
    report = scan_names(config, "report", index=index)
    assert report.complete and report.total == 0
    assert calls[child] == 3 and calls[root] == 1
    assert not any(item.name == "report.txt" for item in browse_folder(child, index=index).items)
    assert calls[child] == 3


def test_changed_snapshot_is_replaced_and_reused_afterwards(project, monkeypatch):
    config, _ = project
    root = config.roots[0].path
    index = DirectoryIndex()
    browse_folder(root, index=index)
    (root / "report.txt").touch()
    calls = record_scans(monkeypatch)
    assert browse_folder(root, index=index).total == 1
    assert browse_folder(root, index=index).cached_directories == 1
    assert calls[root] == 1


def test_cached_directory_replaced_by_reparse_point_is_not_listed(project, monkeypatch):
    config, _ = project
    root = config.roots[0].path
    child = root / "child"
    child.mkdir()
    (child / "report.txt").touch()
    index = DirectoryIndex()
    assert scan_names(config, "report", index=index).total == 1
    original = Path.lstat

    def reparse(path, *args, **kwargs):
        metadata = original(path, *args, **kwargs)
        if path == child:
            return SimpleNamespace(st_mode=metadata.st_mode, st_file_attributes=backend.REPARSE_POINT)
        return metadata

    monkeypatch.setattr(backend.os, "scandir", lambda *_: pytest.fail("不應枚舉 reparse point"))
    monkeypatch.setattr(Path, "lstat", reparse)
    report = scan_names(replace(config, roots=config.roots[:1]), "report", index=index)
    assert report.total == 0 and report.skipped_links == 1 and report.cached_directories == 1


def test_cold_read_walks_ancestors_once_and_cache_reuse_checks_only_itself(project, monkeypatch):
    config, _ = project
    root = config.roots[0].path
    deep = root / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "report.txt").touch()
    single = replace(config, roots=config.roots[:1])
    directories = (root, root / "a", deep)
    full = Counter()
    original = backend.plain_metadata

    def counted(path):
        full[Path(path)] += 1
        return original(path)

    monkeypatch.setattr(backend, "plain_metadata", counted)
    index = DirectoryIndex()
    assert scan_names(single, "report", index=index).total == 1
    # 讀取前逐層檢查一次；讀取後只比對目錄本身，不再走訪祖先。
    assert full == {directory: 1 for directory in directories}
    full.clear()
    report = scan_names(single, "report", index=index)
    assert report.total == 1 and report.cached_directories == 3
    assert not full


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
