"""Read-only paging inside the Shell widget, without a terminal subprocess."""

import re

import anyio
from anytext import on
from anytext.app import ComposeResult
from anytext.binding import Binding
from anytext.containers import Horizontal, Vertical
from anytext.reactive import var
from anytext.strip import Strip
from anytext.widgets import Input, RichLog, Static
from rich.cells import cell_len
from rich.measure import Measurement
from rich.style import Style
from rich.text import Text

from .engine import PageRequest


class PagerLog(RichLog, can_focus=False):
    """Keep keyboard focus on the pager and expose mouse scrolling."""

    search_pattern: re.Pattern[str] | None = None

    def _render_line(self, y: int, scroll_x: int, width: int) -> Strip:
        line = super()._render_line(y, scroll_x, width)
        if self.search_pattern is None or y >= len(self.lines):
            return line
        text = self.lines[y].text
        parts = []
        end = 0
        for match in self.search_pattern.finditer(text):
            start = max(0, cell_len(text[: match.start()]) - scroll_x)
            stop = min(width, cell_len(text[: match.end()]) - scroll_x)
            if stop <= start:
                continue
            parts.append(line.crop(end, start))
            parts.append(
                line.crop(start, stop).apply_style(Style(reverse=True, bold=True))
            )
            end = stop
        if not parts:
            return line
        parts.append(line.crop(end, width))
        return Strip.join(parts)

    def write(
        self,
        content,
        width=None,
        expand=False,
        shrink=True,
        scroll_end=None,
        animate=False,
    ):
        available = self.scrollable_content_region.width
        if width is None and not expand and shrink and available > 0:
            # Measure against the widget, rather than Rich's process-console width.
            # Keep short lines unpadded so regex end anchors retain their meaning.
            console = self.app.console
            options = console.options.update_width(available)
            width = max(1, Measurement.get(console, options, content).maximum)
        return super().write(content, width, expand, shrink, scroll_end, animate)


class PagerSearchInput(Input):
    """A temporary regex search field, separate from the shell command input."""


class Pager(Vertical, can_focus=True):
    BINDINGS = [
        Binding("space,pagedown", "next_page", "Next page", priority=True),
        Binding("b,pageup", "previous_page", "Previous page", priority=True),
        Binding("enter,down", "next_line", show=False, priority=True),
        Binding("up", "previous_line", show=False, priority=True),
        Binding("home,g", "first_page", show=False, priority=True),
        Binding("end,G", "last_page", show=False, priority=True),
        Binding("q,escape", "close_pager", show=False, priority=True),
        Binding("ctrl+q", "force_close", "Close", priority=True),
        Binding("slash", "search_forward", "Search", priority=True),
        Binding("question_mark", "search_backward", show=False, priority=True),
        Binding("n", "next_match", "Next match", priority=True),
        Binding("N", "previous_match", show=False, priority=True),
        Binding("escape", "cancel_search", "Cancel search", priority=True),
    ]
    _searching = var(False, bindings=True)

    DEFAULT_CSS = """
    Pager { width: 1fr; height: 1fr; background: $surface; }
    Pager #pager-search-row { display: none; height: 1; }
    Pager #pager-search-direction { width: 1; }
    Pager PagerSearchInput, Pager PagerSearchInput:focus {
        width: 1fr; height: 1; border: none; padding: 0;
    }
    Pager PagerLog {
        height: 1fr; min-height: 0; padding: 0 1; overflow: auto auto;
        scrollbar-gutter: stable;
    }
    """

    def __init__(self, request: PageRequest) -> None:
        super().__init__()
        self.request = request
        self.closed = anyio.Event()
        self.loaded = False
        self.error: Exception | None = None
        self._pattern: re.Pattern[str] | None = None
        self._direction = 1
        self._match_line: int | None = None

    def compose(self) -> ComposeResult:
        yield PagerLog(wrap=True, markup=False, min_width=1, auto_scroll=False)
        with Horizontal(id="pager-search-row"):
            yield Static("/", id="pager-search-direction")
            yield PagerSearchInput()

    def on_mount(self) -> None:
        self.focus()

    async def load(self) -> None:
        log = self.query_one(PagerLog)
        try:
            async for line in self.request.lines:
                log.write(Text(line), scroll_end=False)
                await anyio.lowlevel.checkpoint()
        except Exception as error:
            self.error = error
            self.closed.set()
        else:
            # EOF can arrive before RichLog has its first layout. Only advertise
            # readiness after the frame that lays out and renders the content.
            self.call_after_refresh(self._mark_loaded)

    def _mark_loaded(self) -> None:
        if self.is_mounted:
            self.loaded = True

    def action_next_page(self) -> None:
        self._match_line = None
        self.query_one(PagerLog).scroll_page_down(animate=False)

    def action_previous_page(self) -> None:
        self._match_line = None
        self.query_one(PagerLog).scroll_page_up(animate=False)

    def action_next_line(self) -> None:
        self._match_line = None
        log = self.query_one(PagerLog)
        log.scroll_to(y=log.scroll_y + 1, animate=False, immediate=True)

    def action_previous_line(self) -> None:
        self._match_line = None
        log = self.query_one(PagerLog)
        log.scroll_to(y=log.scroll_y - 1, animate=False, immediate=True)

    def action_first_page(self) -> None:
        self._match_line = None
        self.query_one(PagerLog).scroll_home(animate=False)

    def action_last_page(self) -> None:
        self._match_line = None
        self.query_one(PagerLog).scroll_end(animate=False)

    def action_close_pager(self) -> None:
        self.closed.set()

    def action_force_close(self) -> None:
        self.closed.set()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "cancel_search":
            return self._searching
        if action in {
            "search_forward",
            "search_backward",
            "next_match",
            "previous_match",
        }:
            return self.request.searchable and not self._searching
        if action != "force_close":
            return not self._searching
        return True

    def _start_search(self, direction: int) -> None:
        self._direction = direction
        self._searching = True
        self.query_one("#pager-search-direction", Static).update(
            "/" if direction > 0 else "?"
        )
        self.query_one("#pager-search-row").display = True
        field = self.query_one(PagerSearchInput)
        field.value = ""
        field.focus()

    def action_search_forward(self) -> None:
        self._start_search(1)

    def action_search_backward(self) -> None:
        self._start_search(-1)

    def action_cancel_search(self) -> None:
        self._searching = False
        self.query_one("#pager-search-row").display = False
        self.focus()

    @on(Input.Changed)
    def search_changed(self, event: Input.Changed) -> None:
        event.stop()

    @on(Input.Submitted)
    def search_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.value:
            try:
                self._pattern = re.compile(event.value)
            except re.error as error:
                self.app.notify(
                    str(error), title="Invalid search", severity="error", markup=False
                )
                return
        log = self.query_one(PagerLog)
        log.search_pattern = self._pattern
        log.refresh()
        self._match_line = None
        self.action_cancel_search()
        if self._pattern is not None:
            self._find_match(self._direction, include_current=True)

    def _find_match(self, direction: int, *, include_current: bool = False) -> None:
        if self._pattern is None:
            return
        log = self.query_one(PagerLog)
        count = len(log.lines)
        if count:
            start = (
                self._match_line if self._match_line is not None else int(log.scroll_y)
            )
            first = 0 if include_current else 1
            for step in range(first, count + first):
                index = (start + direction * step) % count
                if self._pattern.search(log.lines[index].text):
                    self._match_line = index
                    log.scroll_to(y=index, animate=False, immediate=True)
                    return
        self.app.notify("Pattern not found", title="Search", markup=False)

    def action_next_match(self) -> None:
        self._find_match(self._direction)

    def action_previous_match(self) -> None:
        self._find_match(-self._direction)
