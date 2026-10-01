"""檔案處理後端：設定、名稱搜尋、資料夾瀏覽與收藏保存；不依賴 TUI。"""

from __future__ import annotations

import bisect
import json
import ntpath
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Callable


RESULT_LIMIT = 200
PROGRESS_INTERVAL_SECONDS = 0.2
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def path_key(path: str | Path) -> str:
    """以 Windows 規則消除大小寫、分隔符號與相對片段差異。"""
    return ntpath.normcase(ntpath.normpath(os.path.abspath(os.fspath(path))))


def path_error(path: str | Path, error: OSError) -> str:
    if isinstance(error, PermissionError):
        reason = "沒有讀取權限"
    elif isinstance(error, FileNotFoundError):
        reason = "路徑不存在或網路磁碟未連線"
    elif isinstance(error, NotADirectoryError):
        reason = "不是資料夾"
    else:
        reason = f"無法讀取，請檢查磁碟或網路連線（系統代碼 {error.winerror if hasattr(error, 'winerror') else error.errno}）"
    return f"{path}：{reason}"


@dataclass(frozen=True)
class Root:
    name: str
    path: Path


@dataclass(frozen=True)
class Config:
    roots: tuple[Root, ...]
    max_scan_entries: int = 250_000


def load_config(path: Path) -> Config:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as error:
        raise ValueError(f"無法讀取 config.json，設定檔尚未建立：{path}\n請參考 config.example.json 建立設定檔，填入三個搜尋資料夾後重啟。") from error
    except (OSError, ValueError) as error:
        raise ValueError(f"無法讀取 config.json，請檢查檔案與 JSON 格式：{path}") from error
    roots = data.get("roots") if isinstance(data, dict) else None
    if not isinstance(roots, list) or len(roots) != 3:
        raise ValueError("config.json 的 roots 必須剛好包含三個搜尋根目錄。")
    parsed = []
    for index, root in enumerate(roots, 1):
        if not isinstance(root, dict) or not isinstance(root.get("path"), str) or not root["path"].strip():
            raise ValueError(f"第 {index} 個根目錄必須設定非空白 path。")
        directory = Path(root["path"]).expanduser()
        if not directory.is_absolute():
            raise ValueError(f"第 {index} 個根目錄請使用完整絕對路徑。")
        name = root.get("name", f"根目錄 {index}")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"第 {index} 個根目錄名稱不可空白。")
        parsed.append(Root(name, directory))
    limit = data.get("max_scan_entries", 250_000)
    if type(limit) is not int or limit < 1:
        raise ValueError("max_scan_entries 必須是大於零的整數。")
    return Config(tuple(parsed), limit)


@dataclass(frozen=True)
class Item:
    path: Path
    is_dir: bool
    label: str = ""
    error: str = ""
    modified_at: float | None = None

    @property
    def name(self) -> str:
        return self.path.name or str(self.path)

    @property
    def modified_text(self) -> str:
        """依執行電腦的本地時區顯示檔案系統修改時間。"""
        if self.modified_at is None:
            return "無法取得"
        try:
            return datetime.fromtimestamp(self.modified_at).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError):
            return "無法取得"


@dataclass(frozen=True)
class ScanReport:
    items: tuple[Item, ...] = ()
    total: int = 0
    scanned: int = 0
    errors: tuple[str, ...] = ()
    cancelled: bool = False
    limited: bool = False
    skipped_links: int = 0

    @property
    def complete(self) -> bool:
        return not (self.cancelled or self.limited or self.errors)

    def status(self) -> str:
        if self.complete:
            result = f"命中總數 {self.total} 筆，顯示 {len(self.items)} 筆"
        else:
            reasons = []
            if self.cancelled:
                reasons.append("已取消")
            if self.limited:
                reasons.append("已達掃描上限")
            if self.errors:
                reasons.append(f"{len(self.errors)} 項讀取錯誤")
            result = f"結果不完整（{'、'.join(reasons)}）；已找到至少 {self.total} 筆，顯示 {len(self.items)} 筆"
        result += f"；已掃描 {self.scanned} 個項目"
        if self.skipped_links:
            result += f"；已排除 {self.skipped_links} 個連結／reparse point"
        if self.errors:
            result += f"；Ctrl+E 查看全部錯誤\n{self.errors[0]}"
        return result


def match_rank(query: str, name: str) -> int | None:
    """空白分隔的每個關鍵字都須完整出現在名稱中，忽略大小寫。"""
    query, name = query.strip().casefold(), name.casefold()
    words = query.split()
    if not words or not all(word in name for word in words):
        return None
    # 全段連續命中優先，但多個關鍵字不要求排列順序。
    return 0 if query in name else 1


def search_key(query: str, item: Item) -> tuple:
    return (not item.is_dir, match_rank(query, item.name), len(item.name), item.name.casefold(), path_key(item.path))


def entry_item(entry: os.DirEntry) -> Item | None:
    """排除符號連結及所有 Windows reparse point，包含 junction。"""
    metadata = entry.stat(follow_symlinks=False)
    if entry.is_symlink() or getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT:
        return None
    return Item(Path(entry.path), stat.S_ISDIR(metadata.st_mode), modified_at=metadata.st_mtime)


def path_item(path: Path, label: str = "", require_dir: bool = False) -> Item:
    """取得首頁／收藏項目的類型與時間；失效路徑仍保留供移除。"""
    try:
        metadata = path.stat()
        is_dir = stat.S_ISDIR(metadata.st_mode)
        if require_dir and not is_dir:
            return Item(path, False, label, f"{path}：不是資料夾", metadata.st_mtime)
        if not is_dir and not stat.S_ISREG(metadata.st_mode):
            return Item(path, False, label, f"{path}：不是一般檔案或資料夾", metadata.st_mtime)
        error = validate_folder(path) if is_dir else ""
        return Item(path, is_dir, label, error, metadata.st_mtime)
    except OSError as error:
        return Item(path, require_dir, label, path_error(path, error))


@dataclass(frozen=True)
class SearchProgress:
    scanned: int = 0
    found: int = 0
    directory: Path | None = None
    errors: int = 0
    skipped_links: int = 0


def scan_names(config: Config, query: str, cancel: Event | None = None, progress: Callable[[SearchProgress], None] | None = None) -> ScanReport:
    cancel = cancel or Event()
    best: list[tuple[tuple, Item]] = []
    errors: list[str] = []
    visited: set[str] = set()
    total = scanned = skipped_links = 0
    limited = False
    directory = None
    last_progress = float("-inf")
    if not query.strip():
        return ScanReport()
    # 根目錄本身也屬於名稱搜尋範圍；重疊根目錄只計算一次。
    stack = [(root.path, True) for root in reversed(config.roots)]

    def publish_progress(force: bool = False) -> None:
        nonlocal last_progress
        if progress is None or cancel.is_set():
            return
        now = monotonic()
        # 限制背景訊息頻率；最後一份快照保留實際計數，不建立索引。
        if force or now - last_progress >= PROGRESS_INTERVAL_SECONDS:
            last_progress = now
            progress(SearchProgress(scanned, total, directory, len(errors), skipped_links))

    def consider(item: Item) -> None:
        nonlocal total
        if match_rank(query, item.name) is None:
            return
        total += 1
        candidate = (search_key(query, item), item)
        if len(best) < RESULT_LIMIT or candidate[0] < best[-1][0]:
            bisect.insort(best, candidate, key=lambda value: value[0])
            if len(best) > RESULT_LIMIT:
                best.pop()

    while stack and not cancel.is_set() and not limited:
        directory, is_root = stack.pop()
        key = path_key(directory)
        if key in visited:
            continue
        visited.add(key)
        publish_progress()
        try:
            with os.scandir(directory) as entries:
                if is_root:
                    metadata = directory.stat()
                    consider(Item(directory, True, modified_at=metadata.st_mtime))
                for entry in entries:
                    if cancel.is_set():
                        break
                    publish_progress()
                    if scanned >= config.max_scan_entries:
                        limited = True
                        break
                    scanned += 1
                    try:
                        item = entry_item(entry)
                    except OSError as error:
                        errors.append(path_error(entry.path, error))
                        continue
                    if item is None:
                        skipped_links += 1
                        continue
                    if path_key(item.path) in visited:
                        continue
                    if item.is_dir:
                        # 在發現時就計算名稱；展開時不再次計算。
                        consider(item)
                        stack.append((item.path, False))
                    else:
                        consider(item)
        except OSError as error:
            errors.append(path_error(directory, error))
    publish_progress(force=True)
    return ScanReport(tuple(item for _, item in best), total, scanned, tuple(errors), cancel.is_set(), limited, skipped_links)


def browse_folder(directory: Path, cancel: Event | None = None) -> ScanReport:
    """只讀取直接子項目；此處不套用搜尋的 200 筆顯示上限。"""
    cancel = cancel or Event()
    items, errors = [], []
    scanned = skipped_links = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if cancel.is_set():
                    break
                scanned += 1
                try:
                    item = entry_item(entry)
                    if item is None:
                        skipped_links += 1
                    else:
                        items.append(item)
                except OSError as error:
                    errors.append(path_error(entry.path, error))
    except OSError as error:
        errors.append(path_error(directory, error))
    items.sort(key=lambda item: (not item.is_dir, item.name.casefold(), path_key(item.path)))
    return ScanReport(tuple(items), len(items), scanned, tuple(errors), cancel.is_set(), False, skipped_links)


def validate_folder(path: Path) -> str:
    try:
        with os.scandir(path):
            pass
    except OSError as error:
        return path_error(path, error)
    return ""


class Favorites:
    def __init__(self, file: Path):
        self.file = file
        self.paths: list[Path] = []
        self.error = ""
        try:
            data = json.loads(file.read_text(encoding="utf-8-sig"))
            if not isinstance(data, list) or any(not isinstance(path, str) or not Path(path).is_absolute() for path in data):
                raise ValueError("最愛格式錯誤")
            keys = set()
            for value in data:
                if path_key(value) not in keys:
                    self.paths.append(Path(value))
                    keys.add(path_key(value))
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.error = f"無法讀取最愛檔案，收藏已停用；請修正後重啟：{file}"

    def contains(self, path: Path) -> bool:
        return any(path_key(saved) == path_key(path) for saved in self.paths)

    def toggle(self, path: Path) -> bool:
        if self.error:
            raise ValueError(self.error)
        exists = self.contains(path)
        if not exists:
            item = path_item(path)
            if item.error:
                raise ValueError(f"只能收藏可讀取的一般檔案或資料夾。{item.error}")
        updated = [saved for saved in self.paths if path_key(saved) != path_key(path)] if exists else [*self.paths, path]
        temporary = None
        try:
            # 暫存檔與目的檔位於同一資料夾，成功寫入後再原子替換。
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.file.parent, prefix=".favorites-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump([str(saved) for saved in updated], stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.file)
        except OSError as error:
            raise ValueError("最愛保存失敗，原有收藏保持不變；請檢查專案資料夾的寫入權限。") from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        self.paths = updated
        return not exists


def list_favorites(favorites: Favorites, cancel: Event) -> tuple[Item, ...]:
    """只讀取已收藏路徑，不展開其中的資料夾；資料夾排序在前。"""
    items: list[Item] = []
    for path in tuple(favorites.paths):
        if cancel.is_set():
            break
        items.append(path_item(path))
    items.sort(key=lambda item: (not item.is_dir, item.name.casefold(), path_key(item.path)))
    return tuple(items)


def list_home_items(config: Config, favorites: Favorites, cancel: Event, menu: str = "folders") -> tuple[Item, ...]:
    """讀取指定分頁的首頁項目，前端不直接存取檔案系統。"""
    if menu == "favorites":
        return list_favorites(favorites, cancel)
    items: list[Item] = []
    for root in config.roots:
        if cancel.is_set():
            break
        items.append(path_item(root.path, root.name, require_dir=True))
    return tuple(items)


def search_favorites(favorites: Favorites, query: str, cancel: Event) -> ScanReport:
    """依完整關鍵字篩選收藏名稱，保留取消與讀取錯誤資訊。"""
    items = list_favorites(favorites, cancel)
    matches = sorted((item for item in items if match_rank(query, item.name) is not None), key=lambda item: search_key(query, item))
    return ScanReport(tuple(matches[:RESULT_LIMIT]), len(matches), len(items), tuple(item.error for item in items if item.error), cancel.is_set())


def open_file(path: Path, opener: Callable[[str], None] | None) -> str:
    """要求作業系統開啟單一檔案，回傳前端可顯示的結果訊息。"""
    try:
        if not path.is_file():
            raise FileNotFoundError(str(path))
        if opener is None:
            return "此工具的檔案開啟功能僅支援 Windows。"
        opener(str(path))
        return f"已要求 Windows 開啟：{path}"
    except OSError as error:
        return path_error(path, error)

