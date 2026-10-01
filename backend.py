"""檔案處理後端：設定、名稱搜尋、資料夾瀏覽與收藏保存；不依賴 TUI。"""

from __future__ import annotations

import bisect
import json
import ntpath
import os
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from threading import Event, Lock
from time import monotonic
from typing import Callable, Iterator


RESULT_LIMIT = 200
PROGRESS_INTERVAL_SECONDS = 0.2
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_CACHE_RECORD_BYTES = 64 * 1024 * 1024
MAX_RESTORED_ENTRIES = 1_000_000
BIDI_CONTROLS = {0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)}


def display_text(value: object, *, multiline: bool = False) -> str:
    """讓外部文字中的終端控制碼與方向控制字元可見，不改動實際路徑。"""
    def visible(character: str) -> str:
        code = ord(character)
        if character == "\n" and multiline:
            return character
        if code < 32 or 0x7F <= code <= 0x9F:
            return f"\\x{code:02x}"
        if code in BIDI_CONTROLS or code in {0x2028, 0x2029} or 0xD800 <= code <= 0xDFFF:
            return f"\\u{code:04x}"
        return character
    return "".join(visible(character) for character in str(value))


def valid_path_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value and not any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def read_json(file: Path) -> object:
    """有界讀取本機 JSON；格式、深度或編碼異常由呼叫者提供錯誤提示。"""
    with file.open("rb") as stream:
        data = stream.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("JSON 超過 2 MiB 大小限制")
    try:
        return json.loads(data.decode("utf-8-sig"))
    except RecursionError as error:
        raise ValueError("JSON 巢狀層級過深") from error


class ExcludedLinkError(OSError):
    """明確拒絕直接路徑上的符號連結及 Windows reparse point。"""


def plain_metadata(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT:
        raise ExcludedLinkError(str(path))
    return metadata


def path_key(path: str | Path) -> str:
    """以 Windows 規則消除大小寫、分隔符號與相對片段差異。"""
    return ntpath.normcase(ntpath.normpath(os.path.abspath(os.fspath(path))))


def path_error(path: str | Path, error: OSError) -> str:
    if isinstance(error, ExcludedLinkError):
        reason = "不支援符號連結／reparse point"
    elif isinstance(error, PermissionError):
        reason = "沒有讀取權限"
    elif isinstance(error, FileNotFoundError):
        reason = "路徑不存在或網路磁碟未連線"
    elif isinstance(error, NotADirectoryError):
        reason = "不是資料夾"
    else:
        reason = f"無法讀取，請檢查磁碟或網路連線（系統代碼 {error.winerror if hasattr(error, 'winerror') else error.errno}）"
    return f"{display_text(path)}：{reason}"


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
        data = read_json(path)
    except FileNotFoundError as error:
        raise ValueError(f"無法讀取 config.json，設定檔尚未建立：{display_text(path)}\n請參考 config.example.json 建立設定檔，填入三個搜尋資料夾後重啟。") from error
    except (OSError, ValueError) as error:
        raise ValueError(f"無法讀取 config.json，請檢查檔案、JSON 格式及 2 MiB 大小限制：{display_text(path)}") from error
    roots = data.get("roots") if isinstance(data, dict) else None
    if not isinstance(roots, list) or len(roots) != 3:
        raise ValueError("config.json 的 roots 必須剛好包含三個搜尋根目錄。")
    parsed = []
    for index, root in enumerate(roots, 1):
        if not isinstance(root, dict) or not valid_path_text(root.get("path")):
            raise ValueError(f"第 {index} 個根目錄必須設定有效、非空白的 path。")
        try:
            directory = Path(root["path"]).expanduser()
        except (ValueError, RuntimeError) as error:
            raise ValueError(f"第 {index} 個根目錄無法解析，請使用完整絕對路徑。") from error
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
    cached_directories: int = 0
    read_directories: int = 0

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
        if self.cached_directories or self.read_directories:
            result += f"；重用 {self.cached_directories} 個資料夾快取，新讀取 {self.read_directories} 個資料夾"
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


def path_item(path: Path, label: str = "", require_dir: bool = False, index: DirectoryIndex | None = None) -> Item:
    """取得首頁／收藏項目的類型與時間；失效路徑仍保留供移除。"""
    try:
        metadata = plain_metadata(path)
        saved = index.directory_item(path) if index is not None and stat.S_ISDIR(metadata.st_mode) else None
        if saved is not None:
            return replace(saved, label=label)
        is_dir = stat.S_ISDIR(metadata.st_mode)
        if require_dir and not is_dir:
            return Item(path, False, label, f"{display_text(path)}：不是資料夾", metadata.st_mtime)
        if not is_dir and not stat.S_ISREG(metadata.st_mode):
            return Item(path, False, label, f"{display_text(path)}：不是一般檔案或資料夾", metadata.st_mtime)
        error = validate_folder(path) if is_dir else ""
        return Item(path, is_dir, label, error, metadata.st_mtime)
    except OSError as error:
        return Item(path, require_dir, label, path_error(path, error))
    except ValueError:
        return Item(path, require_dir, label, f"{display_text(path)}：路徑格式錯誤")


@dataclass(frozen=True)
class SearchProgress:
    scanned: int = 0
    found: int = 0
    directory: Path | None = None
    errors: int = 0
    skipped_links: int = 0
    cached_directories: int = 0
    read_directories: int = 0


@dataclass(frozen=True)
class DirectorySnapshot:
    """保存一次完整枚舉的所有名稱與修改時間，不受 200 筆結果上限影響。"""
    root: Item
    entries: tuple[Item | str | None, ...]
    signature: tuple[int, ...] = ()


def directory_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (metadata.st_mtime_ns, metadata.st_ctime_ns, metadata.st_dev, metadata.st_ino)


@dataclass(frozen=True)
class IndexCheck:
    checked: int = 0
    changed: int = 0
    errors: tuple[str, ...] = ()
    cancelled: bool = False


@dataclass(frozen=True)
class DirectoryListing:
    root: Item | None
    entries: Iterator[Item | str | None]
    cached: bool = False


class DirectoryIndex:
    """共用名稱索引；可保存至本機 SQLite，核對時只重讀變動的資料夾。"""
    def __init__(self, file: Path | None = None):
        self._snapshots: dict[str, DirectorySnapshot] = {}
        self._locks: dict[str, Lock] = {}
        self._guard = Lock()
        self._generation = 0
        self._versions: dict[str, int] = {}
        self.file = file
        self.error = ""
        self._database_lock = Lock()
        self._pending: dict[str, DirectorySnapshot | None] = {}
        self._reset_pending = False
        self._unverified: set[str] = set()

    @property
    def unverified_count(self) -> int:
        with self._guard:
            return len(self._unverified)

    def _connect(self) -> sqlite3.Connection:
        try:
            metadata = plain_metadata(self.file)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("索引路徑不是一般檔案")
        except FileNotFoundError:
            pass
        connection = sqlite3.connect(self.file, timeout=2)
        try:
            # 不覆寫不屬於本程式、版本不符或損壞的資料庫。
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and not {"finder_meta", "finder_directories"}.issubset(tables):
                raise ValueError("索引資料庫格式不符")
            connection.execute("CREATE TABLE IF NOT EXISTS finder_meta (version INTEGER NOT NULL)")
            version = connection.execute("SELECT version FROM finder_meta").fetchone()
            if version is None:
                connection.execute("INSERT INTO finder_meta VALUES (1)")
            elif version != (1,):
                raise ValueError("索引版本不符")
            connection.execute("CREATE TABLE IF NOT EXISTS finder_directories (key TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            connection.commit()
            return connection
        except Exception:
            connection.close()
            raise

    def _storage_error(self) -> None:
        self.error = "索引快取無法讀寫，已改用記憶體索引；搜尋仍可使用。"

    def load(self, scopes: tuple[Path, ...], cancel: Event) -> int:
        """只還原本次搜尋／收藏範圍的完整目錄；磁碟核對在背景另行執行。"""
        if self.file is None or self.error:
            return 0
        prefixes = tuple(path_key(scope).rstrip("\\") for scope in scopes)
        restored = 0
        restored_entries = 0
        try:
            with self._database_lock:
                connection = self._connect()
                try:
                    # 每份快取最多 64 MiB；避免異常快取造成無界單次讀取。
                    for (key,) in connection.execute("SELECT key FROM finder_directories WHERE typeof(key) = 'text' AND typeof(payload) = 'text' AND length(CAST(payload AS BLOB)) <= ?", (MAX_CACHE_RECORD_BYTES,)):
                        if cancel.is_set():
                            break
                        if not any(key == prefix or key.startswith(prefix + "\\") for prefix in prefixes):
                            continue
                        payload = connection.execute("SELECT payload FROM finder_directories WHERE key = ?", (key,)).fetchone()[0]
                        try:
                            data = json.loads(payload)
                            directory, modified, signature, records = data
                            if not valid_path_text(directory) or not Path(directory).is_absolute() or path_key(directory) != key:
                                raise ValueError("無效目錄")
                            if not isinstance(signature, list) or len(signature) != 4 or any(type(value) is not int for value in signature):
                                raise ValueError("無效目錄時間")
                            if not isinstance(records, list):
                                raise ValueError("無效項目")
                            if len(records) > MAX_RESTORED_ENTRIES - restored_entries:
                                continue
                            root = Item(Path(directory), True, modified_at=self._timestamp(modified))
                            entries = []
                            for record in records:
                                if cancel.is_set():
                                    return restored
                                if record is None:
                                    entries.append(None)
                                    continue
                                path, is_dir, modified_at = record
                                if not valid_path_text(path) or not Path(path).is_absolute() or path_key(Path(path).parent) != key or type(is_dir) is not bool:
                                    raise ValueError("無效子項目")
                                entries.append(Item(Path(path), is_dir, modified_at=self._timestamp(modified_at)))
                            snapshot = DirectorySnapshot(root, tuple(entries), tuple(signature))
                        except (ValueError, TypeError, RecursionError):
                            continue
                        with self._guard:
                            self._snapshots[key] = snapshot
                            self._unverified.add(key)
                        restored += 1
                        restored_entries += len(entries)
                finally:
                    connection.close()
        except (OSError, sqlite3.Error, ValueError):
            self._storage_error()
        return restored

    @staticmethod
    def _timestamp(value: object) -> float | None:
        if value is None:
            return None
        if type(value) not in {int, float} or not (-1e15 < value < 1e15):
            raise ValueError("無效修改時間")
        return float(value)

    def flush(self) -> None:
        """背景保存有變動的快取；不在 UI 執行 SQL 或序列化。"""
        if self.file is None or self.error:
            return
        with self._database_lock:
            with self._guard:
                pending, self._pending = self._pending, {}
                reset, self._reset_pending = self._reset_pending, False
            if not pending and not reset:
                return
            try:
                connection = self._connect()
                try:
                    with connection:
                        if reset:
                            connection.execute("DELETE FROM finder_directories")
                        for key, snapshot in pending.items():
                            if snapshot is None:
                                connection.execute("DELETE FROM finder_directories WHERE key = ?", (key,))
                            else:
                                records = [None if item is None else [str(item.path), item.is_dir, item.modified_at] for item in snapshot.entries]
                                payload = json.dumps([str(snapshot.root.path), snapshot.root.modified_at, snapshot.signature, records], ensure_ascii=True, separators=(",", ":"))
                                if len(payload) <= MAX_CACHE_RECORD_BYTES:
                                    connection.execute("INSERT OR REPLACE INTO finder_directories VALUES (?, ?)", (key, payload))
                                else:
                                    connection.execute("DELETE FROM finder_directories WHERE key = ?", (key,))
                finally:
                    connection.close()
            except (OSError, sqlite3.Error, ValueError):
                self._storage_error()

    def check_changes(self, cancel: Event, directory: Path | None = None, *, refresh_times: bool = False) -> IndexCheck:
        """核對每個已索引目錄的時間／身分；只有變動目錄才重新枚舉。"""
        with self._guard:
            snapshots = list(self._snapshots.items()) if directory is None else [(path_key(directory), self._snapshots.get(path_key(directory)))]
        checked = changed = 0
        errors = []
        for key, snapshot in snapshots:
            if cancel.is_set():
                break
            if snapshot is None:
                continue
            checked += 1
            try:
                metadata = plain_metadata(snapshot.root.path)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise NotADirectoryError(str(snapshot.root.path))
                if directory_signature(metadata) != snapshot.signature:
                    with self._guard:
                        if self._snapshots.get(key) is snapshot:
                            self._discard(key)
                            changed += 1
                    continue
                entries = snapshot.entries
                if refresh_times:
                    updated = []
                    for item in entries:
                        if cancel.is_set():
                            break
                        if item is None:
                            updated.append(None)
                        else:
                            info = plain_metadata(item.path)
                            updated.append(replace(item, is_dir=stat.S_ISDIR(info.st_mode), modified_at=info.st_mtime))
                    if cancel.is_set():
                        break
                    entries = tuple(updated)
                updated_snapshot = DirectorySnapshot(replace(snapshot.root, modified_at=metadata.st_mtime), entries, snapshot.signature)
                with self._guard:
                    if self._snapshots.get(key) is snapshot:
                        self._unverified.discard(key)
                        if updated_snapshot != snapshot:
                            self._snapshots[key] = updated_snapshot
                            self._pending[key] = updated_snapshot
            except OSError as error:
                errors.append(path_error(snapshot.root.path, error))
                with self._guard:
                    if self._snapshots.get(key) is snapshot:
                        self._discard(key)
                        changed += 1
        return IndexCheck(checked, changed, tuple(errors), cancel.is_set())

    def _discard(self, key: str) -> None:
        self._versions[key] = self._versions.get(key, 0) + 1
        self._snapshots.pop(key, None)
        self._unverified.discard(key)
        self._pending[key] = None

    def directory_item(self, directory: Path) -> Item | None:
        with self._guard:
            snapshot = self._snapshots.get(path_key(directory))
            return snapshot.root if snapshot is not None else None

    def invalidate(self, directory: Path | None = None) -> None:
        """更新當前資料夾或整份索引；舊工作不得在更新後重新寫回快取。"""
        with self._guard:
            if directory is None:
                self._generation += 1
                self._pending.clear()
                self._reset_pending = True
                self._snapshots.clear()
                self._versions.clear()
                self._unverified.clear()
            else:
                key = path_key(directory)
                self._discard(key)

    @contextmanager
    def listing(self, directory: Path, cancel: Event) -> Iterator[DirectoryListing]:
        """逐筆提供名稱，只有枚舉到底且沒有錯誤才保存，取消不保存半份資料。"""
        key = path_key(directory)
        with self._guard:
            lock = self._locks.setdefault(key, Lock())
        while not cancel.is_set():
            if lock.acquire(timeout=0.05):
                break
        else:
            yield DirectoryListing(None, iter(()))
            return
        try:
            if cancel.is_set():
                yield DirectoryListing(None, iter(()))
                return
            with self._guard:
                version = (self._generation, self._versions.get(key, 0))
                snapshot = self._snapshots.get(key)
            if snapshot is not None:
                yield DirectoryListing(snapshot.root, iter(snapshot.entries), True)
                return
            records: list[Item | str | None] = []
            complete = failed = False
            metadata = plain_metadata(directory)
            with os.scandir(directory) as entries:
                root = Item(directory, True, modified_at=metadata.st_mtime)

                def read_entries() -> Iterator[Item | str | None]:
                    nonlocal complete, failed
                    for entry in entries:
                        if cancel.is_set():
                            return
                        try:
                            record = entry_item(entry)
                        except OSError as error:
                            record = path_error(entry.path, error)
                            failed = True
                        records.append(record)
                        yield record
                    complete = True

                yield DirectoryListing(root, read_entries())
            if complete and not failed and not cancel.is_set():
                # 掃描途中變動的資料夾不保存為完整索引。
                after = plain_metadata(directory)
                snapshot = DirectorySnapshot(root, tuple(records), directory_signature(metadata))
                with self._guard:
                    if version == (self._generation, self._versions.get(key, 0)) and directory_signature(after) == snapshot.signature:
                        self._snapshots[key] = snapshot
                        self._unverified.discard(key)
                        self._pending[key] = snapshot
        finally:
            lock.release()


def scan_names(config: Config, query: str, cancel: Event | None = None, progress: Callable[[SearchProgress], None] | None = None, index: DirectoryIndex | None = None) -> ScanReport:
    cancel = cancel or Event()
    index = index if index is not None else DirectoryIndex()
    best: list[tuple[tuple, Item]] = []
    errors: list[str] = []
    visited: set[str] = set()
    total = scanned = skipped_links = 0
    cached_directories = read_directories = 0
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
        # 限制背景訊息頻率；分別回報快取重用與真正的磁碟讀取。
        if force or now - last_progress >= PROGRESS_INTERVAL_SECONDS:
            last_progress = now
            progress(SearchProgress(scanned, total, directory, len(errors), skipped_links, cached_directories, read_directories))

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
            with index.listing(directory, cancel) as listing:
                if listing.root is None:
                    continue
                if listing.cached:
                    cached_directories += 1
                else:
                    read_directories += 1
                if is_root:
                    consider(listing.root)
                for item in listing.entries:
                    if cancel.is_set():
                        break
                    publish_progress()
                    if scanned >= config.max_scan_entries:
                        limited = True
                        break
                    scanned += 1
                    if isinstance(item, str):
                        errors.append(item)
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
        except ExcludedLinkError:
            skipped_links += 1
        except OSError as error:
            errors.append(path_error(directory, error))
    publish_progress(force=True)
    return ScanReport(tuple(item for _, item in best), total, scanned, tuple(errors), cancel.is_set(), limited, skipped_links, cached_directories, read_directories)


def browse_folder(directory: Path, cancel: Event | None = None, index: DirectoryIndex | None = None) -> ScanReport:
    """只讀取直接子項目；此處不套用搜尋的 200 筆顯示上限。"""
    cancel = cancel or Event()
    track_cache = index is not None
    index = index if index is not None else DirectoryIndex()
    items, errors = [], []
    scanned = skipped_links = 0
    cached_directories = read_directories = 0
    try:
        # 瀏覽是直接操作；即使已有快取，也拒絕被替換成連結的目標。
        metadata = plain_metadata(directory)
        if not stat.S_ISDIR(metadata.st_mode):
            raise NotADirectoryError(str(directory))
        with index.listing(directory, cancel) as listing:
            if listing.root is not None and track_cache:
                cached_directories = int(listing.cached)
                read_directories = int(not listing.cached)
            for item in listing.entries:
                if cancel.is_set():
                    break
                scanned += 1
                if isinstance(item, str):
                    errors.append(item)
                elif item is None:
                    skipped_links += 1
                else:
                    items.append(item)
    except ExcludedLinkError:
        skipped_links += 1
    except OSError as error:
        errors.append(path_error(directory, error))
    except ValueError:
        errors.append(f"{display_text(directory)}：路徑格式錯誤")
    items.sort(key=lambda item: (not item.is_dir, item.name.casefold(), path_key(item.path)))
    return ScanReport(tuple(items), len(items), scanned, tuple(errors), cancel.is_set(), False, skipped_links, cached_directories, read_directories)


def validate_folder(path: Path) -> str:
    try:
        metadata = plain_metadata(path)
        if not stat.S_ISDIR(metadata.st_mode):
            raise NotADirectoryError(str(path))
        with os.scandir(path):
            pass
    except OSError as error:
        return path_error(path, error)
    except ValueError:
        return f"{display_text(path)}：路徑格式錯誤"
    return ""


class Favorites:
    def __init__(self, file: Path):
        self.file = file
        self.paths: list[Path] = []
        self.error = ""
        try:
            data = read_json(file)
            if not isinstance(data, list) or any(not valid_path_text(path) or not Path(path).is_absolute() for path in data):
                raise ValueError("最愛格式錯誤")
            keys = set()
            for value in data:
                if path_key(value) not in keys:
                    self.paths.append(Path(value))
                    keys.add(path_key(value))
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.error = f"無法讀取最愛檔案，收藏已停用；請檢查格式、路徑及 2 MiB 大小限制後重啟：{display_text(file)}"

    def contains(self, path: Path) -> bool:
        return any(path_key(saved) == path_key(path) for saved in self.paths)

    def toggle(self, path: Path) -> bool:
        if self.error:
            raise ValueError(self.error)
        if not valid_path_text(str(path)) or not path.is_absolute():
            raise ValueError("收藏必須使用有效的完整絕對路徑。")
        exists = self.contains(path)
        if not exists:
            item = path_item(path)
            if item.error:
                raise ValueError(f"只能收藏可讀取的一般檔案或資料夾。{item.error}")
        updated = [saved for saved in self.paths if path_key(saved) != path_key(path)] if exists else [*self.paths, path]
        serialized = json.dumps([str(saved) for saved in updated], ensure_ascii=False, indent=2) + "\n"
        if len(serialized.encode("utf-8")) > MAX_JSON_BYTES:
            raise ValueError("最愛保存失敗：超過 2 MiB 大小限制，原有收藏保持不變。")
        temporary = None
        try:
            # 暫存檔與目的檔位於同一資料夾，成功寫入後再原子替換。
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.file.parent, prefix=".favorites-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(serialized)
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


def list_favorites(favorites: Favorites, cancel: Event, index: DirectoryIndex | None = None) -> tuple[Item, ...]:
    """只讀取已收藏路徑，不展開其中的資料夾；資料夾排序在前。"""
    items: list[Item] = []
    for path in tuple(favorites.paths):
        if cancel.is_set():
            break
        items.append(path_item(path, index=index))
    items.sort(key=lambda item: (not item.is_dir, item.name.casefold(), path_key(item.path)))
    return tuple(items)


def list_home_items(config: Config, favorites: Favorites, cancel: Event, menu: str = "folders", index: DirectoryIndex | None = None) -> tuple[Item, ...]:
    """讀取指定分頁的首頁項目，前端不直接存取檔案系統。"""
    if menu == "favorites":
        return list_favorites(favorites, cancel, index)
    items: list[Item] = []
    for root in config.roots:
        if cancel.is_set():
            break
        items.append(path_item(root.path, root.name, require_dir=True, index=index))
    return tuple(items)


def search_favorites(favorites: Favorites, query: str, cancel: Event, index: DirectoryIndex | None = None) -> ScanReport:
    """依完整關鍵字篩選收藏名稱，保留取消與讀取錯誤資訊。"""
    items = list_favorites(favorites, cancel, index)
    matches = sorted((item for item in items if match_rank(query, item.name) is not None), key=lambda item: search_key(query, item))
    return ScanReport(tuple(matches[:RESULT_LIMIT]), len(matches), len(items), tuple(item.error for item in items if item.error), cancel.is_set())


def open_file(path: Path, opener: Callable[[str], None] | None) -> str:
    """要求作業系統開啟單一檔案，回傳前端可顯示的結果訊息。"""
    try:
        if not stat.S_ISREG(plain_metadata(path).st_mode):
            raise FileNotFoundError(str(path))
        if opener is None:
            return "此工具的檔案開啟功能僅支援 Windows。"
        opener(str(path))
        return f"已要求 Windows 開啟：{display_text(path)}"
    except OSError as error:
        return path_error(path, error)
    except ValueError:
        return f"{display_text(path)}：路徑格式錯誤"


def open_containing_folder(path: Path, opener: Callable[[str], None] | None) -> str:
    """只要求 Windows 開啟父資料夾，不執行選取的檔案。"""
    error = validate_folder(path.parent)
    if error:
        return error
    if opener is None:
        return "此工具的檔案開啟功能僅支援 Windows。"
    try:
        opener(str(path.parent))
        return f"已要求 Windows 開啟所在資料夾：{display_text(path.parent)}"
    except OSError as error:
        return path_error(path.parent, error)
