import anyio
import pytest
from rich.columns import Columns
from rich.table import Table
from rich.text import Text

from anyshell import Command, MemoryFileSystem, Session


async def execute(session, source, *, is_terminal=True):
    events = []

    async def emit(event):
        events.append(event)

    result = await session.execute(source, emit, is_terminal=is_terminal)
    assert result.status == 0, events
    return [event.value for event in events if event.kind == "output"]


@pytest.mark.parametrize("is_terminal", [False, True])
async def test_output_target_and_styled_values(is_terminal):
    session = Session()

    async def report(*, stdout):
        await stdout.send(
            Text("hello", style="bold red") if stdout.is_terminal else "hello"
        )

    session.register(Command("report", "Report output target", report))
    output = await execute(session, "report", is_terminal=is_terminal)
    assert isinstance(output[0], Text) == is_terminal
    assert str(output[0]) == "hello"
    assert await execute(session, "report | cat", is_terminal=is_terminal) == ["hello"]
    await execute(session, "data = report", is_terminal=is_terminal)
    assert session.variables["data"] == ["hello"]
    await execute(session, "report > result; report >> result", is_terminal=is_terminal)
    assert session.filesystem.read_text("/result") == "hello\nhello\n"


async def test_script_and_background_inherit_terminal():
    session = Session()
    targets = []

    async def report(label: str, *, stdout):
        targets.append((label, stdout.is_terminal))

    session.register(Command("report", "Report output target", report))
    async with session.background():
        await execute(session, "if True:\n    report foreground")
        await execute(session, "report simple &")
        await execute(session, 'report {"expression"} &')
        with anyio.fail_after(2):
            await session.wait_jobs()
    assert sorted(targets) == [
        ("expression", True),
        ("foreground", True),
        ("simple", True),
    ]


@pytest.mark.parametrize("redirect", [False, True])
async def test_only_unredirected_last_stage_sees_terminal(redirect):
    session = Session()
    targets = []

    async def report(label: str, *, stdout):
        targets.append((label, stdout.is_terminal))

    session.register(Command("report", "Report output target", report))
    await execute(
        session, "report first | report last" + (" > result" if redirect else "")
    )
    assert sorted(targets) == [("first", False), ("last", not redirect)]


@pytest.mark.parametrize("long", [False, True])
async def test_ls_chooses_presentation_and_keeps_raw_records(long):
    session = Session(
        MemoryFileSystem({"note.txt": "text", "docs/readme.txt": "hello"})
    )
    command = "ls -l" if long else "ls"
    output = await execute(session, command)
    assert len(output) == 1
    assert isinstance(output[0], Table if long else Columns)
    await execute(session, f"data = {command}")
    records = session.variables["data"]
    assert [row["name"] for row in records] == ["docs", "note.txt"]
    assert all(("size" in row) == long for row in records)
    assert await execute(session, f"{command} | select name") == ["docs", "note.txt"]
