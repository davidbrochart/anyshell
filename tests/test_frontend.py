import anyio
import pytest

from anyshell import MemoryFileSystem, Session
from anyshell.frontend import CommandInput, Shell, ShellApp


async def wait_until(predicate):
    with anyio.fail_after(5):
        while not predicate():
            await anyio.sleep(0.01)


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_submit_completion_history_and_cancellation(prompt_position):
    session = Session(MemoryFileSystem({"hello.txt": "hello\nworld\n"}))
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test() as pilot:
        shell = app.query_one(Shell)
        command_input = app.query_one(CommandInput)
        command_input.value = "ec"
        command_input.cursor_position = 2
        await pilot.press("tab")
        assert command_input.value == "echo"
        command_input.value = "cat hello.txt | count"
        await pilot.press("enter")
        await wait_until(lambda: any(e.kind == "status" for e in shell.output))
        assert [e.value for e in shell.output if e.kind == "output"] == [2]
        await pilot.press("up")
        assert command_input.value == "cat hello.txt | count"
        await pilot.press("down")
        assert command_input.value == ""
        command_input.value = "sleep 30"
        await pilot.press("enter")
        await wait_until(lambda: session.running)
        await pilot.press("ctrl+c")
        await wait_until(lambda: not session.running)
        assert session.last_status == 130
        assert app.is_running


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_listing_displays_columns_and_preserves_records(prompt_position):
    session = Session(
        MemoryFileSystem(
            {"readme.txt": "hello", "notes/example.txt": "data", "[red].txt": "literal"}
        )
    )
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test(size=(100, 24)) as pilot:
        command_input = app.query_one(CommandInput)
        command_input.value = "ls"
        await pilot.press("enter")
        shell = app.query_one(Shell)
        await wait_until(lambda: not shell._busy)
        from anytext.widgets import RichLog

        lines = [line.text for line in app.query_one(RichLog).lines]
        assert any("notes/" in line and "readme.txt" in line for line in lines)
        assert any("[red].txt" in line for line in lines)
        assert not any('"type":' in line for line in lines)
        rows = [event.value for event in shell.output if event.kind == "output"]
        assert {"name": "notes", "type": "directory"} in rows
        assert {"name": "readme.txt", "type": "file"} in rows


async def test_inline_prompt_geometry_scrolling_and_clear():
    from anytext.widgets import RichLog, Static

    from anyshell.frontend import ShellScroll

    session = Session(
        MemoryFileSystem({"many.txt": "\n".join(f"line {i}" for i in range(80))})
    )
    app = ShellApp(session)
    async with app.run_test(size=(80, 20)) as pilot:
        shell = app.query_one(Shell)
        transcript = app.query_one(RichLog)
        command_input = app.query_one(CommandInput)
        viewport = app.query_one(ShellScroll)
        await pilot.pause()
        assert shell.prompt_position == "inline"
        assert command_input.region.y == viewport.content_region.y
        assert command_input.region.y >= transcript.region.bottom
        command_input.value = "cat many.txt"
        await pilot.press("enter")
        await wait_until(lambda: not shell._busy)
        await pilot.pause()
        assert viewport.max_scroll_y > 0
        assert command_input.region.y >= viewport.content_region.y
        assert command_input.region.bottom <= viewport.content_region.bottom
        viewport.scroll_home(animate=False, immediate=True)
        await pilot.pause()
        assert command_input.region.y > viewport.content_region.bottom
        assert command_input.has_focus
        await pilot.press("x")
        await pilot.pause()
        assert command_input.value == "x"
        assert command_input.region.bottom <= viewport.content_region.bottom
        command_input.value = "cd /"
        await pilot.press("enter")
        await wait_until(lambda: not shell._busy)
        assert app.query_one("#prompt", Static).render().plain == "/ ❯"
        await pilot.resize_terminal(50, 14)
        await pilot.pause()
        assert command_input.region.bottom <= viewport.content_region.bottom
        command_input.value = "clear"
        await pilot.press("enter")
        await wait_until(lambda: not shell._busy)
        await pilot.pause()
        assert not transcript.lines
        await wait_until(lambda: command_input.region.y == viewport.content_region.y)
        assert command_input.region.y == viewport.content_region.y


def test_prompt_position_validation():
    with pytest.raises(ValueError, match="prompt_position"):
        Shell(prompt_position="other")
    with pytest.raises(ValueError, match="prompt_position"):
        ShellApp(prompt_position="other")


async def test_inline_scrollback_stays_put_until_command_finishes():
    from anyshell import Command
    from anyshell.frontend import ShellScroll

    session = Session()
    started = anyio.Event()
    more_output = anyio.Event()
    finish = anyio.Event()

    async def stream(*, ctx, stdin, stdout):
        for index in range(50):
            await stdout.send(f"row {index}")
        started.set()
        await more_output.wait()
        await stdout.send("later output")
        await finish.wait()

    session.register(Command("stream", "Controlled streaming output", stream))
    app = ShellApp(session)
    async with app.run_test(size=(80, 20)) as pilot:
        shell = app.query_one(Shell)
        command_input = app.query_one(CommandInput)
        viewport = app.query_one(ShellScroll)
        command_input.value = "stream"
        await pilot.press("enter")
        with anyio.fail_after(5):
            await started.wait()
        await pilot.pause()
        viewport.scroll_home(animate=False, immediate=True)
        await pilot.pause()
        assert viewport.scroll_y == 0
        more_output.set()
        await wait_until(lambda: any(e.value == "later output" for e in shell.output))
        await pilot.pause()
        assert viewport.scroll_y == 0
        assert not shell.query_one("#command-line").display
        finish.set()
        await wait_until(lambda: not shell._busy)
        await wait_until(
            lambda: command_input.region.bottom <= viewport.content_region.bottom
        )
        assert command_input.region.bottom <= viewport.content_region.bottom


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_long_listing_table_and_option_completion(prompt_position):
    from anytext.widgets import RichLog

    session = Session(
        MemoryFileSystem({"readme.txt": "hello", "notes/example.txt": "data"})
    )
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test(size=(100, 24)) as pilot:
        command_input = app.query_one(CommandInput)
        command_input.value = "ls --lo"
        command_input.cursor_position = len(command_input.value)
        await pilot.press("tab")
        assert command_input.value == "ls --long"
        await pilot.press("enter")
        shell = app.query_one(Shell)
        await wait_until(lambda: not shell._busy)
        text = "\n".join(line.text for line in app.query_one(RichLog).lines)
        assert "Bytes" in text and "Modified (UTC)" in text
        assert "notes/" in text and "readme.txt" in text
        rows = [event.value for event in shell.output if event.kind == "output"]
        file = next(row for row in rows if row["name"] == "readme.txt")
        assert file["size"] == 5
        assert isinstance(file["modified"], float)
        command_input.value = "ls --help"
        await pilot.press("enter")
        await wait_until(lambda: not shell._busy)
        assert any(
            e.kind == "output" and isinstance(e.value, str) and "--long" in e.value
            for e in shell.output
        )
        assert app.is_running


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_completion_menu_navigation_and_dismissal(prompt_position):
    from anytext.widgets import RichLog

    from anyshell.frontend import CompletionMenu, CompletionPopup

    session = Session(MemoryFileSystem({"apple.txt": "a", "apricot.txt": "b"}))
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test(size=(80, 24)) as pilot:
        shell = app.query_one(Shell)
        editor = app.query_one(CommandInput)
        menu = app.query_one(CompletionMenu)
        popup = app.query_one(CompletionPopup)
        editor.value = "cat ap"
        editor.cursor_position = len(editor.value)
        await pilot.pause()
        before = len(app.query_one(RichLog).lines)
        await pilot.press("tab")
        await pilot.pause()
        assert shell.completion_open and popup.display
        assert editor.has_focus
        assert menu.option_count == 2
        assert popup.region.x == editor.cursor_screen_offset.x
        assert (
            popup.region.y == editor.cursor_screen_offset.y + 1
            if prompt_position == "inline"
            else popup.region.bottom <= editor.cursor_screen_offset.y
        ), (
            menu.region,
            editor.cursor_screen_offset,
            shell.content_region,
            menu.styles.offset,
        )
        assert popup.region in app.screen.region
        await pilot.press("down", "enter")
        assert editor.value == "cat apricot.txt"
        assert not session.history
        assert not shell.completion_open
        assert len(app.query_one(RichLog).lines) == before
        editor.value = "cat ap"
        editor.cursor_position = len(editor.value)
        await pilot.press("tab", "escape")
        assert not shell.completion_open
        assert editor.value == "cat ap"
        await pilot.press("tab", "x")
        assert not shell.completion_open
        assert editor.value == "cat apx"


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_long_completions_have_horizontal_scrollbar(prompt_position):
    from anyshell.frontend import CompletionMenu, CompletionPopup

    names = ["long-" + "界" * 45 + ending for ending in ("-first.txt", "-second.txt")]
    app = ShellApp(
        Session(MemoryFileSystem(dict.fromkeys(names, ""))),
        prompt_position=prompt_position,
    )
    async with app.run_test(size=(60, 20)) as pilot:
        editor = app.query_one(CommandInput)
        editor.value = "cat long-"
        editor.cursor_position = len(editor.value)
        await pilot.press("tab")
        await pilot.pause()
        popup = app.query_one(CompletionPopup)
        menu = app.query_one(CompletionMenu)
        assert popup.max_scroll_x > 0
        assert popup.show_horizontal_scrollbar
        assert menu.virtual_size.height == 2  # Rows stay on one line.
        assert popup.region in app.screen.region
        popup.scroll_to(x=popup.max_scroll_x, animate=False, immediate=True)
        await pilot.pause()
        assert popup.scroll_x == popup.max_scroll_x
        assert editor.has_focus
        await pilot.press("down", "enter")
        assert names[1] in editor.value
        assert not app.query_one(Shell).completion_open


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_completion_expands_shared_prefix_before_opening_menu(prompt_position):
    app = ShellApp(
        Session(MemoryFileSystem({"abc0": "", "abc": ""})),
        prompt_position=prompt_position,
    )
    async with app.run_test() as pilot:
        shell = app.query_one(Shell)
        editor = app.query_one(CommandInput)
        editor.value = "ls a"
        editor.cursor_position = len(editor.value)
        await pilot.press("tab")
        assert editor.value == "ls abc"
        assert editor.cursor_position == len(editor.value)
        assert not shell.completion_open
        await pilot.press("tab")
        assert shell.completion_open
        assert editor.value == "ls abc"
        await pilot.press("down", "enter")
        assert editor.value == "ls abc0"
        assert not shell.session.history


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_help_fits_transcript_width_after_resize(prompt_position):
    from anytext.widgets import RichLog

    app = ShellApp(prompt_position=prompt_position)
    async with app.run_test(size=(60, 24)) as pilot:
        shell = app.query_one(Shell)
        editor = app.query_one(CommandInput)
        for width, source in ((60, "ls -h"), (140, "help ls"), (70, "ls --help")):
            await pilot.resize_terminal(width, 24)
            await pilot.pause()
            editor.value = "clear"
            await pilot.press("enter")
            await wait_until(lambda: not shell._busy)
            await pilot.pause()
            transcript = app.query_one(RichLog)
            available = transcript.scrollable_content_region.width
            editor.value = source
            await pilot.press("enter")
            await wait_until(lambda: not shell._busy)
            lines = [e.value for e in shell.output if e.kind == "output"]
            assert max(map(len, lines)) == available
            assert all(len(line) <= available for line in lines)
            assert transcript.scrollable_content_region.width == available
            await wait_until(
                lambda: any("--long" in line.text for line in transcript.lines)
            )
            expected = [line.rstrip() for line in lines if line.strip()]
            rendered = [
                line.text.rstrip() for line in transcript.lines if line.text.strip()
            ]
            assert rendered[1:] == expected
            assert "--long" in "\n".join(rendered)


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_foreground_hides_prompt_until_completion_or_cancellation(
    prompt_position,
):
    from anyshell import Command

    started = anyio.Event()
    release = anyio.Event()

    async def hold():
        started.set()
        await release.wait()

    session = Session()
    session.register(Command("hold", "Wait for release", hold))
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test() as pilot:
        shell = app.query_one(Shell)
        editor = app.query_one(CommandInput)
        line = shell.query_one("#command-line")
        editor.value = "hold"
        await pilot.press("enter")
        await wait_until(started.is_set)
        assert shell._busy and not line.display
        assert not editor.has_focus
        from anyshell.frontend import RunningCursor

        cursor = shell.query_one(RunningCursor)
        await wait_until(lambda: cursor.display and cursor.has_focus)
        await wait_until(lambda: app.cursor_position == cursor.cursor_screen_offset)
        await wait_until(lambda: not cursor._draw_cursor)
        await wait_until(lambda: cursor._draw_cursor)
        if prompt_position == "inline":
            from anytext.widgets import RichLog

            assert cursor.region.y == shell.query_one(RichLog).region.bottom
        await pilot.press("x", "enter")
        assert editor.value == ""
        release.set()
        await wait_until(lambda: not shell._busy and line.display and editor.has_focus)
        assert line.display and editor.has_focus
        assert not cursor.display
        editor.value = "sleep 30"
        await pilot.press("enter")
        await wait_until(lambda: session.running)
        assert not line.display
        await pilot.press("ctrl+c")
        await wait_until(lambda: not shell._busy and line.display and editor.has_focus)
        assert session.last_status == 130
        assert line.display and editor.has_focus
        editor.value = "sleep 30 &"
        await pilot.press("enter")
        await wait_until(
            lambda: bool(session.jobs) and not shell._busy and editor.has_focus
        )
        assert session.jobs[1].result is None
        assert line.display and editor.has_focus


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_enter_on_empty_prompt_advances_line(prompt_position):
    from anytext.widgets import RichLog

    session = Session()
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test() as pilot:
        shell = app.query_one(Shell)
        editor = app.query_one(CommandInput)
        transcript = app.query_one(RichLog)
        await pilot.pause()
        original_y = editor.region.y
        for text in ("", "   ", ""):
            editor.value = text
            await pilot.press("enter")
        await pilot.pause()
        assert [line.text.strip() for line in transcript.lines] == ["/ ❯"] * 3
        assert editor.value == "" and editor.has_focus
        assert not shell._busy and not session.running
        assert session.history == []
        if prompt_position == "inline":
            assert editor.region.y == original_y + 3


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
@pytest.mark.parametrize("file_count", [100, 400])
@pytest.mark.parametrize("size", [(60, 12), (35, 8)])
async def test_long_listing_restores_visible_prompt_cursor(
    prompt_position, file_count, size
):
    session = Session(
        MemoryFileSystem({f"file-{i:03}.txt": "data" for i in range(file_count)})
    )
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test(size=size) as pilot:
        shell = app.query_one(Shell)
        editor = app.query_one(CommandInput)
        for _ in range(2):
            editor.value = "ls -l"
            await pilot.press("enter")
            await wait_until(lambda: not shell._busy)
            await pilot.pause()
            await wait_until(
                lambda: (
                    editor.has_focus
                    and shell.content_region.contains_region(editor.region)
                    and app.cursor_position == editor.cursor_screen_offset
                )
            )
            if prompt_position == "inline":
                from anyshell.frontend import ShellScroll

                assert shell.query_one(ShellScroll).is_vertical_scroll_end
            await pilot.press("x")
            assert editor.value == "x"


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_mouse_selection_and_copy_of_output(prompt_position):
    from anyshell.frontend import Transcript

    app = ShellApp(prompt_position=prompt_position)
    async with app.run_test(size=(80, 20)) as pilot:
        shell = app.query_one(Shell)
        editor = app.query_one(CommandInput)
        editor.value = "echo hello world"
        await pilot.press("enter")
        await wait_until(lambda: not shell._busy)
        await pilot.pause()
        log = shell.query_one(Transcript)
        await pilot.mouse_down(log, offset=(1, 1))
        await pilot.hover(log, offset=(5, 1))
        await pilot.mouse_up(log, offset=(5, 1))
        await wait_until(lambda: app.screen.get_selected_text() == "hello")
        await wait_until(lambda: editor.has_focus)
        await pilot.press("ctrl+c")
        assert app.clipboard == "hello"
        assert editor.has_focus


async def test_output_selection_handles_unicode_and_multiple_lines():
    from anyshell.frontend import Transcript

    app = ShellApp()
    async with app.run_test(size=(80, 20)) as pilot:
        shell = app.query_one(Shell)
        editor = app.query_one(CommandInput)
        editor.value = "echo 'a界bé\nsecond line'"
        await pilot.press("enter")
        await wait_until(lambda: not shell._busy)
        await pilot.pause()
        log = shell.query_one(Transcript)
        # The two-line submitted command occupies rows 0 and 1.
        await pilot.mouse_down(log, offset=(2, 2))
        await pilot.hover(log, offset=(3, 3))
        await pilot.mouse_up(log, offset=(3, 3))
        await wait_until(lambda: app.screen.get_selected_text() == "界bé\nsec")
        await pilot.press("ctrl+shift+c")
        assert app.clipboard == "界bé\nsec"


@pytest.mark.parametrize("text_style", ["", "white", "bold white", "blue", "dim red"])
async def test_selection_changes_only_background(text_style):
    from anytext.geometry import Offset
    from anytext.selection import Selection
    from rich.text import Text

    from anyshell.frontend import Transcript

    app = ShellApp()
    async with app.run_test() as pilot:
        log = app.query_one(Transcript)
        log.write(Text("white text", style=text_style))
        await pilot.pause()
        original = next(
            segment.style for segment in log.render_line(0) if "white" in segment.text
        )
        app.screen.selections = {log: Selection(Offset(0, 0), Offset(5, 0))}
        await pilot.pause()
        segments = list(log.render_line(0))
        selected = next(segment for segment in segments if segment.text == "white")
        unselected = next(segment for segment in segments if "text" in segment.text)
        highlight = app.screen.get_component_rich_style("screen--selection")
        assert selected.style.color == original.color
        assert selected.style.bold == original.bold
        assert selected.style.dim == original.dim
        assert selected.style.bgcolor == highlight.bgcolor
        assert selected.style.bgcolor != original.bgcolor
        assert unselected.style.color == original.color
        assert unselected.style.bgcolor == original.bgcolor


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_terminal_clicks_keep_active_cursor_focus(prompt_position):
    from anyshell.frontend import RunningCursor, Transcript

    app = ShellApp(prompt_position=prompt_position)
    async with app.run_test(size=(80, 20)) as pilot:
        shell = app.query_one(Shell)
        editor = app.query_one(CommandInput)
        editor.value = "echo hello"
        await pilot.press("enter")
        await wait_until(lambda: not shell._busy)
        for target, offset in (
            ("#shell-terminal", (30, 8)),
            ("#prompt", (0, 0)),
            (Shell, (0, 0)),
            (Transcript, (2, 1)),
        ):
            await pilot.click(target, offset=offset)
            await wait_until(lambda: editor.has_focus)
            await pilot.press("x")
            assert editor.value == "x"
            editor.value = ""
        editor.value = "sleep 30"
        await pilot.press("enter")
        await wait_until(lambda: shell.session.running)
        cursor = shell.query_one(RunningCursor)
        await pilot.click("#shell-terminal", offset=(30, 8))
        await wait_until(lambda: cursor.has_focus)
        await wait_until(lambda: not cursor._draw_cursor)
        await wait_until(lambda: cursor._draw_cursor)
        await pilot.press("ctrl+c")
        await wait_until(lambda: not shell._busy)
        assert editor.has_focus


async def test_manual_scrollback_cancels_pending_prompt_scroll():
    from anyshell.frontend import ShellScroll, Transcript

    app = ShellApp(
        Session(
            MemoryFileSystem({"many.txt": "\n".join(f"line {i}" for i in range(80))})
        )
    )
    async with app.run_test(size=(80, 20)) as pilot:
        shell = app.query_one(Shell)
        editor = app.query_one(CommandInput)
        viewport = shell.query_one(ShellScroll)
        editor.value = "cat many.txt"
        await pilot.press("enter")
        await wait_until(lambda: not shell._busy and viewport.is_vertical_scroll_end)
        await pilot.pause()
        # A command's final scroll request may still be queued when the user scrolls up.
        shell._request_prompt_scroll()
        viewport.scroll_home(animate=False, immediate=True)
        await pilot.pause()
        assert viewport.scroll_y == 0
        # Late layout and output updates must respect the user's chosen position.
        viewport.on_resize()
        shell.query_one(Transcript).write("later output")
        await pilot.pause()
        assert viewport.scroll_y == 0
        assert editor.region.y > viewport.content_region.bottom
        await pilot.press("x")
        await wait_until(lambda: viewport.is_vertical_scroll_end)
        assert editor.value == "x"
