"""An Anytext frontend for the UI-independent shell session."""

from __future__ import annotations

from collections import deque
from os.path import commonprefix
from typing import Literal

import anyio
from anytext import events, on
from anytext.app import App, ComposeResult
from anytext.binding import Binding
from anytext.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from anytext.message import Message
from anytext.widgets import Footer, OptionList, RichLog, Static, TextArea
from rich.cells import cell_len
from rich.columns import Columns
from rich.table import Table
from rich.text import Text

from .editor import FileEditor
from .engine import Event, Session, is_complete
from .log import SelectableLog
from .pager import Pager
from .presentation import (
    LONG_FILE_HEADERS,
    file_name,
    format_value,
    is_file_record,
    is_long_file_record,
    long_file_cells,
)


class CompletionMenu(OptionList, can_focus=False):
    """Completion choices without taking focus from the command editor."""


class CompletionPopup(HorizontalScroll, can_focus=False):
    """Keep long completion rows intact inside a horizontally scrollable popup."""

    def compose(self) -> ComposeResult:
        yield CompletionMenu()


class CommandInput(TextArea):
    BINDINGS = [
        Binding("up", "history_previous", show=False),
        Binding("down", "history_next", show=False),
        Binding("alt+up", "older_command", show=False),
        Binding("alt+down", "newer_command", show=False),
        Binding("enter", "submit", show=False),
        Binding("shift+enter,ctrl+o", "newline", show=False),
        Binding("ctrl+enter", "force_submit", show=False),
        Binding("tab", "complete", show=False),
        Binding("escape", "dismiss_completion", show=False),
        Binding("ctrl+c", "cancel_command", show=False),
    ]

    async def on_key(self, event: events.Key) -> None:
        # Handle editor shortcuts in the same queue as typed characters. App-level
        # priority bindings could submit before earlier edits have been applied.
        for binding in self.BINDINGS:
            if event.key in binding.key.split(","):
                event.stop()
                event.prevent_default()
                await self.run_action(binding.action)
                return

    class Submitted(Message):
        def __init__(self, command_input: CommandInput, value: str) -> None:
            super().__init__()
            self.input = command_input
            self.value = value

    def __init__(self, **kwargs) -> None:
        super().__init__(show_line_numbers=False, highlight_cursor_line=False, **kwargs)

    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, value: str) -> None:
        self.text = value
        self.cursor_position = len(value)

    @property
    def cursor_position(self) -> int:
        return self.document.get_index_from_location(self.cursor_location)

    @cursor_position.setter
    def cursor_position(self, value: int) -> None:
        self.move_cursor(self.document.get_location_from_index(value))

    def resize_editor(self) -> None:
        height = max(1, min(8, self.wrapped_document.height))
        self.styles.height = height
        if self.parent is not None:
            self.parent.styles.height = height + (
                2 if self.shell.prompt_position == "bottom" else 0
            )

    def on_resize(self) -> None:
        self.call_after_refresh(self.resize_editor)

    def render_lines(self, crop):
        if self.has_focus:
            # Paint happens after layout, even when cached lines are reused.
            self.app.cursor_position = self.cursor_screen_offset
        return super().render_lines(crop)

    @property
    def shell(self) -> Shell:
        return self.query_ancestor(Shell)

    def action_history_previous(self) -> None:
        if self.shell.completion_open:
            self.shell.move_completion(-1)
        elif "\n" in self.value:
            self.action_cursor_up()
        else:
            self.shell.history_move(-1)

    def action_history_next(self) -> None:
        if self.shell.completion_open:
            self.shell.move_completion(1)
        elif "\n" in self.value:
            self.action_cursor_down()
        else:
            self.shell.history_move(1)

    def action_older_command(self) -> None:
        self.shell.history_move(-1)

    def action_newer_command(self) -> None:
        self.shell.history_move(1)

    async def action_complete(self) -> None:
        await self.shell.complete()

    def action_dismiss_completion(self) -> None:
        self.shell.dismiss_completion()

    async def action_submit(self) -> None:
        if self.shell.completion_open:
            self.shell.accept_completion()
        elif not self.shell._busy:
            if is_complete(self.value):
                self.action_force_submit()
            else:
                self.action_newline()

    def action_newline(self) -> None:
        self.replace("\n", *self.selection, maintain_selection_offset=False)

    def action_force_submit(self) -> None:
        self.post_message(self.Submitted(self, self.value))

    def action_cancel_command(self) -> None:
        self.shell.dismiss_completion()
        self.shell.cancel()


class RunningCursor(TextArea):
    """A blank, read-only cursor line while a foreground command owns input."""

    def __init__(self) -> None:
        super().__init__(
            id="running-cursor",
            read_only=True,
            show_line_numbers=False,
            highlight_cursor_line=False,
        )

    @property
    def _draw_cursor(self) -> bool:
        # TextArea normally renders read-only cursors steadily. Keep its blink
        # timer visible here while retaining read-only input protection.
        return (
            self.has_focus
            and self.show_cursor
            and (not self.cursor_blink or self._cursor_visible)
        )

    def position_cursor(self) -> None:
        if self.has_focus:
            self.app.cursor_position = self.cursor_screen_offset
            self.refresh()

    def on_focus(self) -> None:
        self.call_after_refresh(self.position_cursor)

    def on_resize(self) -> None:
        self.call_after_refresh(self.position_cursor)

    def on_key(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        if event.key == "ctrl+c":
            self.query_ancestor(Shell).cancel()


class ShellScroll(VerticalScroll, can_focus=False):
    """One scrollback area containing both the transcript and the active prompt."""

    _follow_prompt = True

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if self.is_mounted:
            shell = self.query_ancestor(Shell)
            if new_value < old_value:
                self._follow_prompt = False
                shell._cancel_prompt_scroll()
            self.call_after_refresh(shell._position_prompt_cursor)

    def on_resize(self) -> None:
        shell = self.query_ancestor(Shell)
        if self._follow_prompt and not shell._busy:
            shell._request_prompt_scroll()


class Transcript(SelectableLog, can_focus=False):
    def __init__(self, *, inline_prompt: bool, **kwargs) -> None:
        super().__init__(**kwargs)
        self.inline_prompt = inline_prompt
        self._last_output_width = 0

    def write(
        self,
        content,
        width=None,
        expand=True,
        shrink=True,
        scroll_end=None,
        animate=False,
    ):
        available = self.scrollable_content_region.width
        if available > 0:
            self._last_output_width = available
        elif width is None and self._last_output_width > 0:
            # Pager/editor teardown can emit before the restored pane is laid out.
            width = self._last_output_width
        follow = (
            self.inline_prompt
            and self.parent is not None
            and self.parent.is_vertical_scroll_end
        )
        result = super().write(
            content,
            width,
            expand,
            shrink,
            False if self.inline_prompt else scroll_end,
            animate,
        )
        if self.inline_prompt:
            self.styles.height = len(self.lines)
            if follow and self.is_mounted:
                self.query_ancestor(Shell)._request_prompt_scroll()
        return result

    def on_mouse_up(self) -> None:
        shell = self.query_ancestor(Shell)
        target = shell.query_one(RunningCursor if shell._busy else CommandInput)
        target.focus(scroll_visible=False)

    def clear(self):
        result = super().clear()
        if self.inline_prompt:
            self.styles.height = 0
        return result


class Shell(Vertical, can_focus=True):
    """Embeddable shell UI. Supply a session to retain state across remounts."""

    BINDINGS = [
        Binding("ctrl+c", "cancel", show=False),
        Binding("ctrl+shift+c,super+c", "copy_selection", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    Shell CompletionPopup {
        display: none; overlay: screen; position: absolute;
        constrain: inside inside; width: 40; height: auto; max-height: 10;
        border: round $primary; background: $surface;
        overflow-x: auto; overflow-y: hidden;
    }
    Shell CompletionMenu {
        border: none; padding: 0; height: 1fr; min-width: 100%;
        overflow-x: hidden; overflow-y: auto;
    }
    Shell CompletionMenu > .option-list--option { padding: 0 1; }
    Shell { height: 1fr; border: round $primary; }
    Shell #shell-terminal { height: 1fr; }
    Shell RichLog { height: 1fr; padding: 0 1; }
    Shell.bottom-prompt Transcript { scrollbar-gutter: stable; }
    Shell #running-cursor, Shell #running-cursor:focus {
        display: none; height: 1; min-height: 1;
        border: none; padding: 0 1; margin: 0; background: $surface;
    }
    Shell #command-line { height: 3; }
    Shell #prompt { width: auto; padding: 1 0 0 1; }
    Shell CommandInput, Shell CommandInput:focus {
        width: 1fr; height: 1; border: none; padding: 0; margin: 1 0;
    }
    Shell #shell-scroll { height: 1fr; background: $surface; scrollbar-gutter: stable; }
    Shell.inline-prompt RichLog { height: 0; min-height: 0; overflow: hidden hidden; }
    Shell PagerLog, Shell.inline-prompt PagerLog {
        height: 1fr; min-height: 0; padding: 0 1; overflow: auto auto;
    }
    Shell.inline-prompt #command-line { height: 1; }
    Shell.inline-prompt #prompt { padding: 0 0 0 1; margin-right: 1; }
    Shell.inline-prompt CommandInput, Shell.inline-prompt CommandInput:focus {
        height: 1; border: none; padding: 0; margin: 0; background: $surface;
    }
    """

    def __init__(
        self,
        session: Session | None = None,
        *,
        prompt_position: Literal["inline", "bottom"] = "inline",
        **kwargs,
    ) -> None:
        if prompt_position not in {"inline", "bottom"}:
            raise ValueError("prompt_position must be 'inline' or 'bottom'")
        super().__init__(**kwargs)
        self._prompt_position = prompt_position
        self.set_class(prompt_position == "inline", "inline-prompt")
        self.set_class(prompt_position == "bottom", "bottom-prompt")
        self._prompt_scroll_pending = False
        self._prompt_scroll_generation = 0
        self.session = session if session is not None else Session()
        self._history_index = len(self.session.history)
        self._draft = ""
        self.output: deque[Event] = deque(maxlen=5000)
        self._busy = False
        self._file_rows: list[dict] = []
        self._completions: list[str] = []
        self._completion_source: str | None = None

    def focus_on_click(self) -> bool:
        # Blank space and non-focusable output must not steal the active cursor.
        return False

    @property
    def prompt_position(self) -> Literal["inline", "bottom"]:
        return self._prompt_position

    def _compose_terminal(self) -> ComposeResult:
        yield Transcript(
            id="transcript",
            wrap=True,
            markup=False,
            max_lines=5000,
            min_width=1,
            inline_prompt=self.prompt_position == "inline",
        )
        with Horizontal(id="command-line"):
            yield Static(f"{self.session.cwd} ❯", id="prompt")
            yield CommandInput()
        yield RunningCursor()

    def compose(self) -> ComposeResult:
        with Vertical(id="shell-terminal"):
            if self.prompt_position == "inline":
                with ShellScroll(id="shell-scroll"):
                    yield from self._compose_terminal()
            else:
                yield from self._compose_terminal()
            yield CompletionPopup()

    def _cancel_prompt_scroll(self) -> None:
        self._prompt_scroll_generation += 1
        self._prompt_scroll_pending = False

    def _request_prompt_scroll(self) -> None:
        if self.prompt_position == "inline":
            self.query_one(ShellScroll)._follow_prompt = True
        if not self._prompt_scroll_pending:
            self._prompt_scroll_pending = True
            self.call_after_refresh(self._show_prompt, self._prompt_scroll_generation)

    def _show_prompt(self, generation: int) -> None:
        if generation != self._prompt_scroll_generation:
            return
        self._prompt_scroll_pending = False
        if self.is_mounted:
            if self._busy:
                if self.prompt_position == "inline":
                    self.query_one(ShellScroll).scroll_end(
                        animate=False, immediate=True
                    )
                self.query_one(RunningCursor).position_cursor()
            else:
                if self.prompt_position == "inline":
                    viewport = self.query_one(ShellScroll)
                    viewport.anchor(
                        len(self.query_one(Transcript).lines) + 1
                        > viewport.scrollable_content_region.height
                    )
                    viewport.scroll_end(
                        animate=False,
                        force=True,
                        immediate=True,
                        on_complete=self._position_prompt_cursor,
                    )
                else:
                    self.query_one(CommandInput).scroll_visible(
                        animate=False, immediate=True
                    )
                    self.call_after_refresh(self._position_prompt_cursor)

    def _position_prompt_cursor(self) -> None:
        if self.is_mounted and not self._busy:
            editor = self.query_one(CommandInput)
            if editor.has_focus:
                self.app.cursor_position = editor.cursor_screen_offset
                editor.refresh()

    @on(TextArea.Changed)
    def input_changed(self, event: TextArea.Changed) -> None:
        if isinstance(event.text_area, CommandInput):
            event.stop()
            event.text_area.resize_editor()
            if (
                self.completion_open
                and event.text_area.value != self._completion_source
            ):
                self.dismiss_completion()
            self._request_prompt_scroll()

    def on_resize(self) -> None:
        self.call_after_refresh(self.query_one(CommandInput).resize_editor)
        self.dismiss_completion()
        self._request_prompt_scroll()

    def on_mount(self) -> None:
        self._jobs_ready = anyio.Event()
        self.run_worker(self._background_jobs(), name="shell-jobs")
        self.query_one(CommandInput).focus(scroll_visible=False)
        self._request_prompt_scroll()

    async def _background_jobs(self) -> None:
        async with self.session.background():
            self._jobs_ready.set()
            await anyio.sleep_forever()

    @on(CommandInput.Submitted)
    def submitted(self, event: CommandInput.Submitted) -> None:
        event.stop()
        if self._busy:
            return
        if not event.value.strip():
            self.dismiss_completion()
            event.input.value = ""
            self._history_index = len(self.session.history)
            self._draft = ""
            self._flush_file_rows()
            self.query_one(RichLog).write(
                Text(f"{self.session.cwd} ❯", style="bold cyan")
            )
            self._request_prompt_scroll()
            return
        self._busy = True
        self.dismiss_completion()
        self.query_one("#command-line").display = False
        cursor = self.query_one(RunningCursor)
        cursor.display = True
        cursor.focus(scroll_visible=False)
        source = event.value
        event.input.value = ""
        self.query_one(RichLog).write(
            Text(f"{self.session.cwd} ❯ {source}", style="bold cyan")
        )
        self.run_worker(
            self._execute(source), name="shell-command", exit_on_error=False
        )

    async def _execute(self, source: str) -> None:
        try:
            await self._jobs_ready.wait()
            width = max(1, self.query_one(RichLog).scrollable_content_region.width)
            await self.session.execute(source, self.receive, output_width=width)
        finally:
            self._busy = False
            self._history_index = len(self.session.history)
            self._draft = ""
            self.query_one("#prompt", Static).update(f"{self.session.cwd} ❯")
            self.query_one(RunningCursor).display = False
            self.query_one("#command-line").display = True
            self.query_one(CommandInput).focus(scroll_visible=False)
            self._request_prompt_scroll()

    def _flush_file_rows(self) -> None:
        if not self._file_rows:
            return
        if is_long_file_record(self._file_rows[0]):
            table = Table(box=None, padding=(0, 2))
            for header in LONG_FILE_HEADERS:
                table.add_column(
                    header, justify="right" if header == "Bytes" else "left"
                )
            for row in self._file_rows:
                kind, size, modified, name = long_file_cells(row)
                table.add_row(
                    kind,
                    size,
                    modified,
                    Text(name, style="bold blue" if row["type"] == "directory" else ""),
                )
            self.query_one(RichLog).write(table)
        else:
            names = [
                Text(
                    file_name(row),
                    style="bold blue" if row["type"] == "directory" else "",
                )
                for row in self._file_rows
            ]
            self.query_one(RichLog).write(Columns(names, padding=(0, 2)))
        self._file_rows.clear()

    async def receive(self, event: Event) -> None:
        self.output.append(event)
        transcript = self.query_one(RichLog)
        if event.kind in {"edit", "page"}:
            self._flush_file_rows()
            view = (
                FileEditor(self.session.filesystem, event.value.path)
                if event.kind == "edit"
                else Pager(event.value)
            )
            event.value.handled = True
            terminal = self.query_one("#shell-terminal")
            self.dismiss_completion()
            terminal.display = False
            terminal.disabled = True
            try:
                await self.mount(view)
                if isinstance(view, Pager):
                    async with anyio.create_task_group() as group:
                        group.start_soon(view.load)
                        await view.closed.wait()
                        group.cancel_scope.cancel()
                    if view.error is not None:
                        raise view.error
                else:
                    await view.closed.wait()
            finally:
                with anyio.CancelScope(shield=True):
                    await view.remove()
                terminal.disabled = False
                terminal.display = True
                self.query_one(CommandInput).focus(scroll_visible=False)
                self._request_prompt_scroll()
        elif event.kind == "clear":
            self._file_rows.clear()
            transcript.clear()
            self.output.clear()
        elif event.kind == "output" and is_file_record(event.value):
            # Bound presentation buffering while keeping pipeline records untouched.
            if self._file_rows and is_long_file_record(
                self._file_rows[0]
            ) != is_long_file_record(event.value):
                self._flush_file_rows()
            self._file_rows.append(event.value)
            if len(self._file_rows) >= 128:
                self._flush_file_rows()
        else:
            self._flush_file_rows()
            if event.kind == "job":
                job = event.value
                state = (
                    "running" if job.result is None else f"done ({job.result.status})"
                )
                transcript.write(Text(f"[{job.id}] {state}  {job.source}"))
            if event.kind in {"output", "error"}:
                transcript.write(
                    Text(
                        format_value(event.value),
                        style="red" if event.kind == "error" else "",
                    )
                )

    def action_cancel(self) -> None:
        self.cancel()

    def action_copy_selection(self) -> None:
        self.screen.action_copy_text()

    def cancel(self) -> None:
        if not self.session.cancel():
            if self.screen.get_selected_text():
                self.screen.action_copy_text()
            else:
                self.query_one(CommandInput).value = ""

    def history_move(self, delta: int) -> None:
        command_input = self.query_one(CommandInput)
        if self._history_index == len(self.session.history):
            self._draft = command_input.value
        self._history_index = max(
            0, min(len(self.session.history), self._history_index + delta)
        )
        command_input.value = (
            self._draft
            if self._history_index == len(self.session.history)
            else self.session.history[self._history_index]
        )
        command_input.cursor_position = len(command_input.value)

    async def complete(self) -> None:
        command_input = self.query_one(CommandInput)
        if self.completion_open:
            self.move_completion(1)
            return
        if command_input.cursor_position != len(command_input.value):
            return
        candidates = await self.session.complete(command_input.value)
        if len(candidates) == 1:
            command_input.value = candidates[0]
            command_input.cursor_position = len(command_input.value)
        elif candidates:
            shared = commonprefix(candidates)
            if shared.startswith(command_input.value) and len(shared) > len(
                command_input.value
            ):
                command_input.value = shared
                command_input.cursor_position = len(shared)
                return
            self._completions = candidates
            self._completion_source = command_input.value
            menu = self.query_one(CompletionMenu)
            menu.clear_options()
            menu.add_options(Text(candidate) for candidate in candidates)
            popup = self.query_one(CompletionPopup)
            cursor = command_input.cursor_screen_offset
            content_width = max(cell_len(candidate) for candidate in candidates) + 4
            width = min(self.screen.size.width, max(16, content_width + 2))
            popup.styles.width = width
            menu.styles.width = max(width - 2, content_width)
            height = min(10, len(candidates) + 2 + (content_width > width - 2))
            popup.styles.height = height
            y = cursor.y + 1
            if y + height > self.content_region.bottom:
                y = max(0, cursor.y - height)
            popup.styles.offset = (
                cursor.x - self.content_region.x,
                y - self.content_region.y,
            )
            popup.display = True
            popup.scroll_to(x=0, animate=False, force=True)
            menu.highlighted = 0

    @property
    def completion_open(self) -> bool:
        return bool(self._completions)

    def dismiss_completion(self) -> None:
        self._completions = []
        self._completion_source = None
        self.query_one(CompletionPopup).display = False

    def move_completion(self, delta: int) -> None:
        menu = self.query_one(CompletionMenu)
        menu.highlighted = ((menu.highlighted or 0) + delta) % len(self._completions)

    def accept_completion(self) -> None:
        menu = self.query_one(CompletionMenu)
        replacement = self._completions[menu.highlighted or 0]
        self.dismiss_completion()
        command_input = self.query_one(CommandInput)
        command_input.value = replacement
        command_input.cursor_position = len(replacement)

    @on(OptionList.OptionSelected)
    def completion_selected(self, event: OptionList.OptionSelected) -> None:
        if isinstance(event.option_list, CompletionMenu):
            event.stop()
            self.accept_completion()
            self.query_one(CommandInput).focus(scroll_visible=False)


class ShellApp(App, inherit_bindings=False):
    TITLE = "Anyshell"
    BINDINGS = [Binding("ctrl+q", "quit", "Quit")]

    def __init__(
        self,
        session: Session | None = None,
        *,
        prompt_position: Literal["inline", "bottom"] = "inline",
        **kwargs,
    ) -> None:
        if prompt_position not in {"inline", "bottom"}:
            raise ValueError("prompt_position must be 'inline' or 'bottom'")
        super().__init__(**kwargs)
        self.session = session if session is not None else Session()
        self.prompt_position = prompt_position

    def compose(self) -> ComposeResult:
        yield Shell(self.session, prompt_position=self.prompt_position)
        yield Footer()
