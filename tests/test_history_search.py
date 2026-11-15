"""Reverse history search keeps query editing separate from command execution."""

import anyio
import pytest

from anyshell import Session
from anyshell.frontend import (
    CommandInput,
    CompletionMenu,
    CompletionPopup,
    Shell,
    ShellApp,
)


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_search_matches_cycles_and_accepts_without_running(prompt_position):
    session = Session()
    session.history[:] = [
        "echo alpha old",
        "echo beta",
        "echo alpha new",
        "echo alpha new",
    ]
    original = list(session.history)
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test() as pilot:
        editor = app.query_one(CommandInput)
        shell = app.query_one(Shell)
        await pilot.press("ctrl+r", "a", "l", "p", "h", "a")
        assert shell.history_search_open
        assert editor.value == "alpha"
        assert shell._search_index == 3
        popup = shell.query_one(CompletionPopup)
        assert popup.display
        assert shell.query_one(CompletionMenu).option_count == 2
        assert popup.region in app.screen.region
        await pilot.press("ctrl+r")
        assert shell._search_index == 0
        await pilot.press("up")
        assert shell._search_index == 3
        await pilot.press("down")
        assert shell._search_index == 0
        await pilot.press("enter")
        assert editor.value == "echo alpha old"
        assert editor.cursor_position == len(editor.value)
        assert not shell.history_search_open
        assert not shell.query_one(CompletionPopup).display
        assert editor.has_focus and not shell._busy
        assert session.history == original
        await pilot.press("enter")
        with anyio.fail_after(5):
            while shell._busy:
                await anyio.sleep(0.01)
        assert any(e.kind == "output" and e.value == "alpha old" for e in shell.output)


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
@pytest.mark.parametrize("key", ["escape", "ctrl+c"])
async def test_cancel_search_restores_draft_and_cursor(prompt_position, key):
    session = Session()
    session.history[:] = ["echo draft match"]
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test() as pilot:
        editor = app.query_one(CommandInput)
        shell = app.query_one(Shell)
        editor.value = "draft"
        editor.cursor_position = 2
        await pilot.press("ctrl+r")
        assert shell._search_index == 0
        await pilot.press("end", "x", key)
        assert editor.value == "draft"
        assert editor.cursor_position == 2
        assert not shell.history_search_open
        assert editor.has_focus
        assert session.history == ["echo draft match"]


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_no_match_backspace_and_multiline_history(prompt_position):
    session = Session()
    session.history[:] = ["echo one\necho two", "echo other"]
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test() as pilot:
        shell = app.query_one(Shell)
        editor = app.query_one(CommandInput)
        editor.value = "twoX"
        await pilot.press("ctrl+r", "enter")
        assert shell.history_search_open and shell._search_index is None
        assert not shell._busy
        await pilot.press("backspace")
        assert shell._search_index == 0
        await pilot.press("enter")
        assert editor.value == "echo one\necho two"
        assert not shell.history_search_open
        assert session.history == ["echo one\necho two", "echo other"]


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_empty_history_and_completion_dismissal(prompt_position):
    app = ShellApp(prompt_position=prompt_position)
    async with app.run_test() as pilot:
        shell = app.query_one(Shell)
        editor = app.query_one(CommandInput)
        editor.value = "c"
        await pilot.press("tab")
        assert shell.completion_open
        await pilot.press("ctrl+r")
        assert not shell.completion_open and shell.history_search_open
        assert shell._search_index is None
        await pilot.press("escape")
        assert editor.value == "c"
        assert not shell.history_search_open


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_history_popup_resize_and_mouse_selection(prompt_position):
    session = Session()
    session.history[:] = ["echo old", "echo new"]
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test() as pilot:
        shell = app.query_one(Shell)
        editor = app.query_one(CommandInput)
        await pilot.press("ctrl+r")
        await pilot.resize_terminal(60, 18)
        await pilot.pause()
        popup = shell.query_one(CompletionPopup)
        assert popup.display and shell.history_search_open
        assert popup.region in app.screen.region
        await pilot.click(shell.query_one(CompletionMenu), offset=(2, 1))
        await pilot.pause()
        assert editor.value == "echo old"
        assert editor.has_focus
        assert not shell.history_search_open and not popup.display
        assert session.history == ["echo old", "echo new"]
