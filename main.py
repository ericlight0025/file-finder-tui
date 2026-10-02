"""TUI 前端與啟動入口：畫面、鍵盤事件、瀏覽歷史及背景工作協調。"""

from __future__ import annotations

import argparse
from collections import OrderedDict
from collections.abc import Sequence
import os
from pathlib import Path
import sys
from dataclasses import dataclass, replace
from threading import Event
from time import monotonic
from typing import Callable

from rich.text import Text

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.geometry import Region, Size
from textual.strip import Strip
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, Collapsible, Input, OptionList, Select, Static, Tab, Tabs
from textual.widgets.option_list import Option


import backend
from backend import Config, Favorites, Item, ScanReport, SearchOptions, SearchProgress, PROGRESS_INTERVAL_SECONDS, display_text


DEBOUNCE_SECONDS = 0.25
DOUBLE_ESCAPE_SECONDS = 0.5
SCOPE_OPTIONS = (("範圍：全部", "all"), ("目前資料夾", "current"), ("包含子資料夾", "recursive"))
KIND_OPTIONS = (("類型：全部", "all"), ("只看檔案", "files"), ("只看資料夾", "folders"),
                ("SQL", "sql"), ("Word", "word"), ("Excel", "excel"))
SORT_OPTIONS = (("排序：相關性", "relevance"), ("名稱", "name"), ("更新時間↓", "modified"))
# 黑灰為主，僅在焦點與選取列使用低飽和霧藍。
BLUE_THEME = Theme(
    name="file-finder-blue",
    primary="#809fbe",
    secondary="#92979e",
    accent="#809fbe",
    foreground="#d0d2d5",
    background="#0b1016",
    surface="#101820",
    panel="#182330",
    warning="#c5b48e",
    error="#d49a9a",
    success="#91b4a3",
    dark=True,
    variables={
        "text-muted": "#92979e",
        "border-blurred": "#2c3d50",
        "border": "#809fbe",
        "block-cursor-background": "#284563",
        "block-cursor-foreground": "#e0e2e5",
        "block-cursor-text-style": "none",
        "block-cursor-blurred-background": "#23262c",
        "block-cursor-blurred-foreground": "#b5bac2",
        "block-cursor-blurred-text-style": "none",
        "block-hover-background": "#1c2c3c",
        "input-cursor-background": "#a4adb8",
        "input-cursor-foreground": "#0b1016",
        "input-selection-background": "#284563",
        "input-selection-foreground": "#e0e2e5",
        "scrollbar": "#2c3d50",
        "scrollbar-hover": "#587594",
        "scrollbar-active": "#809fbe",
        "finder-focus-border": "#587594",
    },
)
# 三套低飽和配色共用元件樣式；色彩集中設定，切換時不改動搜尋資料。
WARM_THEME = replace(
    BLUE_THEME,
    name="file-finder-warm",
    primary="#ab9d87", secondary="#a49b8d", accent="#ab9d87",
    foreground="#d8d2c8", background="#141210", surface="#1c1915", panel="#25211b",
    variables={**BLUE_THEME.variables,
        "text-muted": "#a49b8d", "border-blurred": "#3b342b", "border": "#ab9d87",
        "finder-focus-border": "#756957",
        "block-cursor-background": "#40372b", "block-cursor-foreground": "#eee6d8",
        "block-cursor-blurred-background": "#302b24", "block-cursor-blurred-foreground": "#c1b6a6",
        "block-hover-background": "#2c2720", "input-cursor-background": "#c1b6a6",
        "input-cursor-foreground": "#141210", "input-selection-background": "#40372b",
        "input-selection-foreground": "#eee6d8", "scrollbar": "#3b342b",
        "scrollbar-hover": "#756957", "scrollbar-active": "#ab9d87",
    },
)
SAGE_THEME = replace(
    BLUE_THEME,
    name="file-finder-sage",
    primary="#8fa497", secondary="#95a299", accent="#8fa497",
    foreground="#d0d8d2", background="#101512", surface="#171e19", panel="#202922",
    variables={**BLUE_THEME.variables,
        "text-muted": "#95a299", "border-blurred": "#303e34", "border": "#8fa497",
        "finder-focus-border": "#61786a",
        "block-cursor-background": "#2e4235", "block-cursor-foreground": "#e0ece3",
        "block-cursor-blurred-background": "#26332b", "block-cursor-blurred-foreground": "#b2c3b7",
        "block-hover-background": "#253029", "input-cursor-background": "#b2c3b7",
        "input-cursor-foreground": "#101512", "input-selection-background": "#2e4235",
        "input-selection-foreground": "#e0ece3", "scrollbar": "#303e34",
        "scrollbar-hover": "#61786a", "scrollbar-active": "#8fa497",
    },
)
THEME_OPTIONS = (
    ("黑灰霧藍", BLUE_THEME),
    ("暖灰米色", WARM_THEME),
    ("深灰鼠尾草", SAGE_THEME),
)


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
    scope: str = "all"
    options: SearchOptions = SearchOptions()


class Navigation:
    def __init__(self):
        self.view = View()
        self.history: list[View] = []
        self.forward_history: list[View] = []

    def search(self, query: str) -> None:
        previous = self.view
        local = previous.directory is not None and previous.scope != "all"
        if local and previous.kind == "browse":
            self.history.append(replace(previous))
        elif not local:
            self.history.clear()
        self.forward_history.clear()
        self.view = View("search", query=query, directory=previous.directory,
                         scope=previous.scope, options=previous.options, status="等待輸入完成…")

    def enter(self, directory: Path) -> None:
        self.history.append(replace(self.view))
        self.forward_history.clear()
        self.view = View("browse", directory=directory, scope=self.view.scope,
                         options=self.view.options, status="正在讀取資料夾…")

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


class IndexChecked(Message):
    def __init__(self, sequence: int, report: backend.IndexCheck):
        super().__init__()
        self.sequence, self.report = sequence, report


class ResultOption(Option):
    """攜帶清單版本；延遲送達的舊列事件不得操作新清單。"""
    def __init__(self, prompt, revision: int, *, disabled: bool = False):
        super().__init__(prompt, disabled=disabled)
        self.revision = revision


class LazyOptions(Sequence):
    """只為畫面或按鍵實際存取的項目建立 Option。"""
    def __init__(self, owner: FileList):
        self.owner = owner

    def __len__(self):
        return len(self.owner.items)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        cache = self.owner.option_cache
        if index not in cache:
            item = self.owner.items[index]
            cache[index] = ResultOption(self.owner.format_item(item) if item is not None else "", self.owner.revision, disabled=item is None)
            if len(cache) > 128:
                cache.popitem(last=False)
        cache.move_to_end(index)
        return cache[index]


class FileList(OptionList):
    """固定列高的虛擬清單，繪製成本取決於可見列數。"""
    def __init__(self, **kwargs):
        self.items = ()
        self.revision = 0
        self.option_cache = OrderedDict()
        self.row_height = 4
        self.format_item = lambda item: ""
        super().__init__(**kwargs)

    def set_items(self, items, formatter, row_height, selected=None):
        self.revision += 1
        self.highlighted = None
        self.items, self.format_item, self.row_height = items, formatter, row_height
        self.option_cache.clear()
        self._options = LazyOptions(self)
        self.scroll_y = 0
        self._update_lines()
        self.highlighted = selected if selected is not None and 0 <= selected < len(items) else None
        if self.highlighted is None and items:
            self.action_first()
        self.refresh()

    def accepts_option(self, event: OptionList.OptionMessage) -> bool:
        return (event.option_list is self and isinstance(event.option, ResultOption)
                and event.option.revision == self.revision
                and 0 <= event.option_index < len(self.items))

    def _update_lines(self):
        width = max(0, self.scrollable_content_region.width)
        size = Size(width, len(self.items) * self.row_height)
        if size != self.virtual_size:
            self.virtual_size = size
            self._scroll_update(size)

    def get_content_width(self, container, viewport):
        return container.width

    def get_content_height(self, container, viewport, width):
        return len(self.items) * self.row_height

    def scroll_to_highlight(self, top=False):
        if self.highlighted is not None and self.is_mounted:
            self.scroll_to_region(Region(0, self.highlighted * self.row_height, self.scrollable_content_region.width, self.row_height), force=True, animate=False, top=top, immediate=True)

    def _move_page(self, direction):
        if self.items:
            step = max(1, self.scrollable_content_region.height // self.row_height)
            self.highlighted = max(0, min(len(self.items) - 1, (self.highlighted or 0) + direction * step))

    def render_line(self, y):
        width = self.scrollable_content_region.width
        index, offset = divmod(self.scroll_offset.y + y, self.row_height)
        if not 0 <= index < len(self.items):
            return Strip.blank(width, self.get_visual_style("option-list--option").rich_style)
        option = self.options[index]
        component = "option-list--option-disabled" if option.disabled else ("option-list--option-highlighted" if index == self.highlighted else ("option-list--option-hover" if index == self._mouse_hovering_over else ""))
        style = self.get_visual_style("option-list--option", *([component] if component else [])).rich_style
        lines = str(option.prompt).splitlines()
        line = lines[offset] if offset < len(lines) else ""
        if offset == 0 and "\t" in line:
            # 名稱欄最多 36 個終端字元寬，時間緊接其後，避免寬視窗拉大欄距。
            name, modified = line.split("\t", 1)
            available = max(0, width - 2)
            right = Text(modified, style=style)
            right.truncate(min(right.cell_len, max(0, available - 5)), overflow="ellipsis")
            left = Text(name, style=style)
            left.truncate(min(36, max(0, available - right.cell_len - 2)), overflow="ellipsis", pad=True)
            text = Text(" ", style=style, no_wrap=True, end="")
            text.append_text(left)
            text.append("  ")
            text.append_text(right)
        else:
            text = Text(" " + line, style=style, no_wrap=True, overflow="ellipsis", end="")
        text.truncate(max(0, width - 1), overflow="ellipsis", pad=True)
        strip = Strip(self.app.console.render(text, self.app.console.options.update(width=max(1, width))))
        return strip.adjust_cell_length(width, style).apply_meta({"option": index})

    async def _on_click(self, event: events.Click):
        index = event.style.meta.get("option")
        if index is not None and 0 <= index < len(self.items) and self.items[index] is not None:
            self.focus()
            self.highlighted = index
            if event.chain == 2:
                self.action_select()
            event.stop()
            event.prevent_default()

    def _on_mouse_move(self, event: events.MouseMove):
        super()._on_mouse_move(event)
        index = event.style.meta.get("option")
        self.tooltip = display_text(self.items[index].name) if index is not None and 0 <= index < len(self.items) and self.items[index] is not None else None


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
            yield Static(f"將使用 Windows 關聯程式開啟此檔案。文件、腳本或捷徑都可能執行程式，請只開啟可信檔案。\n\n{display_text(self.path)}", markup=False)
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
            yield OptionList(*(Option(f"{index}. {display_text(error)}") for index, error in enumerate(self.errors, 1)), id="error-list", markup=False)
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
    #menu-bar, #search, #search-tools, #location, #results, #help { width: 1fr; max-width: 76; }
    #menu-bar { height: 3; margin: 1 1 0 1; }
    #menu { width: 1fr; }
    #theme-select { width: 24; height: 3; margin-left: 1; border: round $border-blurred; background: $surface; }
    #theme-select:focus-within { border: round $finder-focus-border; }
    #theme-select SelectCurrent { padding: 0 1; background: $surface; color: $text-muted; }
    #menu Tab { color: $text-muted; background: $background; text-style: none; }
    #menu Tab.-active { color: $foreground; background: $panel; text-style: none; }
    #menu Tab:hover { color: $foreground; background: $block-hover-background; text-style: none; }
    #menu Underline { background: $border-blurred; }
    #menu Underline > .underline--bar { color: $finder-focus-border; background: $border-blurred; }
    #search { margin: 1 1; border: round $border-blurred; background: $surface; }
    #search:focus { border: round $finder-focus-border; background-tint: transparent; }
    #search > .input--placeholder { color: $text-muted; }
    #search-tools { height: 3; margin: 0 1; }
    #search-tools Select { width: 1fr; height: 3; border: round $border-blurred; background: $surface; }
    #search-tools Select:focus-within { border: round $finder-focus-border; }
    #search-tools SelectCurrent { padding: 0 1; }
    #location { height: auto; max-height: 3; margin: 0 1; color: $text-muted; }
    #results { height: 1fr; margin: 0 1; border: round $border-blurred; background: $surface; color: $foreground; scrollbar-size-vertical: 1; scrollbar-color: $scrollbar; scrollbar-color-hover: $scrollbar-hover; scrollbar-color-active: $scrollbar-active; scrollbar-background: $surface; scrollbar-background-hover: $surface; scrollbar-background-active: $surface; }
    #results:focus { border: round $finder-focus-border; background-tint: transparent; }
    #results > .option-list--option { padding: 0 1; color: $foreground; text-style: none; }
    #results > .option-list--option-highlighted { color: $block-cursor-foreground; background: $block-cursor-background; text-style: none; }
    #results:focus > .option-list--option-highlighted { color: $block-cursor-foreground; background: $block-cursor-background; text-style: none; }
    #results > .option-list--option-hover { background: $block-hover-background; text-style: none; }
    #results > .option-list--option-disabled { color: $text-muted; text-style: none; }
    #help { height: auto; margin: 0 1 1 1; padding: 0; border: none; background: $background; }
    #help > CollapsibleTitle { color: $text-muted; padding: 0 1; }
    #help > Contents { padding: 0; }
    #status { height: auto; max-height: 5; padding: 0 1; background: $panel; color: $foreground; }
    #keys { height: auto; padding: 0 1; color: $text-muted; }
    #error-list { border: round $border-blurred; background: $background; scrollbar-size-vertical: 1; }
    #error-list:focus { border: round $primary; background-tint: transparent; }
    """
    BINDINGS = [
        Binding("ctrl+l", "focus_search", "搜尋", priority=True),
        Binding("escape", "escape", "上一層"),
        Binding("ctrl+q", "quit", "結束", priority=True),
        Binding("alt+left", "history_back", "上一頁", priority=True),
        Binding("alt+right", "forward", "下一頁", priority=True),
        Binding("ctrl+e", "errors", "錯誤詳情", priority=True),
        Binding("ctrl+f", "favorite", "最愛", priority=True),
        Binding("alt+1", "menu('folders')", "資料夾", priority=True),
        Binding("alt+2", "menu('favorites')", "我的最愛", priority=True),
        Binding("ctrl+tab", "next_menu", "切換分頁", priority=True),
        Binding("f5", "refresh", "更新", priority=True),
        Binding("ctrl+f5", "rebuild", "重建索引", priority=True),
        Binding("ctrl+o", "containing_folder", "所在資料夾", priority=True),
        Binding("backspace", "back", "返回", show=False),
    ]

    def __init__(self, config_path: Path, favorites_path: Path | None = None, opener: Callable[[str], None] | None = None):
        super().__init__()
        for _, theme in THEME_OPTIONS:
            self.register_theme(theme)
        self.theme = BLUE_THEME.name
        self.config_path = config_path.absolute()
        self.favorites_path = favorites_path.absolute() if favorites_path else self.config_path.with_name("favorites.json")
        self.configuration_error = ""
        self.config: Config | None = None
        self.favorites: Favorites | None = None
        # 分頁各自保留搜尋文字、選取位置及瀏覽歷史。
        self.active_menu = "folders"
        self.navigations = {"folders": Navigation(), "favorites": Navigation()}
        self.navigation = self.navigations[self.active_menu]
        self.menu_inputs = {"folders": "", "favorites": ""}
        self.gate = RequestGate()
        self.directory_index = backend.DirectoryIndex(self.config_path.with_name(".file-finder-index.sqlite3"))
        self.index_sequence = 0
        self.index_cancel = Event()
        self.index_checking = False
        self.debounce_timer = None
        self.favorite_busy = False
        self.last_escape_at: float | None = None
        self.opener = opener or getattr(os, "startfile", None)

    def compose(self) -> ComposeResult:
        with Horizontal(id="menu-bar"):
            yield Tabs(Tab("資料夾", id="folders"), Tab("★ 我的最愛", id="favorites"), active="folders", id="menu", disabled=True)
            yield Select([(label, theme.name) for label, theme in THEME_OPTIONS], value=BLUE_THEME.name,
                         allow_blank=False, compact=True, id="theme-select", tooltip="切換主題")
        yield Input(placeholder="搜尋名稱；空白分隔的關鍵字須全部符合…", id="search", select_on_focus=False)
        with Horizontal(id="search-tools"):
            yield Select(SCOPE_OPTIONS, value="all", allow_blank=False, compact=True,
                         id="scope-select", disabled=True, tooltip="進入資料夾後可限制搜尋範圍")
            yield Select(KIND_OPTIONS, value="all", allow_blank=False, compact=True,
                         id="kind-select", disabled=True, tooltip="先篩選類型，再保留最佳 200 筆")
            yield Select(SORT_OPTIONS, value="relevance", allow_blank=False, compact=True,
                         id="sort-select", disabled=True, tooltip="資料夾仍在前；時間以索引為準，F5 核對")
        yield Static("", id="location", markup=False)
        yield FileList(id="results", markup=False)
        # 狀態持續更新，說明區預設收合以保留清單空間。
        with Collapsible(title="狀態／快捷鍵", collapsed=True, id="help"):
            yield Static("正在讀取設定與保存的索引…", id="status", markup=False)
            yield Static("↑↓／單擊 選取 | Enter／雙擊 開啟 | Ctrl+F 最愛 | Ctrl+O 所在資料夾\n直接打字搜尋 | F5 更新 | Ctrl+F5 重建 | Esc 上一層／連按回首頁 | Ctrl+Q 結束", id="keys", markup=False)

    def on_mount(self) -> None:
        self.set_interval(PROGRESS_INTERVAL_SECONDS, self.refresh_search_progress)
        generation, cancel = self.gate.invalidate()
        self.initialize(generation, cancel)
        self.query_one(Input).focus()

    @on(Select.Changed, "#theme-select")
    def on_theme_changed(self, event: Select.Changed) -> None:
        if event.value in {theme.name for _, theme in THEME_OPTIONS}:
            self.theme = event.value
            self.refresh()
        event.stop()

    @on(Select.Changed, "#scope-select, #kind-select, #sort-select")
    def on_search_option_changed(self, event: Select.Changed) -> None:
        """條件跟著分頁及歷史保存；切換立即使上一個背景結果失效。"""
        if self.config is None:
            return
        view = self.navigation.view
        if event.select.id == "scope-select":
            if event.value not in {value for _, value in SCOPE_OPTIONS}:
                return
            if view.directory is None or (self.active_menu == "favorites" and view.kind == "home"):
                return
            view.scope = event.value
            if view.kind == "search" and view.scope != "all" and not self.navigation.history:
                self.navigation.history.append(View("browse", directory=view.directory,
                                                    scope=view.scope, options=view.options))
        else:
            field = "kind" if event.select.id == "kind-select" else "sort"
            view.options = replace(view.options, **{field: event.value})
        event.stop()
        generation, cancel = self.invalidate()
        view.report = None
        self.restore_navigation_view(generation, cancel, refresh=True)

    def sync_search_controls(self) -> None:
        """還原選單時不觸發新的查詢，也不讓首頁誤用不存在的目前資料夾。"""
        view = self.navigation.view
        with self.prevent(Select.Changed):
            scope = self.query_one("#scope-select", Select)
            scope.value = view.scope
            scope.disabled = self.config is None or view.directory is None
            for selector_id, value in (("kind-select", view.options.kind), ("sort-select", view.options.sort)):
                selector = self.query_one(f"#{selector_id}", Select)
                selector.value = value
                selector.disabled = self.config is None

    @work(thread=True, exclusive=True, group="io")
    def initialize(self, generation: int, cancel: Event) -> None:
        try:
            config = backend.load_config(self.config_path)
            favorites = Favorites(self.favorites_path)
            self.directory_index.load(tuple(root.path for root in config.roots) + tuple(favorites.paths), cancel)
            payload = (config, favorites, backend.list_home_items(config, favorites, cancel, index=self.directory_index), "")
        except ValueError as error:
            payload = (None, None, (), str(error))
        self.post_message(IOResult(generation, "initialize", payload))

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
        self.menu_inputs[self.active_menu] = event.value
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
            view = self.navigation.view
            if view.kind == "search" and view.directory is not None and view.scope != "all":
                # 清空局部查詢回到該資料夾，不跳到全域首頁。
                if self.navigation.history and self.navigation.history[-1].directory == view.directory:
                    self.navigation.back()
                    self.navigation.view.scope = view.scope
                    self.navigation.view.options = view.options
                else:
                    self.navigation.view = View("browse", directory=view.directory,
                                                scope=view.scope, options=view.options)
                self.restore_navigation_view(generation, cancel, refresh=True)
                return
            options = view.options
            self.navigation.home()
            self.navigation.view.options = options
            self.render_view()
            self.load_home(generation, cancel, self.active_menu, options)

    def start_search(self, generation: int, query: str, cancel: Event) -> None:
        self.debounce_timer = None
        if self.gate.accepts(generation):
            self.navigation.view.search_started = monotonic()
            self.navigation.view.progress = SearchProgress()
            self.refresh_search_progress()
            view = self.navigation.view
            self.search_worker(generation, query, cancel, self.active_menu,
                               view.directory, view.scope, view.options)

    def refresh_search_progress(self) -> None:
        view = self.navigation.view
        if view.kind != "search" or view.report is not None or view.search_started is None:
            return
        progress = view.progress or SearchProgress()
        elapsed = max(0.0, monotonic() - view.search_started)
        status = f"正在搜尋…耗時 {elapsed:.1f} 秒；已掃描 {progress.scanned} 個項目；目前找到至少 {progress.found} 筆（尚未完成）"
        if progress.directory is not None:
            status += f"\n掃描資料夾：{display_text(progress.directory)}"
        if progress.errors or progress.skipped_links:
            status += f"\n讀取錯誤 {progress.errors} 項；已排除 {progress.skipped_links} 個連結／reparse point"
        if progress.cached_directories or progress.read_directories:
            status += f"\n重用 {progress.cached_directories} 個資料夾快取，新讀取 {progress.read_directories} 個資料夾"
        self.set_status(status)

    @work(thread=True, exclusive=True, group="io")
    def search_worker(self, generation: int, query: str, cancel: Event, menu: str,
                      directory: Path | None = None, scope: str = "all", options: SearchOptions = SearchOptions()) -> None:
        def progress(snapshot: SearchProgress) -> None:
            self.post_message(IOResult(generation, "progress", snapshot))

        kwargs = {"options": options} if options != SearchOptions() else {}
        if directory is not None and scope != "all":
            report = backend.scan_names(self.config, query, cancel, progress, self.directory_index,
                                        directory=directory, recursive=scope == "recursive", **kwargs)
        elif menu == "favorites":
            report = backend.search_favorites(self.favorites, query, cancel, self.directory_index, **kwargs)
        else:
            report = backend.scan_names(self.config, query, cancel, progress, self.directory_index, **kwargs)
        self.post_message(IOResult(generation, "search", report))
        self.directory_index.flush()
        self.post_message(IOResult(generation, "storage", None))

    @work(thread=True, exclusive=True, group="io")
    def load_home(self, generation: int, cancel: Event, menu: str, options: SearchOptions = SearchOptions()) -> None:
        self.post_message(IOResult(generation, "home", backend.list_home_items(self.config, self.favorites, cancel, menu, self.directory_index, options=options)))

    @work(thread=True, exclusive=True, group="io")
    def load_folder(self, generation: int, directory: Path, cancel: Event, options: SearchOptions = SearchOptions()) -> None:
        self.post_message(IOResult(generation, "browse", backend.browse_folder(directory, cancel, self.directory_index, options=options)))
        self.directory_index.flush()
        self.post_message(IOResult(generation, "storage", None))

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
            self.query_one(Tabs).disabled = self.config is None
        elif message.kind == "home":
            removed = self.update_view_items(message.payload)
            view.status = self.home_status(view.items)
            if removed:
                view.status += "；原選取項目已不在清單，改選第一筆" if view.items else "；原選取項目已不在清單"
        elif message.kind in {"search", "browse"}:
            view.report = message.payload
            removed = self.update_view_items(view.report.items)
            view.status = view.report.status() if message.kind == "search" else self.browse_status(view.report)
            if message.kind == "browse" and not view.items and view.options.kind != "all" and view.report.complete:
                view.status = "目前類型篩選沒有符合項目"
            if removed:
                view.status += "；原選取項目已不在清單，改選第一筆" if view.items else "；原選取項目已不在清單"
        elif message.kind == "open":
            self.set_status(message.payload)
            return
        elif message.kind == "storage":
            self.set_status(view.status)
            return
        elif message.kind == "index-refresh":
            self.restore_navigation_view(message.generation, self.gate.cancel, refresh=True)
            return
        self.render_view()
        if message.kind == "initialize" and self.config and self.query_one(Input).value.strip():
            self.search_changed(self.query_one(Input).value)
        if message.kind == "initialize" and self.config and self.directory_index.unverified_count:
            self.index_checking = True
            self.set_status(self.navigation.view.status)
            self.check_index(self.index_sequence, self.index_cancel)

    def update_view_items(self, items) -> bool:
        """重查或更新後依完整路徑還原選取，避免排序改變造成換檔。"""
        view = self.navigation.view
        selected = view.selected
        previous = view.items[selected] if selected is not None and 0 <= selected < len(view.items) else None
        view.items = items
        if previous is None:
            view.selected = None
            return False
        key = backend.path_key(previous.path)
        view.selected = next((index for index, item in enumerate(items)
                              if item is not None and backend.path_key(item.path) == key), None)
        if view.selected is None:
            view.selected = 0 if items else None
            return True
        return False

    @work(thread=True, exclusive=True, group="index-check")
    def check_index(self, sequence: int, cancel: Event) -> None:
        report = self.directory_index.check_changes(cancel)
        self.directory_index.flush()
        self.post_message(IndexChecked(sequence, report))

    def on_index_checked(self, message: IndexChecked) -> None:
        if message.sequence != self.index_sequence or message.report.cancelled:
            return
        self.index_checking = False
        if message.report.changed:
            generation, cancel = self.invalidate()
            self.restore_navigation_view(generation, cancel, refresh=True)
        else:
            self.set_status(self.navigation.view.status)

    def home_status(self, items: tuple[Item | None, ...]) -> str:
        invalid = [item.error for item in items if item is not None and item.error]
        warnings = [self.favorites.error] if self.favorites and self.favorites.error else []
        if invalid:
            warnings.append(f"{len(invalid)} 個根目錄／最愛無法讀取；{invalid[0]}")
        if warnings:
            warnings.append("Ctrl+E 查看全部錯誤")
        ready = "尚無最愛；選取檔案或資料夾後按 Ctrl+F 收藏" if self.active_menu == "favorites" and not items else "準備搜尋"
        if self.active_menu == "favorites" and not items and self.favorites and self.favorites.paths and self.navigation.view.options.kind != "all":
            ready = "目前類型篩選沒有符合的最愛"
        return ready + ("\n" + "\n".join(warnings) if warnings else "")

    @staticmethod
    def browse_status(report: ScanReport) -> str:
        text = f"直接子項目 {report.total} 筆" if report.items else "空資料夾"
        if report.errors or report.cancelled:
            text = f"資料夾讀取不完整，已讀取 {report.total} 筆"
        if report.errors:
            text += f"；{len(report.errors)} 項錯誤；Ctrl+E 查看全部錯誤\n{report.errors[0]}"
        if report.skipped_links:
            text += f"；已排除 {report.skipped_links} 個連結／reparse point"
        if report.cached_directories:
            text += "；使用已掃描快取，F5 更新"
        elif report.read_directories:
            text += "；已建立資料夾快取"
        return text

    def set_status(self, text: str) -> None:
        self.navigation.view.status = text
        shown = "狀態：" + display_text(text, multiline=True)
        if self.index_checking:
            shown += "\n已載入保存的索引，背景核對資料夾變動中…"
        if self.directory_index.error:
            shown += "\n" + self.directory_index.error
        # 結束時背景保存可能剛好回報；畫面已卸載便略過顯示更新。
        status_widgets = self.query("#status")
        if status_widgets:
            status_widgets.first(Static).update(shown)

    def render_view(self) -> None:
        view = self.navigation.view
        self.sync_search_controls()
        favorite_keys = {backend.path_key(path) for path in tuple(self.favorites.paths)} if self.favorites else set()
        kind, menu = view.kind, self.active_menu

        def format_item(item):
            star = " ★" if backend.path_key(item.path) in favorite_keys else ""
            invalid = " [失效／無法讀取]" if item.error else ""
            icon = "📁" if item.is_dir else "📄"
            label = display_text(item.label or item.name)
            # 首頁根目錄顯示完整搜尋路徑；檔案與子項目顯示父目錄以區分同名項目。
            location = item.path if kind == "home" and menu == "folders" else item.path.parent
            prompt = f"{icon} {label}{star}{invalid}\t更新：{item.modified_text}"
            if kind != "browse":
                # 同一資料夾內的路徑只顯示在上方；跨目錄搜尋仍保留路徑以辨識同名檔。
                prompt += f"\n  {display_text(location)}"
            # 留白屬於同一筆選項，方向鍵不會選到額外的空白列。
            prompt += "\n "
            return prompt

        results = self.query_one(FileList)
        results.set_items(view.items, format_item, 2 if kind == "browse" else 3, view.selected)
        view.selected = results.highlighted
        scope = "我的最愛名稱" if self.active_menu == "favorites" else "設定的根目錄"
        if view.directory is not None and view.scope != "all":
            scope = f"{'目前資料夾' if view.scope == 'current' else '包含子資料夾'}：{display_text(view.directory)}"
        location = display_text(view.directory) if view.kind == "browse" else (f"搜尋：{display_text(view.query)}（{scope}；關鍵字全部符合）" if view.kind == "search" else "")
        location_widget = self.query_one("#location", Static)
        location_widget.update(location)
        location_widget.display = bool(location)
        self.set_status(view.status)

    @on(OptionList.OptionHighlighted, "#results")
    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.accepts_option(event):
            self.navigation.view.selected = event.option_index

    async def on_event(self, event: events.Event) -> None:
        # 即使快捷鍵由元件攔截，也能辨認「連續」兩次 Esc；其他操作會取消連按。
        if isinstance(event, (events.Key, events.MouseDown, events.Paste)):
            if not isinstance(event, events.Key) or event.key != "escape":
                self.last_escape_at = None
        await super().on_event(event)

    def on_key(self, event: events.Key) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        if any(selector.has_focus_within for selector in self.query(Select)):
            return
        if isinstance(self.focused, (Input, Tabs)) and event.key in {"down", "up"}:
            results = self.query_one(OptionList)
            results.focus()
            if event.key == "down":
                results.action_first()
            else:
                results.action_last()
            event.stop()
            event.prevent_default()
        elif event.is_printable and "+" not in event.key and not isinstance(self.focused, Input):
            # 清單或分頁取得焦點時，第一個字就送入同一個搜尋欄，不能遺失。
            search = self.query_one(Input)
            if not search.disabled:
                search.focus()
                search.insert_text_at_cursor(event.character)
                event.stop()
                event.prevent_default()

    def on_paste(self, event: events.Paste) -> None:
        if isinstance(self.screen, ModalScreen) or isinstance(self.focused, Input):
            return
        if any(selector.has_focus_within for selector in self.query(Select)):
            return
        search = self.query_one(Input)
        if not search.disabled:
            search.focus()
            search.insert_text_at_cursor(" ".join(event.text.splitlines()))
            event.stop()
            event.prevent_default()

    @on(Tabs.TabActivated, "#menu")
    def on_menu_activated(self, event: Tabs.TabActivated) -> None:
        self.action_menu(event.tab.id)

    def action_menu(self, menu: str) -> None:
        if isinstance(self.screen, ModalScreen) or self.config is None or menu == self.active_menu:
            return
        if menu not in self.navigations:
            return
        self.menu_inputs[self.active_menu] = self.query_one(Input).value
        generation, cancel = self.invalidate()
        self.active_menu = menu
        self.navigation = self.navigations[menu]
        with self.prevent(Tabs.TabActivated):
            self.query_one(Tabs).active = menu
        self.restore_navigation_view(generation, cancel)
        self.set_input(self.menu_inputs[menu])
        self.query_one(Input).focus()

    def action_next_menu(self) -> None:
        self.action_menu("favorites" if self.active_menu == "folders" else "folders")

    @on(OptionList.OptionSelected, "#results")
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if isinstance(self.screen, ModalScreen) or not event.option_list.accepts_option(event):
            return
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
            self.load_folder(generation, item.path, cancel, self.navigation.view.options)
        else:
            # 不以副檔名推測安全性；所有檔案均先確認，取消為預設。
            self.push_screen(ConfirmOpen(item.path), lambda confirmed: self.open_item(item) if confirmed else None)

    def open_item(self, item: Item) -> None:
        self.open_worker(self.gate.generation, item.path)

    @work(thread=True, group="open")
    def open_worker(self, generation: int, path: Path) -> None:
        status = backend.open_file(path, self.opener)
        self.post_message(IOResult(generation, "open", status))

    def action_containing_folder(self) -> None:
        if isinstance(self.screen, ModalScreen) or not isinstance(self.focused, FileList):
            return
        selected = self.focused.highlighted
        if selected is not None and selected < len(self.navigation.view.items):
            item = self.navigation.view.items[selected]
            if item is not None:
                self.open_parent_worker(self.gate.generation, item.path)

    @work(thread=True, group="open")
    def open_parent_worker(self, generation: int, path: Path) -> None:
        self.post_message(IOResult(generation, "open", backend.open_containing_folder(path, self.opener)))

    def action_focus_search(self) -> None:
        if not isinstance(self.screen, ModalScreen):
            self.query_one(Input).focus()

    def action_refresh(self) -> None:
        """只有使用者按 F5 才使索引失效；瀏覽時只更新當前資料夾。"""
        if isinstance(self.screen, ModalScreen) or self.config is None:
            return
        view = self.navigation.view
        generation, cancel = self.invalidate()
        self.index_cancel.set()
        self.index_sequence += 1
        self.index_checking = False
        view.report = None
        view.search_started = None
        view.progress = None
        self.set_status("正在核對變動並增量更新索引…")
        self.refresh_index(generation, cancel, view.directory if view.kind == "browse" else None, view.kind != "home")

    @work(thread=True, exclusive=True, group="io")
    def refresh_index(self, generation: int, cancel: Event, directory: Path | None, refresh_times: bool) -> None:
        self.directory_index.check_changes(cancel, directory, refresh_times=refresh_times)
        self.directory_index.flush()
        self.post_message(IOResult(generation, "index-refresh", None))

    def action_rebuild(self) -> None:
        if isinstance(self.screen, ModalScreen) or self.config is None:
            return
        generation, cancel = self.invalidate()
        self.index_cancel.set()
        self.index_sequence += 1
        self.index_checking = False
        self.directory_index.invalidate()
        self.navigation.view.report = None
        self.set_status("正在完整重建索引…")
        self.restore_navigation_view(generation, cancel, refresh=True)
        self.flush_index()

    @work(thread=True, exclusive=True, group="index-save")
    def flush_index(self) -> None:
        self.directory_index.flush()

    def action_escape(self) -> None:
        """單次退回一個瀏覽層級；0.5 秒內連按兩次才直接回分頁首頁。"""
        if isinstance(self.screen, ModalScreen):
            return
        now = monotonic()
        if self.last_escape_at is not None and now - self.last_escape_at <= DOUBLE_ESCAPE_SECONDS:
            self.last_escape_at = None
            self.action_home()
            return
        self.last_escape_at = now
        if self.config is None:
            # 等待初始化時不取消設定讀取；與原本的 Esc 行為一致。
            self.set_input("")
            return
        if self.navigation.history:
            generation, cancel = self.invalidate()
            self.navigation.back()
            self.restore_navigation_view(generation, cancel)
        elif self.navigation.view.kind == "search":
            # 搜尋清單的上一層就是目前分頁首頁，不跨到其他分頁。
            self.action_home()

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
            self.load_home(generation, cancel, self.active_menu, self.navigation.view.options)
        self.query_one(Input).focus()

    def action_back(self) -> None:
        if isinstance(self.screen, ModalScreen) or not isinstance(self.focused, OptionList):
            return
        self.action_history_back()

    def action_history_back(self) -> None:
        if isinstance(self.screen, ModalScreen) or self.config is None:
            return
        if not self.navigation.history:
            self.set_status("沒有可返回的瀏覽畫面；連按兩次 Esc 可返回分頁首頁")
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

    def restore_navigation_view(self, generation: int, cancel: Event, refresh: bool = False) -> None:
        view = self.navigation.view
        self.set_input(view.query)
        self.render_view()
        if view.kind == "browse":
            # 重新呈現同一份共用索引；只有未完整掃過或按 F5 才讀取磁碟。
            self.load_folder(generation, view.directory, cancel, view.options)
        elif view.kind == "home":
            self.load_home(generation, cancel, self.active_menu, view.options)
        elif view.report is None or refresh:
            view.report = None
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
        if self.favorite_busy:
            return
        self.favorite_busy = True
        self.save_favorite(item.path)

    @work(thread=True, group="favorites")
    def save_favorite(self, path: Path) -> None:
        try:
            added = self.favorites.toggle(path)
            status = f"已{'加入' if added else '取消'}最愛：{display_text(path)}"
        except ValueError as error:
            status = str(error)
        self.post_message(FavoriteResult(status))

    def on_favorite_result(self, message: FavoriteResult) -> None:
        self.favorite_busy = False
        if self.navigation.view.kind == "home":
            generation, cancel = self.invalidate()
            self.load_home(generation, cancel, self.active_menu, self.navigation.view.options)
        elif self.active_menu == "favorites" and self.navigation.view.kind == "search":
            # 收藏搜尋中移除項目後重新篩選，不保留已取消的收藏結果。
            generation, cancel = self.invalidate()
            self.navigation.view.report = None
            self.restore_navigation_view(generation, cancel, refresh=True)
        else:
            self.render_view()
        self.set_status(message.status)

    def action_quit(self) -> None:
        self.invalidate()
        self.index_cancel.set()
        self.gate.cancel.set()
        self.exit()

    def on_unmount(self) -> None:
        self.index_cancel.set()
        self.gate.cancel.set()


def main() -> int:
    parser = argparse.ArgumentParser(description="只搜尋名稱、不修改原始檔案的 Windows TUI。")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"), help="設定檔路徑")
    parser.add_argument("--check", action="store_true", help="只驗證設定與搜尋根目錄，不啟動 TUI 或遞迴搜尋")
    args = parser.parse_args()
    if args.check:
        try:
            config = backend.load_config(args.config)
        except ValueError as error:
            print(str(error))
            return 1
        invalid = False
        for root in config.roots:
            error = backend.validate_folder(root.path)
            print(f"{display_text(root.name)}：{error or display_text(root.path) + '（可讀取）'}")
            invalid |= bool(error)
        return int(invalid)
    FileFinderApp(args.config).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
