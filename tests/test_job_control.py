"""Cooperative job suspension, resumption, and foreground ownership."""

import anyio
import pytest

from anyshell import Command, MemoryFileSystem, Session
from anyshell.frontend import CommandInput, Shell, ShellApp


async def run(session, source):
    events = []

    async def emit(event):
        events.append(event)

    result = await session.execute(source, emit)
    return result, events


async def until(predicate):
    with anyio.fail_after(5):
        while not predicate():
            await anyio.sleep(0.001)


async def test_stop_resume_and_cancel_background_pipeline():
    session = Session()
    count = 0

    async def ticker(*, ctx, stdout):
        nonlocal count
        while True:
            await ctx.sleep(0.005)
            await stdout.send(count)
            count += 1

    session.register(Command("ticker", "Produce values", ticker))
    async with session.background():
        await run(session, "ticker | cat &")
        await until(lambda: count >= 2)
        assert (await run(session, "kill -STOP 1"))[0].status == 0
        frozen = count
        await anyio.sleep(0.04)
        assert count == frozen
        result, events = await run(session, "jobs")
        assert [e.value for e in events if e.kind == "output"] == [
            "[1] stopped  ticker | cat"
        ]
        assert (await run(session, "kill -CONT 1"))[0].status == 0
        await until(lambda: count > frozen)
        await run(session, "kill -STOP 1")
        await run(session, "kill 1")
        await session.wait_jobs(1)
        assert session.jobs[1].result.cancelled


async def test_sleep_preserves_remaining_time_while_stopped():
    session = Session()
    entered = anyio.Event()
    resumed_at = None
    finished_at = None

    async def sleeper(*, ctx):
        nonlocal finished_at
        entered.set()
        await ctx.sleep(0.15)
        finished_at = anyio.current_time()

    session.register(Command("sleeper", "A pause-aware wait", sleeper))
    async with session.background():
        await run(session, "sleeper &")
        await entered.wait()
        await run(session, "kill -STOP 1")
        await anyio.sleep(0.2)
        assert finished_at is None
        resumed_at = anyio.current_time()
        await run(session, "bg")
        await session.wait_jobs(1)
        assert finished_at - resumed_at >= 0.1


async def test_foreground_script_survives_suspend_and_resumes_after_other_inputs():
    session = Session()
    events = []

    async def emit(event):
        events.append(event)

    async with session.background():
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                session.execute,
                "for i in range(2):\n    sleep 0.1\n    echo {i}",
                emit,
            )
            await until(lambda: session._foreground_job is not None)
            assert session.suspend()
        assert session.last_status == 148
        assert session.jobs[1].stopped
        await run(session, "echo another input")
        result, _ = await run(session, "fg")
        assert result.status == 0
        assert [e.value for e in events if e.kind == "output"] == ["0", "1"]
        assert session.jobs[1].done.is_set()


async def test_ctrl_c_targets_foreground_job_and_ctrl_z_returns_it_to_jobs():
    session = Session()
    async with session.background():
        await run(session, "sleep 30 &")
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run, session, "fg 1")
            await until(lambda: session._foreground_job is session.jobs[1])
            assert session.suspend()
        assert session.last_status == 148
        assert session.jobs[1].stopped
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run, session, "fg 1")
            await until(lambda: session._foreground_job is session.jobs[1])
            assert session.cancel()
        assert session.last_status == 130
        assert session.jobs[1].result.cancelled


async def test_default_job_selection_and_invalid_requests():
    session = Session()
    async with session.background():
        for source in ("fg", "bg", "kill -STOP 99", "kill -CONT 99"):
            assert (await run(session, source))[0].status == 1
        await run(session, "sleep 30 & sleep 30 &")
        assert (await run(session, "bg 1"))[0].status == 1
        await run(session, "kill -STOP 1; kill -STOP 2")
        await run(session, "bg")
        assert not session.jobs[2].stopped and session.jobs[1].stopped
        assert (await run(session, "kill -STOP -CONT 1"))[0].status == 1
        await run(session, "bg")
        assert not session.jobs[1].stopped


async def test_background_fg_cannot_take_over_the_prompt():
    session = Session()
    async with session.background():
        await run(session, "sleep 30 &")
        await run(session, "fg 1 &")
        await session.wait_jobs(2)
        assert session.jobs[2].result.status == 1
        assert not session.jobs[1].foreground
        assert session._foreground_job is None
        assert not session.running


async def test_fg_keeps_background_directory_isolated():
    session = Session(MemoryFileSystem({"first/file": "", "second/file": ""}))
    release = anyio.Event()

    async def hold():
        await release.wait()

    session.register(Command("hold", "Wait for release", hold))
    async with session.background():
        await run(session, "cd first; hold & cd /second")
        assert session.cwd == "/second"
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run, session, "fg 1")
            await until(lambda: session._foreground_job is session.jobs[1])
            release.set()
        assert session.last_status == 0
        assert session.cwd == "/second"


async def test_python_only_loop_can_be_stopped_and_cancelled():
    session = Session()
    async with session.background():
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run, session, "i = 0\nwhile True:\n    i += 1")
            await until(lambda: session.variables.get("i", 0) > 5)
            session.suspend()
        frozen = session.variables["i"]
        await anyio.sleep(0.02)
        assert session.variables["i"] == frozen
        await run(session, "bg 1")
        await until(lambda: session.variables["i"] > frozen)
        await run(session, "kill 1")
        await session.wait_jobs(1)
        assert session.jobs[1].result.cancelled


async def test_stopped_command_waits_at_completion_checkpoint():
    session = Session()
    entered = anyio.Event()
    release = anyio.Event()

    async def blocking():
        entered.set()
        await release.wait()

    session.register(Command("blocking", "Wait for an external event", blocking))
    async with session.background():
        await run(session, "blocking &")
        await entered.wait()
        await run(session, "kill -STOP 1")
        release.set()
        await anyio.sleep(0.02)
        assert not session.jobs[1].done.is_set()
        await run(session, "bg 1")
        await session.wait_jobs(1)
        assert session.jobs[1].result.status == 0


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_frontend_ctrl_z_restores_prompt_and_fg_can_be_cancelled(prompt_position):
    session = Session()
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test() as pilot:
        editor = app.query_one(CommandInput)
        shell = app.query_one(Shell)
        editor.value = "sleep 30"
        await pilot.press("enter")
        await until(lambda: session.running)
        await pilot.press("ctrl+z")
        await until(lambda: not shell._busy)
        assert session.jobs[1].stopped
        await pilot.pause()
        assert editor.has_focus
        editor.value = "fg"
        await pilot.press("enter")
        await until(lambda: session._foreground_job is session.jobs[1])
        await pilot.press("ctrl+c")
        await until(lambda: not shell._busy)
        assert session.last_status == 130
