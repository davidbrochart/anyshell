"""Exit ends the issuing session and lets the UI close its owning pane."""

import subprocess
import sys

import anyio
import pytest

from anyshell import Session, ShellError
from anyshell.frontend import CommandInput, Shell, ShellApp
from anyshell.panes import ShellWorkspace


async def execute(session, source):
    events = []

    async def emit(event):
        events.append(event)

    result = await session.execute(source, emit)
    return result, events


@pytest.mark.parametrize(
    "source,status", [("exit", 0), ("exit 17", 17), ("exit 258", 2)]
)
async def test_exit_stops_sequence_and_emits_status(source, status):
    session = Session()
    result, events = await execute(session, source + "; echo should-not-run")
    assert result.status == status
    assert session.exit_status == status
    assert not [e for e in events if e.kind == "output"]
    assert [e.value for e in events if e.kind == "exit"] == [status]
    assert not session.running
    with pytest.raises(ShellError, match="exited"):
        await execute(session, "echo later")


async def test_exit_defaults_to_previous_status():
    session = Session()
    result, _ = await execute(session, "cat missing; exit")
    assert result.status == 1
    session = Session()
    await execute(session, "cat missing")
    result, _ = await execute(session, "exit")
    assert result.status == 1


@pytest.mark.parametrize("source", ["exit invalid", "exit 1 2", "exit | cat"])
async def test_invalid_exit_keeps_session_open(source):
    session = Session()
    result, _ = await execute(session, source)
    assert result.status == 1
    assert session.exit_status is None
    assert (await execute(session, "echo still-open"))[0].status == 0


async def test_background_exit_does_not_close_parent():
    session = Session()
    async with session.background():
        result, events = await execute(session, "exit 17 & echo still-open")
        with anyio.fail_after(3):
            await session.wait_jobs()
        assert session.jobs[1].result.status == 17
        assert session.exit_status is None
        assert result.status == 0
        assert [e.value for e in events if e.kind == "output"] == ["still-open"]
        assert not [e for e in events if e.kind == "exit"]


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
@pytest.mark.parametrize("exit_key", ["enter", "ctrl+d"])
async def test_exit_closes_current_pane_and_its_jobs(prompt_position, exit_key):
    app = ShellApp(prompt_position=prompt_position)
    async with app.run_test() as pilot:
        workspace = app.query_one(ShellWorkspace)
        original = workspace.active
        await pilot.press("alt+v")
        closing = workspace.active
        closing.query_one(CommandInput).value = "sleep 30 &"
        await pilot.press("enter")
        with anyio.fail_after(5):
            while not closing.session.jobs or closing._busy:
                await anyio.sleep(0.01)
        job = closing.session.jobs[1]
        if exit_key == "enter":
            closing.query_one(CommandInput).value = "exit"
        await pilot.press(exit_key)
        with anyio.fail_after(5):
            while closing.is_attached or not job.done.is_set():
                await anyio.sleep(0.01)
        await pilot.pause()
        assert workspace.panes == [original]
        assert original.query_one(CommandInput).has_focus
        assert job.result.cancelled
        assert app.is_running


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
@pytest.mark.parametrize("exit_key", ["enter", "ctrl+d"])
async def test_exit_last_pane_quits_app(prompt_position, exit_key):
    app = ShellApp(prompt_position=prompt_position)
    async with app.run_test() as pilot:
        app.query_one(Shell).session.last_status = 17
        if exit_key == "enter":
            app.query_one(CommandInput).value = "exit 17"
        await pilot.press(exit_key)
        with anyio.fail_after(5):
            while app.is_running:
                await anyio.sleep(0.01)
    assert app.return_value == 17


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_ctrl_d_preserves_nonempty_input(prompt_position):
    app = ShellApp(prompt_position=prompt_position)
    async with app.run_test() as pilot:
        editor = app.query_one(CommandInput)
        editor.value = "echo hello"
        await pilot.press("ctrl+d")
        assert editor.value == "echo hello"
        editor.cursor_position = 5
        await pilot.press("ctrl+d")
        assert editor.value == "echo ello"
        assert app.is_running
        assert app.query_one(Shell).session.exit_status is None


async def test_exit_message_closes_issuing_pane_without_stealing_focus():
    app = ShellApp()
    async with app.run_test() as pilot:
        workspace = app.query_one(ShellWorkspace)
        original = workspace.active
        await pilot.press("alt+v")
        active = workspace.active
        original.post_message(Shell.Exited(original, 0))
        with anyio.fail_after(5):
            while original.is_attached:
                await anyio.sleep(0.01)
        await pilot.pause()
        assert workspace.panes == [active]
        assert active.query_one(CommandInput).has_focus


def test_headless_exit_does_not_wait_for_background_jobs(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "anyshell",
            "--root",
            str(tmp_path),
            "-c",
            "sleep 30 & exit 17; echo should-not-run",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 17
    assert not result.stdout
