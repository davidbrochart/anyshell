"""Trailing lines and cancellable file following on both workspace filesystems."""

import anyio
import pytest

from anyshell import LocalFileSystem, MemoryFileSystem, Session


@pytest.fixture(params=["memory", "local"])
def fs(request, tmp_path):
    return (
        MemoryFileSystem() if request.param == "memory" else LocalFileSystem(tmp_path)
    )


async def execute(session, command):
    events = []

    async def emit(event):
        events.append(event)

    result = await session.execute(command, emit)
    return result, [event.value for event in events if event.kind == "output"], events


@pytest.mark.parametrize("ending", ["", "\n", "\r\n"])
async def test_last_lines_and_counts(fs, ending):
    fs.write_text("/log", "\r\n".join(str(i) for i in range(15)) + ending)
    session = Session(fs)
    for command, expected in [
        ("tail log", [str(i) for i in range(5, 15)]),
        ("tail -n 2 log", ["13", "14"]),
        ("tail --lines 20 log", [str(i) for i in range(15)]),
        ("tail -n 0 log", []),
    ]:
        result, output, _ = await execute(session, command)
        assert result.status == 0
        assert output == expected


async def test_stdin_multiple_files_and_redirection(fs):
    fs.write_text("/a", "a\nb\nc\n")
    fs.write_text("/b", "x\ny\n")
    fs.write_text("/empty", "")
    session = Session(fs)
    for command, expected in [
        ("cat a | tail -n 2", ["b", "c"]),
        ("tail -n 1 <a", ["c"]),
        ("cat a | tail -f -n 1 -", ["c"]),
        ("tail -n 1 a b", ["==> a <==", "c", "==> b <==", "y"]),
        ("tail empty", []),
        ("ls | tail -n 1 | select name", ["empty"]),
    ]:
        result, output, _ = await execute(session, command)
        assert result.status == 0
        assert output == expected
    result, output, _ = await execute(session, "tail -n 2 a >out")
    assert result.status == 0 and output == []
    assert fs.read_text("/out") == "b\nc\n"


@pytest.mark.parametrize(
    "command",
    [
        "tail missing",
        "tail -f missing",
        "tail -n 0 -f missing",
        "tail /",
        "tail -f /",
        "tail -n -1",
        "tail -n nope",
    ],
)
async def test_errors_and_recovery(fs, command):
    session = Session(fs)
    result, output, events = await execute(session, command)
    assert result.status == 1 and output == []
    assert any(event.kind == "error" for event in events)
    assert (await execute(session, "echo recovered"))[1] == ["recovered"]


async def test_follow_appends_partial_lines_unicode_truncation_and_kill(fs):
    fs.write_text("/log", "old\nlast\n")
    session = Session(fs)
    received = []
    signals = {value: anyio.Event() for value in ["last", "héllo", "partial", "new"]}

    async def emit(event):
        if event.kind == "output":
            received.append(event.value)
            if event.value in signals:
                signals[event.value].set()

    # Force UTF-8 characters and CRLF endings to span read boundaries.
    read_bytes = fs.read_bytes
    fs.read_bytes = lambda path, offset, size: read_bytes(path, offset, min(2, size))
    with anyio.fail_after(5):
        async with session.background():
            result = await session.execute("tail -f -n 1 log &", emit)
            assert result.status == 0
            await signals["last"].wait()
            fs.append_text("/log", "héllo\r\npart")
            await signals["héllo"].wait()
            fs.append_text("/log", "ial\n")
            await signals["partial"].wait()
            fs.write_text("/log", "new\n")
            await signals["new"].wait()
            job = session.jobs[1]
            assert not job.done.is_set()
            assert (await execute(session, "kill 1"))[0].status == 0
            await job.done.wait()
            assert job.result.cancelled and job.result.status == 130
    assert received == ["last", "héllo", "partial", "new"]


@pytest.mark.parametrize("count", [0, 1])
async def test_follow_initial_partial_and_foreground_cancel(fs, count):
    fs.write_text("/log", "old\npart")
    session = Session(fs)
    ready, emitted = anyio.Event(), anyio.Event()
    received, results = [], []
    stat = fs.stat

    def observed_stat(path):
        value = stat(path)
        ready.set()
        return value

    fs.stat = observed_stat

    async def emit(event):
        if event.kind == "output":
            received.append(event.value)
            emitted.set()

    async def run():
        results.append(await session.execute(f"tail -n {count} -f log", emit))

    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(run)
            await ready.wait()
            # A following command emits complete lines, retaining partial text.
            fs.append_text("/log", "ial\n")
            await emitted.wait()
            assert session.cancel()
    assert received == (["ial"] if count == 0 else ["partial"])
    assert results[0].cancelled and results[0].status == 130
    assert not session.running


async def test_follow_multiple_files_and_pipeline(fs):
    fs.write_text("/a", "before\n")
    fs.write_text("/b", "before\n")
    session = Session(fs)
    initial, appended = anyio.Event(), anyio.Event()
    output = []

    async def emit(event):
        if event.kind == "output":
            output.append(event.value)
            if output == ["==> a <==", "before", "==> b <==", "before"]:
                initial.set()
            if event.value == "after":
                appended.set()

    with anyio.fail_after(5):
        async with session.background():
            await session.execute("tail -f a b | grep . &", emit)
            await initial.wait()
            fs.append_text("/a", "after\n")
            await appended.wait()
    assert output == [
        "==> a <==",
        "before",
        "==> b <==",
        "before",
        "==> a <==",
        "after",
    ]


async def test_follow_between_sessions_without_sleep_or_leaked_handles(fs, monkeypatch):
    """Shared filesystem writes wake followers without a polling timer."""
    fs.write_text("/log", "ready\n")
    follower, writer = Session(fs), Session(fs)
    ready, appended = anyio.Event(), anyio.Event()

    async def forbid_sleep(*args, **kwargs):
        pytest.fail("tail -f must await notifications rather than sleep")

    monkeypatch.setattr("anyshell.commands.anyio.sleep", forbid_sleep)

    async def emit(event):
        if event.kind == "output":
            if event.value == "ready":
                ready.set()
            elif event.value == "from another session":
                appended.set()

    with anyio.fail_after(5):
        async with follower.background():
            await follower.execute("tail -f log &", emit)
            await ready.wait()
            result, _, _ = await execute(writer, 'echo "from another session" >> log')
            assert result.status == 0
            await appended.wait()
    assert not fs._handles


async def test_follow_reports_removed_file_and_unsubscribes(fs):
    fs.write_text("/log", "ready\n")
    session = Session(fs)
    ready = anyio.Event()
    events = []

    async def emit(event):
        events.append(event)
        if event.kind == "output" and event.value == "ready":
            ready.set()

    with anyio.fail_after(5):
        async with session.background():
            await session.execute("tail -f log &", emit)
            await ready.wait()
            fs.remove("/log")
            job = session.jobs[1]
            await job.done.wait()
            assert job.result.status == 1
    errors = [event.value for event in events if event.kind == "error"]
    assert len(errors) == 1 and errors[0].startswith("tail: ")
    assert "no such file or directory" in errors[0].lower()
    assert "TaskGroup" not in errors[0]
    assert not fs._handles
