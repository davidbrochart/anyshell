"""Multiline command syntax and the growing interactive command editor."""

import anyio
import pytest

from anyshell import MemoryFileSystem, Session, ShellError
from anyshell.engine import is_complete, parse_commands
from anyshell.frontend import CommandInput, Shell, ShellApp


async def execute(session, source):
    events = []

    async def emit(event):
        events.append(event)

    result = await session.execute(source, emit)
    return result, [event.value for event in events if event.kind == "output"]


def test_multiline_parser_and_completeness():
    assert parse_commands("echo one\necho two") == [
        [["echo", "one"]],
        [["echo", "two"]],
    ]
    assert parse_commands("echo a\\\nb |\n cat") == [[["echo", "ab"], ["cat"]]]
    assert parse_commands("echo 'first\nsecond'") == [[["echo", "first\nsecond"]]]
    assert parse_commands('echo "ab\\\ncd"') == [[["echo", "abcd"]]]
    assert parse_commands("echo 'ab\\\ncd'") == [[["echo", "ab\\\ncd"]]]
    assert parse_commands("echo a\\|\necho b") == [[["echo", "a|"]], [["echo", "b"]]]
    for source in ("echo 'unfinished", 'echo "unfinished', "echo hi |", "echo hi\\"):
        assert not is_complete(source)
        with pytest.raises(ShellError):
            parse_commands(source)
    assert is_complete("echo hi\\\n")
    assert is_complete("echo 'finished'")
    assert is_complete("echo \\\\")


async def test_multiline_sequences_continuations_and_history():
    fs = MemoryFileSystem()
    fs.mkdir("/docs")
    session = Session(fs)
    source = 'cd docs\nwrite hello.txt \\\n "hello world"\ncat hello.txt |\n count\npwd'
    result, output = await execute(session, source)
    assert result.status == 0 and output == [1, "/docs"]
    assert fs.read_text("/docs/hello.txt") == "hello world\n"
    assert session.history == [source]
    result, output = await execute(session, "echo 'first\nsecond'\necho last")
    assert output == ["first\nsecond", "last"]
    assert result.status == 0
    result, output = await execute(session, "echo before\nsleep invalid")
    assert result.status == 1 and output == []  # Validate before executing.
    result, output = await execute(session, "cat missing\necho recovered")
    assert result.status == 0 and output == ["recovered"]
    assert (await execute(session, "echo hi\\\n"))[1] == ["hi"]


async def test_multiline_completion_context():
    session = Session(MemoryFileSystem({"file.txt": ""}))
    assert await session.complete("echo first\nec") == ["echo first\necho"]
    assert "echo first\nls" in await session.complete("echo first\n")
    assert await session.complete("ls \\\n --lo") == ["ls \\\n --long"]
    assert await session.complete("echo hi |\n co") == ["echo hi |\n count"]
    assert await session.complete("ls fi\\\nle") == []


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
@pytest.mark.parametrize(
    "start,end,output",
    [
        ("echo 'first", "second'", "first\nsecond"),
        ("echo hi |", "cat", "hi"),
        ("echo one\\", "two", "onetwo"),
    ],
)
async def test_enter_continues_incomplete_command(prompt_position, start, end, output):
    session = Session()
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test() as pilot:
        editor = app.query_one(CommandInput)
        editor.value = start
        await pilot.press("enter")
        assert editor.value == start + "\n"
        assert not session.history
        assert editor.size.height >= 2
        editor.value += end
        await pilot.press("enter")
        with anyio.fail_after(5):
            while app.query_one(Shell)._busy:
                await anyio.sleep(0.01)
        shell = app.query_one(Shell)
        assert [e.value for e in shell.output if e.kind == "output"] == [output]
        assert session.history == [start + "\n" + end]
        assert editor.value == "" and editor.size.height == 1
        await pilot.press("up")
        assert editor.value == session.history[0]
        await pilot.press("alt+down")
        assert editor.value == ""


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_shift_enter_sequences_cursor_navigation_and_cancel(prompt_position):
    app = ShellApp(prompt_position=prompt_position)
    async with app.run_test() as pilot:
        editor = app.query_one(CommandInput)
        editor.value = "echo first"
        await pilot.press("shift+enter")
        assert editor.value == "echo first\n"
        editor.value += "echo second"
        await pilot.press("up")
        assert editor.cursor_location[0] == 0
        await pilot.press("down")
        assert editor.cursor_location[0] == 1
        await pilot.press("enter")
        with anyio.fail_after(5):
            while app.query_one(Shell)._busy:
                await anyio.sleep(0.01)
        assert [e.value for e in app.query_one(Shell).output if e.kind == "output"] == [
            "first",
            "second",
        ]
        editor.value = "echo 'unfinished"
        await pilot.press("enter", "ctrl+c")
        assert editor.value == ""
