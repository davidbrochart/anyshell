"""Interactive file editor for the optional Anytext frontend."""

import anyio
from anytext import on
from anytext.app import ComposeResult
from anytext.binding import Binding
from anytext.containers import Vertical
from anytext.reactive import var
from anytext.widgets import Static, TextArea
from rich.text import Text

from .filesystem import FileSystem


class FileEditor(Vertical):
    """Edit one UTF-8 file without starting a process or leaving the app."""

    BINDINGS = [
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("ctrl+q", "close_editor", "Close", priority=True),
        Binding("ctrl+q", "confirm_discard", "Discard and close", priority=True),
        Binding("escape", "keep_editing", "Keep editing", priority=True),
        Binding("ctrl+d", "discard", show=False, priority=True),
    ]
    _confirming_close = var(False, bindings=True)

    DEFAULT_CSS = """
    FileEditor { width: 1fr; height: 1fr; background: $surface; }
    FileEditor #editor-window {
        width: 1fr; height: 1fr; background: $surface;
    }
    FileEditor #editor-title { height: 1; padding: 0 1; }
    FileEditor TextArea { height: 1fr; }
    """

    def __init__(self, filesystem: FileSystem, path: str) -> None:
        super().__init__()
        self.closed = anyio.Event()
        self._confirming_close = False
        self.filesystem = filesystem
        self.path = path
        if filesystem.is_dir(path):
            raise IsADirectoryError(f"{path!r} is a directory")
        try:
            self._original = filesystem.read_text(path)
        except FileNotFoundError:
            self._original = ""
        self._saved_text = self._original
        self._observed_text = self._original
        self._newline = "\r\n" if "\r\n" in self._original else "\n"

    def compose(self) -> ComposeResult:
        with Vertical(id="editor-window"):
            yield Static(Text(self.path), id="editor-title")
            yield TextArea(self._original, show_line_numbers=True, id="file-text")

    def on_mount(self) -> None:
        area = self.query_one(TextArea)
        # Use the widget's normalized representation when checking for changes.
        self._saved_text = area.text
        self._observed_text = area.text
        area.focus()

    @property
    def dirty(self) -> bool:
        return self.query_one(TextArea).text != self._saved_text

    @on(TextArea.Changed)
    def text_changed(self) -> None:
        self.query_one("#editor-title", Static).update(
            Text(self.path + (" *" if self.dirty else ""))
        )
        text = self.query_one(TextArea).text
        if text != self._observed_text:
            self._observed_text = text
            self._confirming_close = False

    def action_save(self) -> None:
        text = self.query_one(TextArea).text
        self._observed_text = text
        # Preserve the newline convention of an existing CRLF file.
        content = text.replace("\r\n", "\n").replace("\n", self._newline)
        try:
            self.filesystem.write_text(self.path, content)
        except (OSError, UnicodeError) as error:
            self.app.notify(
                str(error), title="Could not save", severity="error", markup=False
            )
            return
        self._saved_text = text
        self.query_one("#editor-title", Static).update(Text(self.path))
        self._confirming_close = False
        self.query_one(TextArea).focus()

    def action_close_editor(self) -> None:
        text = self.query_one(TextArea).text
        if self._confirming_close and text == self._observed_text:
            self.closed.set()
            return
        self._observed_text = text
        if self.dirty:
            self._confirming_close = True
        else:
            self.closed.set()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "close_editor":
            return not self._confirming_close
        if action in {"discard", "confirm_discard", "keep_editing"}:
            return self._confirming_close
        return True

    def action_confirm_discard(self) -> None:
        self.action_close_editor()

    def action_discard(self) -> None:
        if self._confirming_close:
            self.closed.set()

    def action_keep_editing(self) -> None:
        self._confirming_close = False
        self.query_one(TextArea).focus()
