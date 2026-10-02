"""驗證檔案／資料夾收藏、正規化、原子保存與損壞保護。"""

import json
from pathlib import Path

import pytest

import backend
from backend import Favorites, path_key


def test_add_reload_and_remove(tmp_path):
    folder = tmp_path / "中文資料夾"
    folder.mkdir()
    file = tmp_path / "favorites.json"
    store = Favorites(file)
    assert store.toggle(folder)
    assert store.contains(folder)
    assert json.loads(file.read_text(encoding="utf-8")) == [str(folder)]
    reloaded = Favorites(file)
    assert reloaded.contains(folder)
    assert not reloaded.toggle(folder)
    assert Favorites(file).paths == []


def test_windows_path_normalization():
    assert path_key("C:\\Test\\Work\\..\\Work\\") == path_key("c:/test/work")
    assert path_key("U:\\SQL\\") == path_key("u:/sql")
    assert path_key("\\\\Server\\Share\\Folder") == path_key("\\\\server\\share\\folder\\")


def test_loading_duplicate_paths_and_toggle_alias(tmp_path):
    folder = tmp_path / "資料夾"
    folder.mkdir()
    alias = str(folder).upper() + "\\.\\"
    file = tmp_path / "favorites.json"
    file.write_text(json.dumps([str(folder), alias]), encoding="utf-8")
    store = Favorites(file)
    assert len(store.paths) == 1
    assert not store.toggle(Path(alias))
    assert json.loads(file.read_text(encoding="utf-8")) == []


def test_file_favorite_reload_and_missing_path_rejection(tmp_path):
    file = tmp_path / "report.txt"
    file.touch()
    store = Favorites(tmp_path / "favorites.json")
    assert store.toggle(file)
    assert Favorites(store.file).contains(file)
    file.unlink()
    # 檔案失效後仍可明確移除；加入新的失效路徑則拒絕。
    assert not store.toggle(file)
    with pytest.raises(ValueError, match="只能收藏"):
        store.toggle(tmp_path / "missing")
    assert store.paths == []


def test_missing_favorite_retained_until_explicit_remove(tmp_path):
    folder = tmp_path / "deleted"
    folder.mkdir()
    store = Favorites(tmp_path / "favorites.json")
    store.toggle(folder)
    folder.rmdir()
    reloaded = Favorites(store.file)
    assert reloaded.paths == [folder]
    assert backend.validate_folder(folder)
    assert not reloaded.toggle(folder)


@pytest.mark.parametrize("data", ["{", "{}", '[1]', '["relative/path"]'])
def test_corrupt_json_is_never_overwritten(tmp_path, data):
    file = tmp_path / "favorites.json"
    file.write_text(data, encoding="utf-8")
    folder = tmp_path / "folder"
    folder.mkdir()
    store = Favorites(file)
    assert store.error
    with pytest.raises(ValueError, match="收藏已停用"):
        store.toggle(folder)
    assert file.read_text(encoding="utf-8") == data


def test_atomic_failure_preserves_original_and_memory(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    store = Favorites(tmp_path / "favorites.json")
    store.toggle(first)
    old = store.file.read_bytes()

    def failed_replace(source, destination):
        assert Path(source).parent == Path(destination).parent
        assert json.loads(Path(source).read_text(encoding="utf-8")) == [str(first), str(second)]
        raise PermissionError("模擬原子替換失敗")

    monkeypatch.setattr(backend.os, "replace", failed_replace)
    with pytest.raises(ValueError, match="保存失敗"):
        store.toggle(second)
    assert store.paths == [first]
    assert store.file.read_bytes() == old
    assert list(tmp_path.glob(".favorites-*.tmp")) == []


def test_save_uses_replace_after_fsync(tmp_path, monkeypatch):
    folder = tmp_path / "folder"
    folder.mkdir()
    calls = []
    fsync, replace = backend.os.fsync, backend.os.replace

    def record_fsync(fd):
        calls.append("fsync")
        return fsync(fd)

    def record_replace(source, destination):
        calls.append("replace")
        return replace(source, destination)

    monkeypatch.setattr(backend.os, "fsync", record_fsync)
    monkeypatch.setattr(backend.os, "replace", record_replace)
    Favorites(tmp_path / "favorites.json").toggle(folder)
    assert calls == ["fsync", "replace"]
