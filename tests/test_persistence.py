"""驗證跨次啟動索引、增量核對及保存失敗時的退回行為。"""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
from threading import Event
from types import SimpleNamespace

import pytest

import backend
from backend import DirectoryIndex, browse_folder, path_key, scan_names


def scopes(config):
    return tuple(root.path for root in config.roots)


def test_restart_restores_full_index_without_enumeration(project, monkeypatch):
    config, config_file = project
    root = config.roots[0].path
    for number in range(230):
        (root / f"report{number}.txt").touch()
    file = config_file.with_name("cache.sqlite3")
    index = DirectoryIndex(file)
    report = scan_names(config, "report", index=index)
    assert report.total == 230 and len(report.items) == 200
    index.flush()
    restored = DirectoryIndex(file)
    assert restored.load(scopes(config), Event()) == 3
    assert restored.unverified_count == 3
    monkeypatch.setattr(backend.os, "scandir", lambda *_: pytest.fail("重啟不應重掃未變動的目錄"))
    assert restored.check_changes(Event()).changed == 0
    assert restored.unverified_count == 0
    report = scan_names(config, "report229", index=restored)
    assert report.total == 1 and report.cached_directories == 3
    assert browse_folder(root, index=restored).total == 230


def test_incremental_check_finds_nested_change_without_rescanning_other_roots(project, monkeypatch):
    config, file = project
    root = config.roots[0].path
    child = root / "child"
    child.mkdir()
    (child / "old.txt").touch()
    index = DirectoryIndex(file.with_name("cache.sqlite3"))
    scan_names(config, "new", index=index)
    index.flush()
    root_before = backend.directory_signature(root.lstat())
    target = child / "new.txt"
    target.touch()
    assert backend.directory_signature(root.lstat()) == root_before
    restored = DirectoryIndex(index.file)
    assert restored.load(scopes(config), Event()) == 4
    calls = Counter()
    original = backend.os.scandir

    def counted(path):
        calls[Path(path)] += 1
        return original(path)

    monkeypatch.setattr(backend.os, "scandir", counted)
    checked = restored.check_changes(Event())
    assert checked.checked == 4 and checked.changed == 1
    report = scan_names(config, "new", index=restored)
    assert report.items[0].path == target
    assert calls == {child: 1}
    restored.flush()
    again = DirectoryIndex(index.file)
    assert again.load(scopes(config), Event()) == 4
    assert again.check_changes(Event()).changed == 0


def test_f5_metadata_refresh_updates_file_time_without_directory_enumeration(project, monkeypatch):
    config, _ = project
    root = config.roots[0].path
    target = root / "report.txt"
    target.touch()
    index = DirectoryIndex()
    before = browse_folder(root, index=index).items[0].modified_at
    root_signature = backend.directory_signature(root.lstat())
    os.utime(target, (before + 20, before + 20))
    assert backend.directory_signature(root.lstat()) == root_signature
    monkeypatch.setattr(backend.os, "scandir", lambda *_: pytest.fail("只有修改時間變動不應枚舉目錄"))
    checked = index.check_changes(Event(), refresh_times=True)
    assert checked.changed == 0
    assert browse_folder(root, index=index).items[0].modified_at == target.stat().st_mtime


def test_deleted_directory_is_removed_from_saved_index(project):
    config, file = project
    child = config.roots[0].path / "gone"
    child.mkdir()
    index = DirectoryIndex(file.with_name("cache.sqlite3"))
    scan_names(config, "gone", index=index)
    index.flush()
    child.rmdir()
    checked = index.check_changes(Event())
    assert checked.changed == 2 and checked.errors
    index.flush()
    restored = DirectoryIndex(index.file)
    restored.load(scopes(config), Event())
    assert restored.directory_item(child) is None
    assert not scan_names(config, "gone", index=restored).items


@pytest.mark.parametrize("kind", ["corrupt", "foreign", "newer-version"])
def test_invalid_database_is_preserved_and_search_uses_memory(project, kind):
    config, file = project
    database = file.with_name("cache.sqlite3")
    if kind == "corrupt":
        database.write_bytes(b"not a database")
    else:
        with sqlite3.connect(database) as connection:
            if kind == "foreign":
                connection.execute("CREATE TABLE private_data(value TEXT)")
            else:
                connection.execute("CREATE TABLE finder_meta(version INTEGER)")
                connection.execute("INSERT INTO finder_meta VALUES (99)")
                connection.execute("CREATE TABLE finder_directories(key TEXT PRIMARY KEY,payload TEXT)")
    original = database.read_bytes()
    index = DirectoryIndex(database)
    assert index.load(scopes(config), Event()) == 0 and index.error
    (config.roots[0].path / "report.txt").touch()
    assert scan_names(config, "report", index=index).total == 1
    index.flush()
    assert database.read_bytes() == original


def test_cache_records_cannot_inject_paths_outside_direct_parent(project):
    config, file = project
    root = config.roots[0].path
    index = DirectoryIndex(file.with_name("cache.sqlite3"))
    browse_folder(root, index=index)
    index.flush()
    payload = [str(root), None, list(backend.directory_signature(root.lstat())), [[str(root.parent / "outside.exe"), False, None]]]
    with sqlite3.connect(index.file) as connection:
        connection.execute("UPDATE finder_directories SET payload = ? WHERE key = ?", (json.dumps(payload), path_key(root)))
    restored = DirectoryIndex(index.file)
    assert restored.load(scopes(config), Event()) == 0
    assert restored.directory_item(root) is None


def test_cancelled_partial_directory_is_not_persisted(project, monkeypatch):
    config, file = project
    root = config.roots[0].path
    for number in range(5):
        (root / f"report{number}.txt").touch()
    index = DirectoryIndex(file.with_name("cache.sqlite3"))
    cancel = Event()
    original = backend.entry_item

    def cancel_read(entry):
        item = original(entry)
        cancel.set()
        return item

    monkeypatch.setattr(backend, "entry_item", cancel_read)
    assert browse_folder(root, cancel, index=index).cancelled
    index.flush()
    restored = DirectoryIndex(index.file)
    assert restored.load(scopes(config), Event()) == 0


def test_full_invalidation_is_saved_and_does_not_delete_source_files(project):
    config, file = project
    target = config.roots[0].path / "report.txt"
    target.write_text("保留原始內容", encoding="utf-8")
    index = DirectoryIndex(file.with_name("cache.sqlite3"))
    scan_names(config, "report", index=index)
    index.flush()
    index.invalidate()
    index.flush()
    restored = DirectoryIndex(index.file)
    assert restored.load(scopes(config), Event()) == 0
    assert target.read_text(encoding="utf-8") == "保留原始內容"


def test_cache_loading_only_restores_current_scope(project):
    config, file = project
    index = DirectoryIndex(file.with_name("cache.sqlite3"))
    scan_names(config, "nothing", index=index)
    index.flush()
    restored = DirectoryIndex(index.file)
    assert restored.load((config.roots[0].path,), Event()) == 1
    assert restored.directory_item(config.roots[1].path) is None


def test_full_rebuild_clears_records_outside_current_scope(project):
    config, file = project
    index = DirectoryIndex(file.with_name("cache.sqlite3"))
    scan_names(config, "nothing", index=index)
    index.flush()
    restored = DirectoryIndex(index.file)
    assert restored.load((config.roots[0].path,), Event()) == 1
    restored.invalidate()
    restored.flush()
    again = DirectoryIndex(index.file)
    assert again.load(scopes(config), Event()) == 0


def test_restore_limit_skips_whole_directory_instead_of_using_partial_cache(project, monkeypatch):
    config, file = project
    root = config.roots[0].path
    for number in range(3):
        (root / f"report{number}.txt").touch()
    index = DirectoryIndex(file.with_name("cache.sqlite3"))
    browse_folder(root, index=index)
    index.flush()
    monkeypatch.setattr(backend, "MAX_RESTORED_ENTRIES", 2)
    restored = DirectoryIndex(index.file)
    assert restored.load(scopes(config), Event()) == 0
    assert browse_folder(root, index=restored).total == 3


def test_rebuild_during_disk_save_cannot_restore_old_records(project, monkeypatch):
    config, file = project
    index = DirectoryIndex(file.with_name("cache.sqlite3"))
    scan_names(config, "nothing", index=index)
    started, release = Event(), Event()
    original = index._connect

    def delayed():
        started.set()
        assert release.wait(5)
        return original()

    monkeypatch.setattr(index, "_connect", delayed)
    with ThreadPoolExecutor(max_workers=2) as executor:
        saving = executor.submit(index.flush)
        try:
            assert started.wait(5)
            index.invalidate()
            cleanup = executor.submit(index.flush)
        finally:
            release.set()
        saving.result(timeout=5)
        cleanup.result(timeout=5)
    restored = DirectoryIndex(index.file)
    assert restored.load(scopes(config), Event()) == 0


def test_write_failure_stays_nonfatal_and_preserves_source(project, monkeypatch):
    config, file = project
    target = config.roots[0].path / "report.txt"
    target.write_text("原始資料", encoding="utf-8")
    index = DirectoryIndex(file.with_name("cache.sqlite3"))
    scan_names(config, "report", index=index)

    def failed():
        raise sqlite3.OperationalError("磁碟無法寫入")

    monkeypatch.setattr(index, "_connect", failed)
    index.flush()
    assert index.error
    assert scan_names(config, "report", index=index).total == 1
    assert target.read_text(encoding="utf-8") == "原始資料"


def count_metadata_checks(monkeypatch):
    """分別計算逐層祖先檢查與只檢查自身的次數。"""
    full, own = Counter(), Counter()
    original_plain, original_own = backend.plain_metadata, backend.own_metadata

    def counted_plain(path):
        full[Path(path)] += 1
        return original_plain(path)

    def counted_own(path):
        own[Path(path)] += 1
        return original_own(path)

    monkeypatch.setattr(backend, "plain_metadata", counted_plain)
    monkeypatch.setattr(backend, "own_metadata", counted_own)
    return full, own


def test_f5_time_refresh_checks_entries_themselves_not_every_ancestor(project, monkeypatch):
    config, _ = project
    root = config.roots[0].path
    files = [root / f"report{number}.txt" for number in range(20)]
    for path in files:
        path.touch()
    index = DirectoryIndex()
    browse_folder(root, index=index)
    full, own = count_metadata_checks(monkeypatch)
    assert index.check_changes(Event(), refresh_times=True).changed == 0
    # 目錄本身逐層檢查一次；每個項目只檢查自身，祖先不隨項目數重複 lstat。
    assert full == {root: 1}
    assert all(own[path] == 1 for path in files)
    assert own[root] == own[root.parent] == 1


def test_f5_time_refresh_still_rejects_entry_replaced_by_reparse_point(project, monkeypatch):
    config, _ = project
    root = config.roots[0].path
    target = root / "report.txt"
    target.touch()
    index = DirectoryIndex()
    browse_folder(root, index=index)
    original = Path.lstat

    def reparse(path, *args, **kwargs):
        metadata = original(path, *args, **kwargs)
        if path == target:
            return SimpleNamespace(st_mode=metadata.st_mode, st_file_attributes=backend.REPARSE_POINT)
        return metadata

    monkeypatch.setattr(Path, "lstat", reparse)
    checked = index.check_changes(Event(), refresh_times=True)
    assert checked.changed == 1 and "reparse point" in checked.errors[0]
    assert index.directory_item(root) is None


def test_load_reads_each_scope_range_in_one_query_without_sibling_prefixes(project, monkeypatch):
    config, file = project
    root = config.roots[0].path
    child = root / "child"
    child.mkdir()
    # 名稱以根目錄開頭但不在其底下；「[」與空白排在「\」前，「x」與「]」排在範圍上界之後。
    siblings = [root.parent / name for name in ("root0 copy", "root0[old]", "root0x", "root0]")]
    index = DirectoryIndex(file.with_name("cache.sqlite3"))
    for directory in (root, child, *siblings):
        directory.mkdir(exist_ok=True)
        browse_folder(directory, index=index)
    index.flush()
    statements = []
    connect = backend.sqlite3.connect

    def traced(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(backend.sqlite3, "connect", traced)
    restored = DirectoryIndex(index.file)
    # 重疊範圍只查一次，同一筆紀錄不重複計算。
    assert restored.load((root, child, root), Event()) == 2
    assert restored.directory_item(child) is not None
    assert all(restored.directory_item(sibling) is None for sibling in siblings)
    reads = [statement for statement in statements if "FROM finder_directories" in statement]
    assert len(reads) == 1 and "payload" in reads[0]


def test_load_skips_records_over_size_cap(project, monkeypatch):
    config, file = project
    root = config.roots[0].path
    (root / "report.txt").touch()
    index = DirectoryIndex(file.with_name("cache.sqlite3"))
    browse_folder(root, index=index)
    index.flush()
    monkeypatch.setattr(backend, "MAX_CACHE_RECORD_BYTES", 16)
    restored = DirectoryIndex(index.file)
    assert restored.load(scopes(config), Event()) == 0 and not restored.error
    assert browse_folder(root, index=restored).total == 1


def short_lock_timeout(monkeypatch):
    """鎖定測試不等待正式的 2 秒逾時。"""
    connect = backend.sqlite3.connect
    monkeypatch.setattr(backend.sqlite3, "connect", lambda file, *args, **kwargs: connect(file, timeout=0.05))


def busy_error():
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return error


def test_locked_database_keeps_pending_writes_for_retry(project, monkeypatch):
    config, file = project
    database = file.with_name("cache.sqlite3")
    index = DirectoryIndex(database)
    scan_names(config, "nothing", index=index)
    index.flush()
    child = config.roots[0].path / "child"
    child.mkdir()
    browse_folder(child, index=index)
    holder = sqlite3.connect(database)
    try:
        # 模擬另一個實例持有寫入鎖。
        holder.execute("BEGIN EXCLUSIVE")
        short_lock_timeout(monkeypatch)
        index.flush()
        assert not index.error and "下次保存時重試" in index.warning
    finally:
        holder.close()
    index.flush()
    assert not index.error and not index.warning
    restored = DirectoryIndex(database)
    assert restored.load(scopes(config), Event()) == 4
    assert restored.directory_item(child) is not None


def test_locked_database_on_startup_does_not_disable_saving(project, monkeypatch):
    config, file = project
    database = file.with_name("cache.sqlite3")
    index = DirectoryIndex(database)
    scan_names(config, "nothing", index=index)
    index.flush()
    restored = DirectoryIndex(database)
    holder = sqlite3.connect(database)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        short_lock_timeout(monkeypatch)
        assert restored.load(scopes(config), Event()) == 0
        assert not restored.error and "未還原" in restored.warning
    finally:
        holder.close()
    (config.roots[0].path / "report.txt").touch()
    assert scan_names(config, "report", index=restored).total == 1
    restored.flush()
    assert not restored.error and not restored.warning
    again = DirectoryIndex(database)
    assert again.load(scopes(config), Event()) == 3
    assert again.check_changes(Event()).changed == 0


def test_locked_save_during_rebuild_does_not_requeue_old_records(project, monkeypatch):
    config, file = project
    index = DirectoryIndex(file.with_name("cache.sqlite3"))
    scan_names(config, "nothing", index=index)
    index.flush()
    child = config.roots[0].path / "child"
    child.mkdir()
    browse_folder(child, index=index)
    original = index._connect

    def rebuilt_then_locked():
        index.invalidate()
        raise busy_error()

    monkeypatch.setattr(index, "_connect", rebuilt_then_locked)
    index.flush()
    assert not index.error and index.warning
    monkeypatch.setattr(index, "_connect", original)
    index.flush()
    assert not index.warning
    restored = DirectoryIndex(index.file)
    assert restored.load(scopes(config), Event()) == 0


@pytest.mark.parametrize("code,transient", [
    (sqlite3.SQLITE_BUSY, True), (sqlite3.SQLITE_LOCKED, True), (sqlite3.SQLITE_BUSY | (2 << 8), True),
    (sqlite3.SQLITE_IOERR, False), (sqlite3.SQLITE_READONLY, False), (None, False),
])
def test_only_busy_or_locked_sqlite_errors_are_retried(code, transient):
    error = sqlite3.OperationalError("模擬錯誤")
    if code is not None:
        error.sqlite_errorcode = code
    assert DirectoryIndex._transient(error) is transient
    assert not DirectoryIndex._transient(ValueError("索引版本不符"))
