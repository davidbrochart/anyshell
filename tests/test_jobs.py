import anyio
import pytest

from anyshell import Command, MemoryFileSystem, Session, ShellError
from anyshell.engine import is_complete, parse_commands
from anyshell.frontend import CommandInput, Shell, ShellApp


async def execute(session, source):
    events = []

    async def emit(event):
        events.append(event)

    result = await session.execute(source, emit)
    return result, events


def test_background_syntax():
    assert parse_commands('echo "a&b" & echo next') == [
        [["echo", "a&b"]],
        [["echo", "next"]],
    ]
    assert parse_commands(r"echo a\&b") == [[["echo", "a&b"]]]
    assert is_complete("sleep 1 &")
    for source in ("&", "echo a && echo b", "echo a | &"):
        with pytest.raises(ShellError):
            parse_commands(source)


async def test_background_requires_lifetime_and_validates_before_launch():
    session = Session()
    result, _ = await execute(session, "sleep 30 &")
    assert result.status == 1
    assert not session.jobs
    async with session.background():
        result, _ = await execute(session, "sleep 30 & missing-command")
        assert result.status == 1
        assert not session.jobs


async def test_jobs_return_prompt_and_can_be_cancelled():
    session = Session()
    async with session.background():
        with anyio.fail_after(3):
            assert (await execute(session, "sleep 30 & echo ready"))[0].status == 0
            assert not session.running
            assert session.last_status == 0
            assert session.jobs[1].result is None
            _, events = await execute(session, "jobs")
            assert any(e.kind == "output" and "[1] running" in e.value for e in events)
            assert not session.cancel()
            assert (await execute(session, "kill 1"))[0].status == 0
            await session.wait_jobs(1)
        assert session.jobs[1].result.cancelled
        assert session.jobs[1].result.status == 130
        assert (await execute(session, "wait 1"))[0].status == 1
        assert (await execute(session, "kill 99"))[0].status == 1
        assert (await execute(session, "wait 99"))[0].status == 1


async def test_background_pipeline_uses_launch_directory_and_shared_filesystem():
    session = Session(MemoryFileSystem({"docs/message": "hello\n"}))
    gate = anyio.Event()

    async def delayed(*, ctx, stdout):
        await gate.wait()
        await stdout.send(ctx.fs.read_text(ctx.resolve("message")).strip())

    session.register(Command("delayed", "Read later", delayed))
    events = []

    async def emit(event):
        events.append(event)

    async with session.background():
        await session.execute("cd docs\ndelayed | cat &\ncd /", emit)
        gate.set()
        with anyio.fail_after(3):
            assert await session.wait_jobs() == 0
        assert session.cwd == "/"
        assert [e.value for e in events if e.kind == "output"] == ["hello"]
        assert session.last_status == 0


async def test_background_failure_and_interactive_commands():
    session = Session()
    async with session.background():
        await execute(session, "cat missing &\nedit file &\necho hello | less &")
        with anyio.fail_after(3):
            await session.wait_jobs()
        assert all(job.result.status == 1 for job in session.jobs.values())
        assert session.last_status == 0


async def test_context_exit_cancels_jobs():
    session = Session()
    with anyio.fail_after(3):
        async with session.background():
            await execute(session, "sleep 30 &")
    assert session.jobs[1].done.is_set()
    assert session.jobs[1].result.cancelled


async def test_ctrl_c_cancels_wait_but_preserves_job():
    session = Session()
    async with session.background():
        await execute(session, "sleep 30 &")
        async with anyio.create_task_group() as group:
            group.start_soon(execute, session, "wait")
            with anyio.fail_after(3):
                while not session.running:
                    await anyio.lowlevel.checkpoint()
                session.cancel()
        assert session.last_status == 130
        assert session.jobs[1].result is None


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_frontend_background_prompt_and_shutdown(prompt_position):
    session = Session()
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test() as pilot:
        editor = app.query_one(CommandInput)
        editor.value = "sleep 30 &"
        await pilot.press("enter")
        with anyio.fail_after(5):
            while not session.jobs or app.query_one(Shell)._busy:
                await anyio.sleep(0.01)
        editor.value = "echo available"
        await pilot.press("enter")
        with anyio.fail_after(5):
            while app.query_one(Shell)._busy:
                await anyio.sleep(0.01)
        assert any(
            e.kind == "output" and e.value == "available"
            for e in app.query_one(Shell).output
        )
    assert session.jobs[1].done.is_set()


async def test_completion_after_background_separator():
    session = Session()
    assert await session.complete("sleep 30 & ec") == ["sleep 30 & echo"]
    assert await session.complete("sleep 30 &\n ec") == ["sleep 30 &\n echo"]


def test_headless_background_jobs_finish():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "anyshell", "-c", "sleep 0.01 & echo ready; sleep 0.01"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ready\n"
    assert "[1] running" in result.stderr and "[1] done (0)" in result.stderr


async def test_jobs_reports_completed_once_and_keeps_running_ids_unique():
    session = Session()
    async with session.background():
        await execute(session, "sleep 0 & sleep 30 &")
        with anyio.fail_after(3):
            await session.wait_jobs(1)
        _, events = await execute(session, "jobs")
        assert [e.value for e in events if e.kind == "output"] == [
            "[1] done (0)  sleep 0",
            "[2] running  sleep 30",
        ]
        assert set(session.jobs) == {2}
        _, events = await execute(session, "jobs")
        assert [e.value for e in events if e.kind == "output"] == [
            "[2] running  sleep 30"
        ]
        await execute(session, "sleep 0 &")
        assert set(session.jobs) == {2, 3}
        await execute(session, "kill 2")
        with anyio.fail_after(3):
            await session.wait_jobs()
        _, events = await execute(session, "jobs")
        assert [e.value for e in events if e.kind == "output"] == [
            "[2] done (130)  sleep 30",
            "[3] done (0)  sleep 0",
        ]
        assert not session.jobs
        assert not [
            e for e in (await execute(session, "jobs"))[1] if e.kind == "output"
        ]
        await execute(session, "sleep 0 &")
        assert set(session.jobs) == {4}
