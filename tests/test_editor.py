"""Interactive editing uses the same filesystem as commands, on either backend."""

import anyio
import pytest
from anytext.widgets import TextArea

from anyshell import LocalFileSystem, MemoryFileSystem, Session
from anyshell.editor import FileEditor
from anyshell.frontend import CommandInput, Shell, ShellApp


async def wait_until(predicate):
    with anyio.fail_after(5):
        while not predicate():
            await anyio.sleep(0.01)


@pytest.fixture(params=["memory", "local"])
def filesystem(request, tmp_path):
    return (
        MemoryFileSystem() if request.param == "memory" else LocalFileSystem(tmp_path)
    )


async def start_editor(app, pilot, command):
    editor = app.query_one(CommandInput)
    editor.value = command
    await pilot.press("enter")
    await wait_until(lambda: bool(app.query(FileEditor)))
    await pilot.pause()
    return app.query_one(FileEditor)


async def test_edit_save_existing_and_new_file(filesystem):
    filesystem.write_text("/hello.txt", "hello\nworld\n")
    session = Session(filesystem)
    app = ShellApp(session)
    async with app.run_test() as pilot:
        screen = await start_editor(app, pilot, "edit hello.txt")
        assert screen.query_one(TextArea).text == "hello\nworld\n"
        assert session.running
        from anytext.widgets import Button

        assert not screen.query(Button)
        assert not screen.query("#editor-status")
        assert screen.query_one(TextArea).region.bottom == screen.content_region.bottom
        screen.query_one(TextArea).text = "edited\n"
        await pilot.press("ctrl+s")
        assert filesystem.read_text("/hello.txt") == "edited\n"
        assert not screen.dirty
        await pilot.press("ctrl+q")
        await wait_until(lambda: not app.query_one(Shell)._busy)
        assert session.last_status == 0
        assert app.is_running
        await wait_until(lambda: app.query_one(CommandInput).has_focus)
        assert app.query_one(CommandInput).has_focus
        screen = await start_editor(app, pilot, "edit new.txt")
        assert screen.query_one(TextArea).text == ""
        await pilot.press("ctrl+s", "ctrl+q")
        await wait_until(lambda: not app.query_one(Shell)._busy)
        assert filesystem.read_text("/new.txt") == ""


async def test_edit_discard_and_keep_editing(filesystem):
    filesystem.write_text("/hello.txt", "original")
    app = ShellApp(Session(filesystem))
    async with app.run_test() as pilot:
        screen = await start_editor(app, pilot, "edit hello.txt")
        screen.query_one(TextArea).text = "unsaved"
        await pilot.press("ctrl+q")
        assert app.query_one(FileEditor) is screen
        assert screen.query_one("#editor-title").render().plain.endswith(" *")
        bindings = app.screen.active_bindings
        assert bindings["ctrl+q"].binding.description == "Discard and close"
        assert bindings["escape"].binding.description == "Keep editing"
        await pilot.press("escape")
        assert app.screen.active_bindings["ctrl+q"].binding.description == "Close"
        assert "escape" not in app.screen.active_bindings
        assert screen.query_one(TextArea).has_focus
        await pilot.press("ctrl+q")
        assert app.query_one(FileEditor) is screen
        screen.query_one(TextArea).text = "more changes"
        await pilot.pause()
        await pilot.press("ctrl+q")
        assert app.query_one(FileEditor) is screen
        await pilot.press("ctrl+q")
        await wait_until(lambda: not app.query_one(Shell)._busy)
        assert app.is_running
        assert filesystem.read_text("/hello.txt") == "original"
        await start_editor(app, pilot, "edit absent.txt")
        await pilot.press("ctrl+q")
        await wait_until(lambda: not app.query_one(Shell)._busy)
        with pytest.raises(FileNotFoundError):
            filesystem.stat("/absent.txt")


async def test_edit_preserves_crlf_and_reports_save_failure(filesystem, monkeypatch):
    filesystem.mkdir("/docs")
    filesystem.write_text("/docs/test.txt", "one\r\ntwo\r\n")
    app = ShellApp(Session(filesystem))
    async with app.run_test() as pilot:
        screen = await start_editor(app, pilot, "edit docs/test.txt")
        screen.query_one(TextArea).text = "changed\n"
        await pilot.press("ctrl+s")
        assert filesystem.read_text("/docs/test.txt") == "changed\r\n"
        await pilot.press("ctrl+q")
        await wait_until(lambda: not app.query_one(Shell)._busy)
        screen = await start_editor(app, pilot, "edit missing/new.txt")
        notifications = []
        monkeypatch.setattr(
            app,
            "notify",
            lambda message, **kwargs: notifications.append((message, kwargs)),
        )
        screen.query_one(TextArea).text = "keep me"
        await pilot.press("ctrl+s")
        assert app.query_one(FileEditor) is screen and screen.dirty
        assert len(notifications) == 1
        assert notifications[0][0]
        assert notifications[0][1] == {
            "title": "Could not save",
            "severity": "error",
            "markup": False,
        }
        assert screen.query_one(TextArea).text == "keep me"
        await pilot.press("ctrl+q")
        await pilot.press("ctrl+d")
        await wait_until(lambda: not app.query_one(Shell)._busy)
        app.query_one(CommandInput).value = "edit docs"
        await pilot.press("enter")
        await wait_until(lambda: not app.query_one(Shell)._busy)
        assert app.query_one(Shell).session.last_status == 1
        assert any(
            e.kind == "error" and "is a directory" in e.value
            for e in app.query_one(Shell).output
        )


async def test_edit_requires_frontend_and_cannot_be_piped(filesystem):
    session = Session(filesystem)
    events = []

    async def emit(event):
        events.append(event)

    result = await session.execute("edit file.txt", emit)
    assert result.status == 1
    assert any(e.kind == "error" and "interactive frontend" in e.value for e in events)
    events.clear()
    assert (await session.execute("edit file.txt | count", emit)).status == 1
    assert not any(e.kind == "edit" for e in events)


async def test_editor_closes_on_session_cancellation():
    session = Session()
    app = ShellApp(session)
    async with app.run_test() as pilot:
        await start_editor(app, pilot, "edit new.txt")
        assert session.cancel()
        await wait_until(lambda: not app.query(FileEditor))
        await wait_until(lambda: not app.query_one(Shell)._busy)
        assert session.last_status == 130


async def test_app_can_shutdown_with_editor_open():
    app = ShellApp()
    with anyio.fail_after(5):
        async with app.run_test() as pilot:
            await start_editor(app, pilot, "edit new.txt")


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_editor_fills_only_embedded_shell_and_restores_transcript(
    prompt_position,
):
    from anytext.app import App, ComposeResult
    from anytext.binding import Binding
    from anytext.widgets import RichLog, Static

    class EmbeddedApp(App):
        # Let focused widgets handle Ctrl+Q before the application's quit action.
        BINDINGS = [Binding("ctrl+q", "quit", "Quit", priority=False)]
        CSS = "#outside { height: 2; } Shell { width: 70; height: 20; }"

        def compose(self) -> ComposeResult:
            yield Static("Other application content", id="outside")
            yield Shell(
                Session(MemoryFileSystem({"test.txt": "hello"})),
                prompt_position=prompt_position,
            )

    app = EmbeddedApp()
    async with app.run_test(size=(100, 30)) as pilot:
        shell = app.query_one(Shell)
        transcript = shell.query_one(RichLog)
        app.query_one(CommandInput).value = "echo previous output"
        await pilot.press("enter")
        await wait_until(lambda: not shell._busy)
        screen = app.screen
        await start_editor(app, pilot, "edit test.txt")
        editor = shell.query_one(FileEditor)
        assert app.screen is screen
        assert editor.region == shell.content_region
        assert app.query_one("#outside").visible
        assert not shell.query_one("#shell-terminal").display
        assert transcript is shell.query_one(RichLog)
        assert any("previous output" in line.text for line in transcript.lines)
        await pilot.press("ctrl+q")
        await wait_until(lambda: not shell._busy)
        assert app.is_running
        assert not shell.query(FileEditor)
        assert shell.query_one("#shell-terminal").display
        assert transcript is shell.query_one(RichLog)
        assert any("previous output" in line.text for line in transcript.lines)
        await wait_until(lambda: shell.query_one(CommandInput).has_focus)
        assert shell.query_one(CommandInput).has_focus


async def test_edit_title_tracks_changes_after_save(filesystem):
    filesystem.write_text("/test.txt", "original")
    app = ShellApp(Session(filesystem))
    async with app.run_test() as pilot:
        editor = await start_editor(app, pilot, "edit test.txt")
        area = editor.query_one(TextArea)
        title = editor.query_one("#editor-title")
        area.text = "saved text"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert title.render().plain == "/test.txt"
        area.text = "changed again"
        await pilot.pause()
        assert editor.dirty
        assert title.render().plain == "/test.txt *"
        assert filesystem.read_text("/test.txt") == "saved text"
        area.text = "saved text"
        await pilot.pause()
        assert not editor.dirty
        assert title.render().plain == "/test.txt"
        area.text = "final text"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert title.render().plain == "/test.txt"
        assert filesystem.read_text("/test.txt") == "final text"
        await pilot.press("ctrl+q")
        await wait_until(lambda: not app.query_one(Shell)._busy)


async def test_editor_orders_typing_save_and_close_without_quitting_app():
    from anytext import events

    fs = MemoryFileSystem({"test.txt": ""})
    app = ShellApp(Session(fs))
    async with app.run_test() as pilot:
        editor = await start_editor(app, pilot, "edit test.txt")
        area = editor.query_one(TextArea)
        for char in "saved":
            area.post_message(events.Key(char, char))
        area.post_message(events.Key("ctrl+s", None))
        await wait_until(lambda: fs.read_text("/test.txt") == "saved")
        assert not editor.dirty
        for char in " discarded":
            area.post_message(events.Key("space" if char == " " else char, char))
        area.post_message(events.Key("ctrl+q", None))
        area.post_message(events.Key("ctrl+q", None))
        await wait_until(lambda: not app.query_one(Shell)._busy)
        assert app.is_running
        assert not app.query(FileEditor)
        await wait_until(lambda: app.query_one(CommandInput).has_focus)
        assert app.query_one(CommandInput).has_focus
        assert fs.read_text("/test.txt") == "saved"
