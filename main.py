"""極簡檔名搜尋與資料夾瀏覽工具；不修改使用者的原始檔案。"""

from __future__ import annotations

import argparse
import bisect
import json
import ntpath
import os
from pathlib import Path
import stat
import sys
import tempfile
from dataclasses import dataclass, replace
from threading import Event
from time import monotonic
from typing import Callable

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option


RESULT_LIMIT = 200
DEBOUNCE_SECONDS = 0.25
PROGRESS_INTERVAL_SECONDS = 0.2
# 以深藍灰底與柔和霧藍標示焦點，保持終端長時間閱讀的清楚對比。
BLUE_THEME = Theme(
    name="file-finder-blue",
    primary="#7896b9",
    secondary="#92a4ba",
    accent="#7896b9",
    foreground="#d8e2ee",
    background="#101722",
    surface="#172130",
    panel="#1d2b3d",
    warning="#c5b48e",
    error="#d49a9a",
    success="#91b4a3",
    dark=True,
    variables={
        "text-muted": "#92a4ba",
        "border-blurred": "#1d2b3d",
        "border": "#7896b9",
        "block-cursor-background": "#2a405a",
        "block-cursor-foreground": "#d8e2ee",
        "block-cursor-text-style": "bold",
        "block-cursor-blurred-background": "#1d2b3d",
        "block-cursor-blurred-foreground": "#92a4ba",
        "block-cursor-blurred-text-style": "none",
        "block-hover-background": "#223246",
        "input-cursor-background": "#7896b9",
        "input-cursor-foreground": "#101722",
        "input-selection-background": "#2a405a",
        "input-selection-foreground": "#d8e2ee",
        "scrollbar": "#2a405a",
        "scrollbar-hover": "#7896b9",
        "scrollbar-active": "#7896b9",
    },
)
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
EXECUTABLE_SUFFIXES = {
    ".exe", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".vbs",
    ".vbe", ".js", ".jse", ".wsf", ".wsh", ".msi", ".msp", ".scr",
    ".cpl", ".hta", ".reg", ".lnk", ".url", ".py", ".pyw", ".jar",
}


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

    @property
    def name(self) -> str:
        return self.path.name or str(self.path)


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
    """連續子字串優先，其次為依序字元；不讀取檔案內容。"""
    query, name = query.casefold(), name.casefold()
    if query in name:
        return 0
    position = 0
    for character in name:
        if position < len(query) and character == query[position]:
            position += 1
    return 1 if position == len(query) else None


def search_key(query: str, item: Item) -> tuple:
    return (match_rank(query, item.name), not item.is_dir, len(item.name), item.name.casefold(), path_key(item.path))


def entry_item(entry: os.DirEntry) -> Item | None:
    """排除符號連結及所有 Windows reparse point，包含 junction。"""
    metadata = entry.stat(follow_symlinks=False)
    if entry.is_symlink() or getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT:
        return None
    return Item(Path(entry.path), stat.S_ISDIR(metadata.st_mode))


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
    if not query:
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
                    consider(Item(directory, True))
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
            error = validate_folder(path)
            if error:
                raise ValueError(f"只能收藏可讀取的資料夾。{error}")
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


@dataclass
class View:
    kind: str = "home"
    query: str = ""
    directory: Path | None = None
    items: tuple[Item | None, ...] = ()
    report: ScanReport | None = None
    status: str = "準備搜尋"
    selected: int | None = None
    progress: SearchProgress | None = None
    search_started: float | None = None


class Navigation:
    def __init__(self):
        self.view = View()
        self.history: list[View] = []
        self.forward_history: list[View] = []

    def search(self, query: str) -> None:
        self.history.clear()
        self.forward_history.clear()
        self.view = View("search", query=query, status="等待輸入完成…")

    def enter(self, directory: Path) -> None:
        self.history.append(replace(self.view))
        self.forward_history.clear()
        self.view = View("browse", directory=directory, status="正在讀取資料夾…")

    def back(self) -> bool:
        if not self.history:
            return False
        self.forward_history.append(replace(self.view))
        self.view = self.history.pop()
        return True

    def forward(self) -> bool:
        if not self.forward_history:
            return False
        self.history.append(replace(self.view))
        self.view = self.forward_history.pop()
        return True

    def home(self) -> None:
        self.history.clear()
        self.forward_history.clear()
        self.view = View()


class RequestGate:
    """在輸入改變當下使舊結果失效，不必等到 debounce 結束。"""
    def __init__(self):
        self.generation = 0
        self.cancel = Event()

    def invalidate(self) -> tuple[int, Event]:
        self.cancel.set()
        self.generation += 1
        self.cancel = Event()
        return self.generation, self.cancel

    def accepts(self, generation: int) -> bool:
        return generation == self.generation and not self.cancel.is_set()


class IOResult(Message):
    def __init__(self, generation: int, kind: str, payload):
        super().__init__()
        self.generation, self.kind, self.payload = generation, kind, payload


class FavoriteResult(Message):
    def __init__(self, status: str):
        super().__init__()
        self.status = status


class ConfirmOpen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "取消")]
    DEFAULT_CSS = """
    ConfirmOpen { align: center middle; background: $background 70%; }
    ConfirmOpen > Vertical { width: 80%; height: auto; max-height: 90%; padding: 1 2; border: round $warning; background: $surface; }
    ConfirmOpen Horizontal { height: 3; align: center middle; }
    ConfirmOpen Button { margin: 0 1; }
    """

    def __init__(self, path: Path):
        super().__init__()
        self.path = path

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"此檔案可能執行程式、腳本或捷徑目標。確定要開啟嗎？\n\n{self.path}", markup=False)
            with Horizontal():
                yield Button("取消", id="cancel")
                yield Button("確認開啟", id="confirm", variant="warning")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


class ErrorDetails(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "返回")]
    DEFAULT_CSS = """
    ErrorDetails { align: center middle; background: $background 70%; }
    ErrorDetails > Vertical { width: 90%; height: 85%; padding: 1 2; border: round $warning; background: $surface; }
    ErrorDetails Static { height: auto; }
    #error-list { height: 1fr; margin: 1 0; }
    """

    def __init__(self, errors: tuple[str, ...]):
        super().__init__()
        self.errors = errors

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"讀取錯誤（共 {len(self.errors)} 項）", markup=False)
            yield OptionList(*(Option(f"{index}. {error}") for index, error in enumerate(self.errors, 1)), id="error-list", markup=False)
            yield Static("↑↓／PageUp／PageDown 捲動 | Esc 返回", markup=False)
            yield Button("返回", id="error-close")

    def on_mount(self) -> None:
        self.query_one("#error-list", OptionList).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()

    def action_close(self) -> None:
        self.dismiss()


class FileFinderApp(App):
    TITLE = "File Finder"
    CSS = """
    Screen { layout: vertical; background: $background; color: $foreground; }
    #title { height: 1; margin: 1 2 0 2; color: $primary; text-style: bold; }
    #search { margin: 1 2; border: round $border-blurred; background: $surface; }
    #search:focus { border: round $primary; background-tint: transparent; }
    #search > .input--placeholder { color: $text-muted; }
    #location { height: auto; max-height: 3; margin: 0 2; color: $text-muted; }
    #results { height: 1fr; margin: 0 2; border: round $border-blurred; background: $surface; scrollbar-size-vertical: 1; }
    #results:focus { border: round $primary; background-tint: transparent; }
    #results > .option-list--option-disabled { color: $primary; text-style: bold; }
    #status { height: auto; max-height: 5; margin: 0 2; padding: 0 1; background: $panel; color: $foreground; }
    #keys { height: auto; margin: 0 2 1 2; padding: 0 1; color: $text-muted; }
    #error-list { border: round $border-blurred; background: $background; scrollbar-size-vertical: 1; }
    #error-list:focus { border: round $primary; background-tint: transparent; }
    """
    BINDINGS = [
        Binding("ctrl+l", "focus_search", "搜尋", priority=True),
        Binding("escape", "home", "首頁"),
        Binding("ctrl+q", "quit", "結束", priority=True),
        Binding("alt+left", "history_back", "上一頁", priority=True),
        Binding("alt+right", "forward", "下一頁", priority=True),
        Binding("ctrl+e", "errors", "錯誤詳情", priority=True),
        Binding("f,F", "favorite", "最愛", show=False),
        Binding("backspace", "back", "返回", show=False),
    ]

    def __init__(self, config_path: Path, favorites_path: Path | None = None, opener: Callable[[str], None] | None = None):
        super().__init__()
        self.register_theme(BLUE_THEME)
        self.theme = BLUE_THEME.name
        self.config_path = config_path.absolute()
        self.favorites_path = favorites_path.absolute() if favorites_path else self.config_path.with_name("favorites.json")
        self.configuration_error = ""
        self.config: Config | None = None
        self.favorites: Favorites | None = None
        self.navigation = Navigation()
        self.gate = RequestGate()
        self.debounce_timer = None
        self.favorite_busy = False
        self.opener = opener or getattr(os, "startfile", None)

    def compose(self) -> ComposeResult:
        yield Static("File Finder", id="title")
        yield Input(placeholder="搜尋檔案或資料夾…", id="search", select_on_focus=False)
        yield Static(f"首頁\n設定檔：{self.config_path}", id="location", markup=False)
        yield OptionList(id="results", markup=False)
        yield Static("正在驗證設定與根目錄…", id="status", markup=False)
        yield Static("↑↓ 選取 | Enter 開啟 | F 最愛 | Backspace／Alt+← 返回 | Alt+→ 前進 | Esc 首頁\nCtrl+L 搜尋 | Ctrl+E 錯誤詳情 | Ctrl+Q 結束 | 滑鼠單擊選取並開啟", id="keys", markup=False)

    def on_mount(self) -> None:
        self.set_interval(PROGRESS_INTERVAL_SECONDS, self.refresh_search_progress)
        generation, cancel = self.gate.invalidate()
        self.initialize(generation, cancel)
        self.query_one(Input).focus()

    @work(thread=True, exclusive=True, group="io")
    def initialize(self, generation: int, cancel: Event) -> None:
        try:
            config = load_config(self.config_path)
            favorites = Favorites(self.favorites_path)
            payload = (config, favorites, self.home_items(config, favorites, cancel), "")
        except ValueError as error:
            payload = (None, None, (), str(error))
        self.post_message(IOResult(generation, "initialize", payload))

    @staticmethod
    def home_items(config: Config, favorites: Favorites, cancel: Event) -> tuple[Item | None, ...]:
        items: list[Item | None] = [None]
        for path in favorites.paths:
            if cancel.is_set():
                break
            items.append(Item(path, True, str(path), validate_folder(path)))
        items.append(None)
        for root in config.roots:
            if cancel.is_set():
                break
            items.append(Item(root.path, True, root.name, validate_folder(root.path)))
        return tuple(items)

    def invalidate(self) -> tuple[int, Event]:
        if self.debounce_timer is not None:
            self.debounce_timer.stop()
            self.debounce_timer = None
        self.workers.cancel_group(self, "io")
        return self.gate.invalidate()

    def set_input(self, value: str) -> None:
        # 程式內部清除或還原輸入，不觸發新的搜尋。
        with self.prevent(Input.Changed):
            search = self.query_one(Input)
            search.value = value
            search.cursor_position = len(value)

    def on_input_changed(self, event: Input.Changed) -> None:
        self.search_changed(event.value)

    def search_changed(self, value: str) -> None:
        if self.config is None:
            return
        generation, cancel = self.invalidate()
        query = value.strip()
        if query:
            self.navigation.search(query)
            self.render_view()
            self.debounce_timer = self.set_timer(DEBOUNCE_SECONDS, lambda: self.start_search(generation, query, cancel))
        else:
            self.navigation.home()
            self.render_view()
            self.load_home(generation, cancel)

    def start_search(self, generation: int, query: str, cancel: Event) -> None:
        self.debounce_timer = None
        if self.gate.accepts(generation):
            self.navigation.view.search_started = monotonic()
            self.navigation.view.progress = SearchProgress()
            self.refresh_search_progress()
            self.search_worker(generation, query, cancel)

    def refresh_search_progress(self) -> None:
        view = self.navigation.view
        if view.kind != "search" or view.report is not None or view.search_started is None:
            return
        progress = view.progress or SearchProgress()
        elapsed = max(0.0, monotonic() - view.search_started)
        status = f"正在搜尋…耗時 {elapsed:.1f} 秒；已掃描 {progress.scanned} 個項目；目前找到至少 {progress.found} 筆（尚未完成）"
        if progress.directory is not None:
            status += f"\n掃描資料夾：{progress.directory}"
        if progress.errors or progress.skipped_links:
            status += f"\n讀取錯誤 {progress.errors} 項；已排除 {progress.skipped_links} 個連結／reparse point"
        self.set_status(status)

    @work(thread=True, exclusive=True, group="io")
    def search_worker(self, generation: int, query: str, cancel: Event) -> None:
        def progress(snapshot: SearchProgress) -> None:
            self.post_message(IOResult(generation, "progress", snapshot))

        self.post_message(IOResult(generation, "search", scan_names(self.config, query, cancel, progress)))

    @work(thread=True, exclusive=True, group="io")
    def load_home(self, generation: int, cancel: Event) -> None:
        self.post_message(IOResult(generation, "home", self.home_items(self.config, self.favorites, cancel)))

    @work(thread=True, exclusive=True, group="io")
    def load_folder(self, generation: int, directory: Path, cancel: Event) -> None:
        self.post_message(IOResult(generation, "browse", browse_folder(directory, cancel)))

    @on(IOResult)
    def on_io_result(self, message: IOResult) -> None:
        if not self.gate.accepts(message.generation):
            return
        view = self.navigation.view
        if message.kind == "progress":
            # 舊請求及已完成搜尋的延遲進度不可覆蓋目前狀態。
            if view.kind == "search" and view.report is None:
                view.progress = message.payload
                self.refresh_search_progress()
            return
        if message.kind == "initialize":
            self.config, self.favorites, items, error = message.payload
            self.configuration_error = error
            view.items = items
            view.status = error or self.home_status(items)
            self.query_one(Input).disabled = self.config is None
        elif message.kind == "home":
            view.items = message.payload
            view.status = self.home_status(view.items)
        elif message.kind in {"search", "browse"}:
            view.report = message.payload
            view.items = view.report.items
            view.status = view.report.status() if message.kind == "search" else self.browse_status(view.report)
        elif message.kind == "open":
            self.set_status(message.payload)
            return
        self.render_view()
        if message.kind == "initialize" and self.config and self.query_one(Input).value.strip():
            self.search_changed(self.query_one(Input).value)

    def home_status(self, items: tuple[Item | None, ...]) -> str:
        invalid = [item.error for item in items if item is not None and item.error]
        warnings = [self.favorites.error] if self.favorites and self.favorites.error else []
        if invalid:
            warnings.append(f"{len(invalid)} 個根目錄／最愛無法讀取；{invalid[0]}")
        if warnings:
            warnings.append("Ctrl+E 查看全部錯誤")
        return "準備搜尋" + ("\n" + "\n".join(warnings) if warnings else "")

    @staticmethod
    def browse_status(report: ScanReport) -> str:
        text = f"直接子項目 {report.total} 筆" if report.items else "空資料夾"
        if report.errors or report.cancelled:
            text = f"資料夾讀取不完整，已讀取 {report.total} 筆"
        if report.errors:
            text += f"；{len(report.errors)} 項錯誤；Ctrl+E 查看全部錯誤\n{report.errors[0]}"
        if report.skipped_links:
            text += f"；已排除 {report.skipped_links} 個連結／reparse point"
        return text

    def set_status(self, text: str) -> None:
        self.navigation.view.status = text
        self.query_one("#status", Static).update("狀態：" + text)

    def render_view(self) -> None:
        view = self.navigation.view
        options = []
        section = 0
        for item in view.items:
            if item is None:
                section += 1
                title = "★ 我的最愛" if section == 1 else "📂 搜尋根目錄"
                options.append(Option(title, disabled=True))
                continue
            star = " ★" if self.favorites and self.favorites.contains(item.path) else ""
            invalid = " [失效／無法讀取]" if item.error else ""
            icon = "📁" if item.is_dir else "📄"
            label = item.label or item.name
            # 首頁根目錄顯示完整搜尋路徑；檔案與子項目顯示父目錄以區分同名項目。
            location = item.path if view.kind == "home" and section == 2 else item.path.parent
            prompt = f"{icon} {label}{star}{invalid}\n  {location}"
            options.append(Option(prompt))
        results = self.query_one(OptionList)
        previous = view.selected
        results.clear_options().add_options(options)
        if previous is not None and previous < len(view.items) and view.items[previous] is not None:
            results.highlighted = previous
        elif options:
            results.action_first()
        view.selected = results.highlighted
        location = str(view.directory) if view.kind == "browse" else (f"搜尋：{view.query}（固定三個根目錄）" if view.kind == "search" else f"首頁\n設定檔：{self.config_path}")
        self.query_one("#location", Static).update(location)
        self.set_status(view.status)

    @on(OptionList.OptionHighlighted, "#results")
    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.navigation.view.selected = event.option_index

    def on_key(self, event: events.Key) -> None:
        if isinstance(self.focused, Input) and event.key in {"down", "up"}:
            results = self.query_one(OptionList)
            results.focus()
            if event.key == "down":
                results.action_first()
            else:
                results.action_last()
            event.stop()
            event.prevent_default()

    @on(OptionList.OptionSelected, "#results")
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        view = self.navigation.view
        if event.option_index >= len(view.items):
            return
        item = view.items[event.option_index]
        if item is None:
            return
        view.selected = event.option_index
        if item.is_dir:
            generation, cancel = self.invalidate()
            self.navigation.enter(item.path)
            self.set_input("")
            self.render_view()
            self.query_one(OptionList).focus()
            self.load_folder(generation, item.path, cancel)
        elif item.path.suffix.casefold() in EXECUTABLE_SUFFIXES:
            self.push_screen(ConfirmOpen(item.path), lambda confirmed: self.open_item(item) if confirmed else None)
        else:
            self.open_item(item)

    def open_item(self, item: Item) -> None:
        self.open_worker(self.gate.generation, item.path)

    @work(thread=True, group="open")
    def open_worker(self, generation: int, path: Path) -> None:
        try:
            if not path.is_file():
                raise FileNotFoundError(str(path))
            if self.opener is None:
                status = "此工具的檔案開啟功能僅支援 Windows。"
            else:
                self.opener(str(path))
                status = f"已要求 Windows 開啟：{path}"
        except OSError as error:
            status = path_error(path, error)
        self.post_message(IOResult(generation, "open", status))

    def action_focus_search(self) -> None:
        if not isinstance(self.screen, ModalScreen):
            self.query_one(Input).focus()

    def action_home(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        if self.config is None:
            # 初始化尚未完成或設定無效時，保留目前提示與初始化工作。
            self.set_input("")
            return
        generation, cancel = self.invalidate()
        self.navigation.home()
        self.set_input("")
        self.render_view()
        if self.config and self.favorites:
            self.load_home(generation, cancel)
        self.query_one(Input).focus()

    def action_back(self) -> None:
        if isinstance(self.screen, ModalScreen) or not isinstance(self.focused, OptionList):
            return
        self.action_history_back()

    def action_history_back(self) -> None:
        if isinstance(self.screen, ModalScreen) or self.config is None:
            return
        if not self.navigation.history:
            self.set_status("沒有可返回的瀏覽畫面；Esc 可返回首頁")
            return
        generation, cancel = self.invalidate()
        self.navigation.back()
        self.restore_navigation_view(generation, cancel)

    def action_forward(self) -> None:
        if isinstance(self.screen, ModalScreen) or self.config is None:
            return
        if not self.navigation.forward_history:
            self.set_status("沒有可前進的瀏覽畫面。")
            return
        generation, cancel = self.invalidate()
        self.navigation.forward()
        self.restore_navigation_view(generation, cancel)

    def restore_navigation_view(self, generation: int, cancel: Event) -> None:
        view = self.navigation.view
        self.set_input(view.query)
        self.render_view()
        if view.kind == "browse":
            # 返回資料夾也重新讀取，不依賴之前的資料夾快取。
            self.load_folder(generation, view.directory, cancel)
        elif view.kind == "home":
            self.load_home(generation, cancel)
        elif view.report is None:
            self.start_search(generation, view.query, cancel)

    def current_errors(self) -> tuple[str, ...]:
        view = self.navigation.view
        if view.report is not None:
            return view.report.errors
        if view.kind == "home":
            errors = [item.error for item in view.items if item is not None and item.error]
            if self.favorites and self.favorites.error:
                errors.append(self.favorites.error)
            if self.configuration_error:
                errors.append(self.configuration_error)
            return tuple(errors)
        return ()

    def action_errors(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        errors = self.current_errors()
        if errors:
            self.push_screen(ErrorDetails(errors))
        else:
            self.set_status("目前畫面沒有讀取錯誤。")

    def action_favorite(self) -> None:
        if isinstance(self.screen, ModalScreen) or not isinstance(self.focused, OptionList) or self.favorites is None:
            return
        index = self.query_one(OptionList).highlighted
        if index is None or index >= len(self.navigation.view.items):
            return
        item = self.navigation.view.items[index]
        if item is None:
            return
        if not item.is_dir:
            self.set_status("只能收藏資料夾，不能收藏單一檔案。")
            return
        if self.favorite_busy:
            return
        self.favorite_busy = True
        self.save_favorite(item.path)

    @work(thread=True, group="favorites")
    def save_favorite(self, path: Path) -> None:
        try:
            added = self.favorites.toggle(path)
            status = f"已{'加入' if added else '取消'}最愛：{path}"
        except ValueError as error:
            status = str(error)
        self.post_message(FavoriteResult(status))

    def on_favorite_result(self, message: FavoriteResult) -> None:
        self.favorite_busy = False
        if self.navigation.view.kind == "home":
            generation, cancel = self.invalidate()
            self.load_home(generation, cancel)
        else:
            self.render_view()
        self.set_status(message.status)

    def action_quit(self) -> None:
        self.invalidate()
        self.gate.cancel.set()
        self.exit()


def main() -> int:
    parser = argparse.ArgumentParser(description="只搜尋名稱、不修改原始檔案的 Windows TUI。")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"), help="設定檔路徑")
    parser.add_argument("--check", action="store_true", help="只驗證設定與三個根目錄，不啟動 TUI 或遞迴搜尋")
    args = parser.parse_args()
    if args.check:
        try:
            config = load_config(args.config)
        except ValueError as error:
            print(str(error))
            return 1
        invalid = False
        for root in config.roots:
            error = validate_folder(root.path)
            print(f"{root.name}：{error or str(root.path) + '（可讀取）'}")
            invalid |= bool(error)
        return int(invalid)
    FileFinderApp(args.config).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
