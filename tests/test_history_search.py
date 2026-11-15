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
        popup = shell.query_one(CompletionPopup)
        # Resizing must not hide an open search between layout frames.
        shell.on_resize()
        assert popup.display and shell.history_search_open
        await pilot.resize_terminal(60, 18)
        await pilot.pause()
        assert popup.display and shell.history_search_open
        # Flush the deferred positioning callback, then its resulting layout.
        positioned = anyio.Event()
        shell.on_resize()
        shell.call_after_refresh(positioned.set)
        with anyio.fail_after(5):
            await positioned.wait()
            await pilot.pause()
            menu = shell.query_one(CompletionMenu)
            while (
                popup.region not in app.screen.region
                or app.screen.get_style_at(
                    menu.region.x + 2, menu.region.y + 1
                ).meta.get("option")
                != 1
            ):
                await pilot.pause()
        assert popup.region in app.screen.region
        assert await pilot.click(menu, offset=(2, 1))
        with anyio.fail_after(5):
            while shell.history_search_open:
                await pilot.pause()
        assert editor.value == "echo old"
        assert editor.has_focus
        assert not shell.history_search_open and not popup.display
        assert session.history == ["echo old", "echo new"]


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_cancel_history_before_pending_resize_refresh(prompt_position):
    """A queued resize refresh cannot reopen a cancelled search popup."""
    session = Session()
    session.history[:] = ["echo old"]
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test() as pilot:
        shell = app.query_one(Shell)
        editor = app.query_one(CommandInput)
        editor.value = "draft"
        await pilot.press("ctrl+r")
        shell.on_resize()
        shell.finish_history_search(accept=False)
        await pilot.pause()
        assert not shell.history_search_open
        assert not shell.query_one(CompletionPopup).display
        assert editor.value == "draft"


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_resize_preserves_history_options_and_selection(prompt_position):
    """A resize cannot rebuild options or reset a selected history entry."""
    session = Session()
    session.history[:] = ["echo old", "echo new"]
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test() as pilot:
        shell = app.query_one(Shell)
        await pilot.press("ctrl+r", "ctrl+r")
        menu = shell.query_one(CompletionMenu)
        options = tuple(menu.options)
        assert menu.highlighted == 1 and shell._search_index == 0
        positioned = anyio.Event()
        shell.on_resize()
        shell.call_after_refresh(positioned.set)
        with anyio.fail_after(5):
            await positioned.wait()
        assert tuple(menu.options) == options
        assert menu.highlighted == 1 and shell._search_index == 0


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_history_click_uses_selected_option_event(prompt_position):
    """A queued click keeps its selected entry even if the highlight moves."""
    session = Session()
    session.history[:] = ["echo old", "echo new"]
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test() as pilot:
        shell = app.query_one(Shell)
        await pilot.press("ctrl+r")
        menu = shell.query_one(CompletionMenu)
        event = menu.OptionSelected(menu, menu.get_option_at_index(1), 1)
        menu.highlighted = 0
        shell.completion_selected(event)
        assert shell.query_one(CommandInput).value == "echo old"
        assert not shell.history_search_open


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_history_click_from_previous_results_is_ignored(prompt_position):
    """An old click cannot select an entry from newly filtered results."""
    session = Session()
    session.history[:] = ["echo old", "echo new"]
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test() as pilot:
        shell = app.query_one(Shell)
        await pilot.press("ctrl+r")
        menu = shell.query_one(CompletionMenu)
        event = menu.OptionSelected(menu, menu.get_option_at_index(1), 1)
        await pilot.press("n")
        shell.completion_selected(event)
        assert shell.query_one(CommandInput).value == "n"
        assert shell.history_search_open
