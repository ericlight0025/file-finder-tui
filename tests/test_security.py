"""以安全的假資料驗證外部文字、JSON 及直接路徑的信任邊界。"""

import asyncio
import json
import os
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from textual.widgets import OptionList, Static

import backend
import main
from backend import Config, DirectoryIndex, Favorites, Root
from main import ConfirmOpen, ErrorDetails, FileFinderApp


def test_parent_reparse_point_blocks_open_and_browse(tmp_path, monkeypatch):
    parent = tmp_path / "redirected"
    parent.mkdir()
    target = parent / "report.txt"
    target.touch()
    original = Path.lstat

    def reparse(path, *args, **kwargs):
        metadata = original(path, *args, **kwargs)
        if path == parent:
            return SimpleNamespace(st_mode=metadata.st_mode, st_file_attributes=backend.REPARSE_POINT)
        return metadata

    monkeypatch.setattr(Path, "lstat", reparse)
    opened = []
    assert backend.path_item(target).error
    assert "reparse point" in backend.open_file(target, opened.append)
    assert not opened


@pytest.mark.skipif(os.name != "nt", reason="Windows handle 共享模式驗證")
def test_open_locks_target_and_parent_against_replacement(tmp_path):
    parent = tmp_path / "folder"
    parent.mkdir()
    target = parent / "report.txt"
    replacement = parent / "replacement.txt"
    target.write_text("原始檔案", encoding="utf-8")
    replacement.write_text("替換檔案", encoding="utf-8")
    opened = []

    def opener(path):
        with pytest.raises(OSError):
            os.replace(replacement, target)
        with pytest.raises(OSError):
            parent.rename(tmp_path / "moved")
        opened.append(path)

    assert "已要求" in backend.open_file(target, opener)
    assert opened == [str(target)]
    assert target.read_text(encoding="utf-8") == "原始檔案"
    # handle 釋放後可正常替換，避免永久鎖住使用者檔案。
    os.replace(replacement, target)


@pytest.mark.parametrize("bad", ["bad\x00path", "bad\ud800path"])
def test_invalid_json_paths_are_rejected_before_io(project, bad):
    config, file = project
    data = json.loads(file.read_text(encoding="utf-8"))
    data["roots"][0]["path"] = str(config.roots[0].path / bad)
    file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="有效"):
        backend.load_config(file)
    favorites_file = file.with_name("favorites.json")
    original = json.dumps([str(config.roots[0].path / bad)])
    favorites_file.write_text(original, encoding="utf-8")
    store = Favorites(favorites_file)
    assert store.error and not store.paths
    with pytest.raises(ValueError, match="收藏已停用"):
        store.toggle(config.roots[0].path)
    assert favorites_file.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("kind", ["config", "favorites"])
@pytest.mark.parametrize("payload", ["deep", "oversized"])
def test_json_resource_limits_do_not_escape_as_worker_errors(tmp_path, monkeypatch, kind, payload):
    file = tmp_path / f"{kind}.json"
    if payload == "deep":
        contents = "[" * 20000 + "0" + "]" * 20000
    else:
        monkeypatch.setattr(backend, "MAX_JSON_BYTES", 64)
        contents = " " * 65
    file.write_text(contents, encoding="utf-8")
    if kind == "config":
        with pytest.raises(ValueError, match="JSON"):
            backend.load_config(file)
    else:
        assert Favorites(file).error
    assert file.read_text(encoding="utf-8") == contents


def test_oversized_favorite_save_keeps_original(tmp_path, monkeypatch):
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    first.touch()
    second.touch()
    store = Favorites(tmp_path / "favorites.json")
    store.toggle(first)
    original = store.file.read_bytes()
    monkeypatch.setattr(backend, "MAX_JSON_BYTES", len(original))
    with pytest.raises(ValueError, match="大小限制"):
        store.toggle(second)
    assert store.paths == [first]
    assert store.file.read_bytes() == original
    assert not list(tmp_path.glob(".favorites-*.tmp"))


def test_direct_invalid_paths_return_errors_without_opening():
    path = Path("/bad\x00path")
    opened = []
    assert backend.path_item(path).error
    assert backend.validate_folder(path)
    assert backend.browse_folder(path).errors
    assert "路徑格式錯誤" in backend.open_file(path, opened.append)
    assert not opened


@pytest.mark.skipif(os.name == "nt", reason="實際 symlink 建立需要 Windows 權限；另以 reparse 屬性驗證")
def test_direct_links_cannot_bypass_scan_browse_or_open(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.touch()
    directory_link = tmp_path / "root-link"
    directory_link.symlink_to(outside, target_is_directory=True)
    config = Config((Root("link", directory_link),))
    report = backend.scan_names(config, "secret")
    assert not report.items and report.skipped_links == 1
    report = backend.browse_folder(directory_link)
    assert not report.items and report.skipped_links == 1
    assert backend.path_item(directory_link, require_dir=True).error
    assert "reparse point" in backend.validate_folder(directory_link)
    file_link = tmp_path / "report.txt"
    file_link.symlink_to(secret)
    opened = []
    assert "reparse point" in backend.open_file(file_link, opened.append)
    assert not opened
    store = Favorites(tmp_path / "favorites.json")
    with pytest.raises(ValueError, match="只能收藏"):
        store.toggle(file_link)


def test_reparse_attribute_blocks_direct_io_even_with_cached_folder(project, monkeypatch):
    config, _ = project
    root = config.roots[0].path
    (root / "old.txt").touch()
    index = DirectoryIndex()
    assert backend.browse_folder(root, index=index).items
    original = Path.lstat

    def reparse(path, *args, **kwargs):
        metadata = original(path, *args, **kwargs)
        if path == root:
            return SimpleNamespace(st_mode=metadata.st_mode, st_file_attributes=backend.REPARSE_POINT)
        return metadata

    monkeypatch.setattr(Path, "lstat", reparse)
    monkeypatch.setattr(backend.os, "scandir", lambda *_: pytest.fail("不應枚舉 reparse point"))
    report = backend.browse_folder(root, index=index)
    assert report.skipped_links == 1 and not report.items
    assert backend.path_item(root, index=index).error
    opened = []
    assert "reparse point" in backend.open_file(root, opened.append)
    assert not opened


def test_cached_directory_replaced_by_file_is_not_browsed(tmp_path):
    directory = tmp_path / "folder"
    directory.mkdir()
    index = DirectoryIndex()
    backend.browse_folder(directory, index=index)
    directory.rmdir()
    directory.touch()
    assert backend.browse_folder(directory, index=index).errors
    assert backend.path_item(directory, require_dir=True, index=index).error


def test_display_escapes_controls_but_keeps_actual_unicode_name():
    raw = "契變\x1b[2J\n\x85\u202etxt.exe\ud800"
    text = backend.display_text(raw)
    assert text == "契變\\x1b[2J\\x0a\\x85\\u202etxt.exe\\ud800"
    assert backend.display_text("第一行\n第二行", multiline=True) == "第一行\n第二行"


def test_terminal_control_payload_never_reaches_rendered_external_fields(project):
    config, file = project
    payload = "hello\x1b[2J\u202etxt.exe"
    data = json.loads(file.read_text(encoding="utf-8"))
    data["roots"][0]["name"] = payload
    file.write_text(json.dumps(data), encoding="utf-8")

    async def scenario():
        app = FileFinderApp(file)
        async with app.run_test(size=(110, 40)) as pilot:
            async with asyncio.timeout(5):
                while app.config is None:
                    await asyncio.sleep(0.01)
            await pilot.pause()
            prompt = str(app.query_one(OptionList).get_option_at_index(0).prompt)
            assert payload not in prompt
            assert backend.display_text(payload) in prompt
            output = app.screen._compositor.render_full_update().render_segments(app.console)
            assert payload not in output
            assert "hello\\x1b[2J\\u202etxt.exe" in output
            app.set_status(payload)
            assert payload not in str(app.query_one("#status", Static).render())
            app.push_screen(ConfirmOpen(config.roots[0].path / payload))
            await pilot.pause()
            assert payload not in str(app.screen.query_one(Static).render())
            await pilot.press("escape")
            app.push_screen(ErrorDetails((payload,)))
            await pilot.pause()
            error_prompt = str(app.screen.query_one(OptionList).get_option_at_index(0).prompt)
            assert payload not in error_prompt

    asyncio.run(scenario())


def test_cli_check_escapes_config_labels(project, monkeypatch, capsys):
    _, file = project
    payload = "hello\x1b[2J\u202etxt.exe"
    data = json.loads(file.read_text(encoding="utf-8"))
    data["roots"][0]["name"] = payload
    file.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(main.sys, "argv", ["main.py", "--config", str(file), "--check"])
    assert main.main() == 0
    output = capsys.readouterr().out
    assert payload not in output and backend.display_text(payload) in output


def test_invalid_favorites_keeps_tui_running(project):
    _, file = project
    file.with_name("favorites.json").write_text(json.dumps([str(file.parent / "bad\x00path")]), encoding="utf-8")

    async def scenario():
        app = FileFinderApp(file)
        async with app.run_test(size=(110, 40)) as pilot:
            async with asyncio.timeout(5):
                while app.config is None:
                    await asyncio.sleep(0.01)
            assert app.favorites.error
            await pilot.press("alt+2")
            await pilot.pause()
            assert app.active_menu == "favorites"
            assert not app.navigation.view.items
            assert "收藏已停用" in app.navigation.view.status

    asyncio.run(scenario())
