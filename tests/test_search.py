"""驗證名稱搜尋、上限、取消、設定與檔案系統例外。"""

from dataclasses import replace
import json
import os
from pathlib import Path
import stat
import subprocess
from threading import Event
from types import SimpleNamespace

import pytest

import main
from main import Config, Item, RequestGate, Root, entry_item, load_config, match_rank, scan_names


@pytest.mark.parametrize("query,name,expected", [
    ("rpt", "report.xlsx", 1), ("REPORT", "report.xlsx", 0),
    ("報告", "月度報告.xlsx", 0), ("rpt", "rpt.xlsx", 0),
    ("rpt", "part.txt", None), ("aaa", "abca", None),
    ("aaa", "aabca", 1), ("ss", "straße.txt", 0),
])
def test_fuzzy_match(query, name, expected):
    assert match_rank(query, name) == expected


def test_recursive_same_names_and_no_content_reads(project, monkeypatch):
    config, _ = project
    expected = set()
    for root in config.roots:
        deep = root.path / "第一層" / "第二層"
        deep.mkdir(parents=True)
        file = deep / "REPORT.xlsx"
        file.write_text("檔案內容不得被搜尋讀取", encoding="utf-8")
        expected.add(file)
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: pytest.fail("名稱搜尋不得讀取內容"))
    report = scan_names(config, "rpt")
    assert report.complete
    assert report.total == 3
    assert {item.path for item in report.items} == expected
    assert len({item.path.parent for item in report.items}) == 3


def test_sort_priority(project):
    config, _ = project
    root = config.roots[0].path
    for name in ("rpt", "rptLong", "rapt"):
        (root / name).mkdir()
    for name in ("rpt.b", "rpt.a", "xrpt.txt", "report.xlsx"):
        (root / name).touch()
    report = scan_names(config, "rpt")
    assert [item.name for item in report.items] == ["rpt", "rptLong", "rpt.a", "rpt.b", "xrpt.txt", "rapt", "report.xlsx"]


def test_total_and_best_200_across_entire_scan(project):
    config, _ = project
    root = config.roots[0].path
    for number in reversed(range(230)):
        (root / f"item{number:03}.txt").touch()
    (config.roots[2].path / "item").mkdir()
    report = scan_names(config, "item")
    assert report.total == 231
    assert len(report.items) == 200
    assert report.items[0].is_dir
    assert report.items[-1].name == "item198.txt"
    assert "命中總數 231" in report.status()


def test_scan_limit_is_explicitly_incomplete(project):
    config, _ = project
    for number in range(10):
        (config.roots[0].path / f"item{number}").touch()
    report = scan_names(replace(config, max_scan_entries=3), "item")
    assert report.limited and not report.complete
    assert report.scanned == 3
    assert "至少 3" in report.status()
    assert "結果不完整" in report.status()
    assert "命中總數" not in report.status()


def test_cancellation_during_scan(project, monkeypatch):
    config, _ = project
    for number in range(10):
        (config.roots[0].path / f"item{number}").touch()
    cancel = Event()
    original = main.entry_item
    calls = []

    def stop_after_first(entry):
        calls.append(entry.path)
        cancel.set()
        return original(entry)

    monkeypatch.setattr(main, "entry_item", stop_after_first)
    report = scan_names(config, "item", cancel)
    assert len(calls) == 1
    assert report.cancelled and not report.complete
    assert "已取消" in report.status()


def test_pre_cancelled_scan_and_empty_query(project):
    config, _ = project
    cancel = Event()
    cancel.set()
    assert scan_names(config, "x", cancel).cancelled
    assert scan_names(config, "").total == 0


def test_progress_counts_and_errors_cover_three_roots(project):
    config, _ = project
    snapshots = []
    for root in config.roots[:2]:
        child = root.path / "deep"
        child.mkdir()
        (child / "report.txt").touch()
    config.roots[2].path.rmdir()
    report = scan_names(config, "rpt", progress=snapshots.append)
    assert snapshots[0].scanned == snapshots[0].found == 0
    assert snapshots[0].directory == config.roots[0].path
    assert snapshots[-1].scanned == report.scanned == 4
    assert snapshots[-1].found == report.total == 2
    assert snapshots[-1].errors == len(report.errors) == 1
    assert snapshots[-1].directory == config.roots[2].path
    assert not report.complete


def test_progress_is_throttled_during_fast_scan(project, monkeypatch):
    config, _ = project
    for number in range(50):
        (config.roots[0].path / f"report{number}.txt").touch()
    monkeypatch.setattr(main, "monotonic", lambda: 10.0)
    snapshots = []
    report = scan_names(config, "report", progress=snapshots.append)
    assert len(snapshots) == 2
    assert snapshots[-1].found == report.total == 50
    assert report == scan_names(config, "report")


def test_progress_advances_during_scan_and_stops_after_cancel(project, monkeypatch):
    config, _ = project
    for number in range(20):
        (config.roots[0].path / f"report{number}.txt").touch()
    clock = [0.0]
    original = main.entry_item
    cancel = Event()
    snapshots = []

    def slow_entry(entry):
        clock[0] += 0.1
        return original(entry)

    def progress(snapshot):
        assert not cancel.is_set()
        snapshots.append(snapshot)
        if snapshot.scanned >= 4:
            cancel.set()

    monkeypatch.setattr(main, "monotonic", lambda: clock[0])
    monkeypatch.setattr(main, "entry_item", slow_entry)
    report = scan_names(config, "report", cancel, progress)
    assert len(snapshots) >= 3
    assert snapshots[0].scanned == 0
    assert snapshots[-1].scanned >= 4
    assert report.scanned < 20
    assert report.cancelled
    assert scan_names(config, "report", cancel, lambda snapshot: pytest.fail("取消後不得回報進度")).cancelled


def test_generation_protects_against_old_results():
    gate = RequestGate()
    old_generation, old_cancel = gate.invalidate()
    new_generation, _ = gate.invalidate()
    assert old_cancel.is_set()
    assert not gate.accepts(old_generation)
    assert gate.accepts(new_generation)


def test_overlap_roots_do_not_duplicate(project):
    config, _ = project
    nested = config.roots[0].path / "report"
    nested.mkdir()
    (nested / "report.xlsx").touch()
    overlap = Config((config.roots[0], Root("重疊", nested), config.roots[2]))
    for roots in (overlap.roots, tuple(reversed(overlap.roots))):
        report = scan_names(Config(roots), "report")
        assert report.total == 2
        assert len({item.path for item in report.items}) == 2


@pytest.mark.parametrize("error", [PermissionError("拒絕讀取"), FileNotFoundError("磁碟斷線"), OSError(53, "網路錯誤")])
def test_unreadable_directory_keeps_other_results(project, monkeypatch, error):
    config, _ = project
    blocked = config.roots[0].path / "blocked"
    blocked.mkdir()
    (config.roots[1].path / "report.txt").touch()
    original = os.scandir

    def denied(path):
        if Path(path) == blocked:
            raise error
        return original(path)

    monkeypatch.setattr(main.os, "scandir", denied)
    report = scan_names(config, "report")
    assert report.total == 1
    assert not report.complete and len(report.errors) == 1
    assert str(blocked) in report.errors[0]
    assert "命中總數" not in report.status()


def test_missing_root_does_not_abort_scan(project):
    config, _ = project
    config.roots[0].path.rmdir()
    (config.roots[1].path / "report.txt").touch()
    report = scan_names(config, "report")
    assert report.total == 1 and report.errors


def test_reparse_point_is_excluded():
    class FakeEntry:
        path = "C:\\假測試\\loop"

        def stat(self, follow_symlinks):
            assert not follow_symlinks
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=main.REPARSE_POINT)

        def is_symlink(self):
            return False

    assert entry_item(FakeEntry()) is None


@pytest.mark.skipif(os.name != "nt", reason="此案例驗證 Windows junction")
def test_real_windows_junction_does_not_recurse(project):
    config, _ = project
    root = config.roots[0].path
    (root / "report.txt").touch()
    link = root / "loop"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(root)], capture_output=True)
    assert result.returncode == 0, "測試用 junction 建立失敗"
    report = scan_names(config, "report")
    assert report.total == 1
    assert report.skipped_links == 1
    assert report.scanned == 2


@pytest.mark.parametrize("roots", [[], [{"path": "relative"}] * 3, [{"path": ""}] * 3, [None] * 3])
def test_invalid_config_shape(tmp_path, roots):
    file = tmp_path / "config.json"
    file.write_text(json.dumps({"roots": roots}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(file)


@pytest.mark.parametrize("limit", [0, -1, True, "100"])
def test_invalid_scan_limit(project, limit):
    _, file = project
    data = json.loads(file.read_text(encoding="utf-8"))
    data["max_scan_entries"] = limit
    file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="max_scan_entries"):
        load_config(file)


def test_config_valid_missing_and_malformed(project, tmp_path):
    config, file = project
    assert load_config(file).roots == config.roots
    with pytest.raises(ValueError, match="無法讀取"):
        load_config(tmp_path / "missing.json")
    file.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON"):
        load_config(file)


def test_check_cli_does_not_scan_recursively(project, monkeypatch, capsys):
    config, file = project
    monkeypatch.setattr(main, "scan_names", lambda *args: pytest.fail("設定驗證不得遞迴掃描"))
    monkeypatch.setattr(main.sys, "argv", ["main.py", "--config", str(file), "--check"])
    assert main.main() == 0
    assert "可讀取" in capsys.readouterr().out
    config.roots[0].path.rmdir()
    assert main.main() == 1


def test_favorites_do_not_expand_search_scope(project, tmp_path):
    config, _ = project
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret_report.txt").touch()
    store = main.Favorites(tmp_path / "favorites.json")
    store.toggle(outside)
    assert scan_names(config, "report").total == 0
