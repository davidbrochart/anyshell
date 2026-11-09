"""Cyclopts integration: typed options and structured pipeline execution."""

import subprocess
import sys
from typing import Annotated

import pytest
from cyclopts import Parameter

from anyshell import Command, Context, LocalFileSystem, MemoryFileSystem, Session


async def execute(session, source):
    events = []

    async def emit(event):
        events.append(event)

    result = await session.execute(source, emit)
    return result, [e.value for e in events if e.kind == "output"], events


@pytest.fixture(params=["memory", "local"])
def filesystem(request, tmp_path):
    fs = MemoryFileSystem() if request.param == "memory" else LocalFileSystem(tmp_path)
    fs.write_text("/visible.txt", "é")
    fs.write_text("/.hidden", "secret")
    fs.mkdir("/docs")
    return fs


@pytest.mark.parametrize("flag", ["-l", "--long"])
async def test_long_listing_metadata_and_pipeline_types(filesystem, flag):
    session = Session(filesystem)
    result, rows, _ = await execute(session, f"ls {flag}")
    assert result.status == 0
    assert {row["name"] for row in rows} == {"visible.txt", "docs"}
    file = next(row for row in rows if row["type"] == "file")
    directory = next(row for row in rows if row["type"] == "directory")
    assert file["size"] == 2  # UTF-8 bytes, not character count.
    assert directory["size"] is None
    assert isinstance(file["modified"], float) and file["modified"] > 0
    assert (await execute(session, "ls -l | where type file | select size"))[1] == [2]
    _, rows, _ = await execute(session, "ls")
    assert all(set(row) == {"name", "type"} for row in rows)


@pytest.mark.parametrize("flag", ["-a", "--all", "-la", "-al"])
async def test_hidden_entries_and_combined_flags(filesystem, flag):
    result, rows, _ = await execute(Session(filesystem), f"ls {flag}")
    assert result.status == 0
    assert {row["name"] for row in rows} == {"visible.txt", "docs", ".hidden"}
    assert all(("size" in row) == (flag in {"-la", "-al"}) for row in rows)


async def test_validation_happens_before_any_pipeline_stage_runs():
    session = Session()
    calls = []

    def marker():
        calls.append(True)

    session.register(Command("marker", "Record execution", marker))
    result, _, events = await execute(session, "marker | count --unknown")
    assert result.status == 1
    assert calls == []
    assert any(e.kind == "error" and "--unknown" in e.value for e in events)
    for source in (
        "sleep not-a-number",
        "sleep -1",
        "sleep nan",
        "select",
        "pwd extra",
    ):
        assert (await execute(session, source))[0].status == 1
    assert (await execute(session, "echo recovered"))[1] == ["recovered"]


async def test_help_is_captured_and_does_not_invoke_commands(capsys):
    session = Session()
    calls = []

    def forbidden():
        calls.append(True)
        raise AssertionError("help invoked the command")

    session.register(Command("probe", "Describe a probe command", forbidden))
    result, values, _ = await execute(session, "probe --help")
    assert result.status == 0 and calls == []
    assert "probe" in "\n".join(values)
    result, values, _ = await execute(session, "help ls")
    assert result.status == 0
    text = "\n".join(values)
    assert "--long" in text and "-l" in text and "--all" in text
    assert "--ctx" not in text and "--stdout" not in text
    assert "\x1b" not in text
    assert (await execute(session, "cd --help | grep directory"))[0].status == 0
    _, _, events = await execute(session, "clear --help")
    assert not any(e.kind == "clear" for e in events)
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


async def test_custom_typed_commands_receive_context_streams_and_return_values():
    session = Session()
    record = {"nested": [1, 2]}

    def source():
        return record

    async def take(
        *,
        limit: Annotated[int, Parameter(name=["--limit", "-n"])] = 1,
        context: Context,
        stdin,
        stdout,
    ):
        assert context.session is session
        count = 0
        async for value in stdin:
            if count < limit:
                await stdout.send(value)
            count += 1

    def add(left: int, right: int):
        return left + right

    for name, function in (("source", source), ("take", take), ("add", add)):
        session.register(Command(name, "A custom typed command", function))
    result, values, _ = await execute(session, "source | take -n 1")
    assert result.status == 0 and values[0] is record
    result, values, _ = await execute(session, "add 2 3")
    assert result.status == 0 and values == [5]  # An integer is data, not an exit code.
    assert (await execute(session, "add 2 3 | count"))[1] == [1]
    assert (await execute(session, "add not-an-int 3"))[0].status == 1


async def test_typed_builtin_options_and_end_of_options():
    session = Session(
        MemoryFileSystem({"words": "Beta\nAlpha\nalpha\n", "-dash": "literal"})
    )
    assert (await execute(session, "cat words | grep -i alpha | sort -r"))[1] == [
        "alpha",
        "Alpha",
    ]
    assert (await execute(session, "cat -- -dash"))[1] == ["literal"]
    assert await session.complete("ls --lo") == ["ls --long"]
    options = await session.complete("ls --")
    assert "ls --help" in options and "ls --long" in options
    assert not any("ctx" in name or "stdout" in name for name in options)


def test_headless_long_listing(tmp_path):
    (tmp_path / "visible.txt").write_text("é", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "anyshell", "--root", str(tmp_path), "-c", "ls -l"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "Bytes" in result.stdout and "Modified (UTC)" in result.stdout
    assert "visible.txt" in result.stdout and '"name"' not in result.stdout


async def test_listing_explicit_file_paths(filesystem):
    session = Session(filesystem)
    for source, name in (("ls visible.txt", "visible.txt"), ("ls /.hidden", ".hidden")):
        result, rows, _ = await execute(session, source)
        assert result.status == 0
        assert rows == [{"name": name, "type": "file"}]
    filesystem.write_text("/docs/nested.txt", "nested")
    await execute(session, "cd docs")
    result, rows, _ = await execute(session, "ls -l ../visible.txt")
    assert result.status == 0
    assert rows == [
        {"name": "visible.txt", "type": "file", **filesystem.stat("/visible.txt")}
    ]
    assert (await execute(session, "ls /docs/nested.txt | select name"))[1] == [
        "nested.txt"
    ]
    for flag in ("", "-l"):
        result, rows, events = await execute(session, f"ls {flag} /missing.txt")
        assert result.status == 1 and rows == []
        assert any(event.kind == "error" for event in events)
