"""File redirections retain object pipelines until the file boundary."""

import subprocess
import sys

import anyio
import pytest

from anyshell import Command, LocalFileSystem, MemoryFileSystem, Session


@pytest.fixture(params=["memory", "local"])
def fs(request, tmp_path):
    return (
        MemoryFileSystem() if request.param == "memory" else LocalFileSystem(tmp_path)
    )


async def execute(session, source):
    events = []

    async def emit(event):
        events.append(event)

    result = await session.execute(source, emit)
    return result, [e.value for e in events if e.kind == "output"], events


async def test_output_overwrite_append_and_create(fs):
    session = Session(fs)
    result, output, _ = await execute(session, "echo one>file")
    assert result.status == 0 and output == []
    assert fs.read_text("/file") == "one\n"
    assert (await execute(session, "echo two >>file"))[0].status == 0
    assert fs.read_text("/file") == "one\ntwo\n"
    await execute(session, "echo replaced > file")
    assert fs.read_text("/file") == "replaced\n"
    await execute(session, "sleep 0 > file")
    assert fs.read_text("/file") == ""
    await execute(session, "sleep 0 >>empty")
    assert fs.read_text("/empty") == ""


async def test_input_pipelines_and_structured_output(fs):
    fs.write_text("/input", "beta\r\nalpha\r\nalpha\r\n")
    session = Session(fs)
    result, output, _ = await execute(session, "sort<input | count >counted")
    assert result.status == 0 and output == []
    assert fs.read_text("/counted") == "3\n"
    assert (await execute(session, "<input cat | grep ^a"))[1] == ["alpha", "alpha"]
    await execute(session, "mkdir docs; touch docs/a; find docs >paths")
    assert fs.read_text("/paths") == "docs\ndocs/a\n"


async def test_quotes_order_and_multiple_redirects(fs):
    session = Session(fs)
    result, _, _ = await execute(session, """echo "a>b" 'c<d' \\> >'file name' """)
    assert result.status == 0
    assert fs.read_text("/file name") == "a>b c<d >\n"
    await execute(session, "echo first >first >last")
    assert fs.read_text("/first") == ""
    assert fs.read_text("/last") == "first\n"
    fs.write_text("/input", "old\n")
    await execute(session, "cat <input >input")
    assert fs.read_text("/input") == ""
    fs.write_text("/input", "old\n")
    await execute(session, "cat >input <input")
    assert fs.read_text("/input") == ""


@pytest.mark.parametrize(
    "source",
    ["echo hi >", "cat <", "echo hi > >file", "echo hi 2>file", "cat <<file", ">file"],
)
async def test_invalid_redirection_is_validated_before_commands(fs, source):
    session = Session(fs)
    result, output, _ = await execute(session, "touch untouched; " + source)
    assert result.status == 1 and output == []
    with pytest.raises(FileNotFoundError):
        fs.entry_type("/untouched")


async def test_redirection_errors_do_not_run_command(fs):
    session = Session(fs)
    called = []

    async def mark(*, stdout):
        called.append(True)
        await stdout.send("ran")

    session.register(Command("mark", "Record invocation", mark))
    fs.mkdir("/directory")
    for source in (
        "mark <missing",
        "mark >directory",
        "mark >>directory",
        "mark >missing/file",
    ):
        result, output, events = await execute(session, source)
        assert result.status == 1 and output == []
        assert any(e.kind == "error" for e in events)
    assert called == []
    result, output, events = await execute(session, "cat missing >output")
    assert result.status == 1 and output == []
    assert any(e.kind == "error" for e in events)
    assert fs.read_text("/output") == ""


async def test_redirect_overrides_pipe_and_downstream_can_exit(fs):
    fs.write_text("/input", "from file\n")
    session = Session(fs)

    async def many(*, stdout):
        for index in range(100):
            await stdout.send(str(index))

    session.register(Command("many", "Produce beyond the buffer", many))
    with anyio.fail_after(3):
        result, output, _ = await execute(session, "many | cat <input")
    assert result.status == 0 and output == ["from file"]
    assert (await execute(session, "echo data >output | count"))[1] == [0]
    assert fs.read_text("/output") == "data\n"


async def test_background_and_working_directory(fs):
    fs.mkdir("/docs")
    fs.write_text("/docs/input", "hello\n")
    session = Session(fs)
    async with session.background():
        result, _, _ = await execute(session, "cd docs; cat <input >>output & cd /")
        with anyio.fail_after(3):
            assert await session.wait_jobs() == 0
    assert result.status == 0
    assert fs.read_text("/docs/output") == "hello\n"


def test_headless_redirection(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "anyshell",
            "--root",
            str(tmp_path),
            "-c",
            "echo hello >file; cat <file",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout == "hello\n"
    assert result.stderr == ""


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_frontend_redirection(prompt_position):
    from anyshell.frontend import CommandInput, Shell, ShellApp

    session = Session()
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test() as pilot:
        editor = app.query_one(CommandInput)
        editor.value = "echo hello >file; cat <file"
        await pilot.press("enter")
        shell = app.query_one(Shell)
        with anyio.fail_after(5):
            while shell._busy:
                await anyio.sleep(0.01)
        assert session.filesystem.read_text("/file") == "hello\n"
        assert [e.value for e in shell.output if e.kind == "output"] == ["hello"]
