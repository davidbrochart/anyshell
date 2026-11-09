"""The pager reads files and Python pipelines inside either Shell layout."""

import anyio
import pytest
from anytext.widgets import RichLog

from anyshell import Command, LocalFileSystem, MemoryFileSystem, Session
from anyshell.frontend import CommandInput, Shell, ShellApp
from anyshell.pager import Pager, PagerLog


async def wait_until(predicate):
    with anyio.fail_after(5):
        while not predicate():
            await anyio.sleep(0.01)


@pytest.fixture(params=["memory", "local"])
def filesystem(request, tmp_path):
    fs = MemoryFileSystem() if request.param == "memory" else LocalFileSystem(tmp_path)
    fs.write_text("/long.txt", "\n".join(f"line {i}" for i in range(120)))
    fs.write_text("/other.txt", "[red] literal\nlast\n")
    return fs


async def open_pager(app, pilot, source):
    app.query_one(CommandInput).value = source
    await pilot.press("enter")
    await wait_until(lambda: bool(app.query(Pager)))
    pager = app.query_one(Pager)
    await wait_until(lambda: pager.loaded or pager.error is not None)
    assert pager.query_one(PagerLog).scrollable_content_region.height > 0
    return pager


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_more_files_navigation_and_restoration(filesystem, prompt_position):
    session = Session(filesystem)
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test(size=(70, 20)) as pilot:
        shell = app.query_one(Shell)
        transcript = app.query_one(RichLog)
        pager = await open_pager(app, pilot, "more long.txt other.txt")
        log = pager.query_one(PagerLog)
        assert pager.region == shell.content_region
        assert log.size.height > 5
        assert log.max_scroll_y > 0
        assert log.scroll_y == 0
        assert len(log.lines) == 122
        assert log.lines[120].text.strip() == "[red] literal"
        assert session.running
        await pilot.press("space")
        assert log.scroll_y > 0
        await pilot.press("b")
        assert log.scroll_y == 0
        await pilot.press("enter")
        assert log.scroll_y == 1
        await pilot.press("up")
        assert log.scroll_y == 0
        await pilot.press("end")
        assert log.is_vertical_scroll_end
        await pilot.press("home")
        assert log.scroll_y == 0
        await pilot.press("q")
        await wait_until(lambda: not shell._busy)
        assert not app.query(Pager)
        assert session.last_status == 0 and app.is_running
        assert app.query_one(CommandInput).has_focus
        assert app.query_one(RichLog) is transcript
        assert not any("line 119" in line.text for line in transcript.lines)


async def test_more_pipeline_structured_values_and_empty_input(filesystem):
    session = Session(filesystem)
    app = ShellApp(session)
    async with app.run_test() as pilot:
        pager = await open_pager(app, pilot, "cat other.txt | more")
        assert [line.text.strip() for line in pager.query_one(PagerLog).lines] == [
            "[red] literal",
            "last",
        ]
        await pilot.press("ctrl+q")
        await wait_until(lambda: not app.query_one(Shell)._busy)
        pager = await open_pager(app, pilot, "ls | more")
        assert any("long.txt" in line.text for line in pager.query_one(PagerLog).lines)
        await pilot.press("escape")
        await wait_until(lambda: not app.query_one(Shell)._busy)
        pager = await open_pager(app, pilot, "more")
        assert not pager.query_one(PagerLog).lines
        await pilot.press("q")
        await wait_until(lambda: not app.query_one(Shell)._busy)


async def test_more_reports_read_errors(filesystem):
    app = ShellApp(Session(filesystem))
    async with app.run_test() as pilot:
        for path in ("missing", "/"):
            app.query_one(CommandInput).value = f"more {path}"
            await pilot.press("enter")
            await wait_until(lambda: not app.query_one(Shell)._busy)
            assert not app.query(Pager)
            assert app.query_one(Shell).session.last_status == 1
            assert any(e.kind == "error" for e in app.query_one(Shell).output)


@pytest.mark.parametrize("command", ["more", "less"])
async def test_more_quitting_stops_streaming_pipeline(command):
    finished = anyio.Event()

    async def endless(*, stdout):
        try:
            while True:
                await stdout.send("streamed line")
        finally:
            finished.set()

    session = Session()
    session.register(Command("endless", "Stream until closed", endless))
    app = ShellApp(session)
    async with app.run_test() as pilot:
        app.query_one(CommandInput).value = f"endless | {command}"
        await pilot.press("enter")
        await wait_until(lambda: bool(app.query(Pager)))
        await pilot.press("q")
        await wait_until(lambda: not app.query_one(Shell)._busy)
        assert finished.is_set()
        assert session.last_status == 0
        await session.execute("echo recovered", app.query_one(Shell).receive)


@pytest.mark.parametrize("command", ["more", "less"])
async def test_more_requires_interactive_frontend(command):
    session = Session()
    events = []

    async def emit(event):
        events.append(event)

    result = await session.execute(f"{command} missing.txt", emit)
    assert result.status == 1
    assert any(e.kind == "error" and "interactive frontend" in e.value for e in events)


async def test_pager_closes_on_cancellation_and_shutdown():
    session = Session(MemoryFileSystem({"file.txt": "hello"}))
    app = ShellApp(session)
    async with app.run_test() as pilot:
        await open_pager(app, pilot, "more file.txt")
        assert session.cancel()
        await wait_until(lambda: not app.query_one(Shell)._busy)
        assert not app.query(Pager)
        assert session.last_status == 130
        await open_pager(app, pilot, "more file.txt")


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_less_search_and_repeat_matches(filesystem, prompt_position):
    from anyshell.pager import PagerSearchInput

    app = ShellApp(Session(filesystem), prompt_position=prompt_position)
    async with app.run_test(size=(70, 20)) as pilot:
        pager = await open_pager(app, pilot, "less long.txt")
        log = pager.query_one(PagerLog)
        assert not pager.query_one("#pager-search-row").display
        await pilot.press("slash")
        field = pager.query_one(PagerSearchInput)
        assert field.has_focus
        field.value = r"^line (30|60)$"
        await pilot.press("enter")
        assert pager._match_line == 30
        assert log.scroll_y == 30
        assert not pager.query_one("#pager-search-row").display
        await pilot.press("n")
        assert pager._match_line == 60
        await pilot.press("N")
        assert pager._match_line == 30
        await pilot.press("N")
        assert pager._match_line == 60  # Search wraps around the text.
        await pilot.press("question_mark")
        field.value = r"^line (10|20)$"
        await pilot.press("enter")
        assert pager._match_line == 20
        await pilot.press("n")
        assert pager._match_line == 10
        await pilot.press("N")
        assert pager._match_line == 20
        await pilot.press("slash", "q", "space", "b", "n")
        assert field.value == "q bn"  # Pager shortcuts are text while searching.
        await pilot.press("escape")
        assert pager.has_focus and not pager._searching
        await pilot.press("q")
        await wait_until(lambda: not app.query_one(Shell)._busy)
        assert app.is_running


async def test_less_pipeline_and_search_errors(filesystem, monkeypatch):
    from anyshell.pager import PagerSearchInput

    app = ShellApp(Session(filesystem))
    notices = []
    monkeypatch.setattr(
        app, "notify", lambda message, **kwargs: notices.append((message, kwargs))
    )
    async with app.run_test() as pilot:
        pager = await open_pager(app, pilot, "cat other.txt | less")
        assert pager.request.searchable
        await pilot.press("slash")
        field = pager.query_one(PagerSearchInput)
        field.value = "["
        await pilot.press("enter")
        assert pager._searching and field.has_focus
        assert notices[-1][1]["title"] == "Invalid search"
        field.value = "no match"
        await pilot.press("enter")
        assert not pager._searching and pager.has_focus
        assert notices[-1][0] == "Pattern not found"
        await pilot.press("slash")
        field.value = "last"
        await pilot.press("enter")
        assert pager._match_line == 1
        await pilot.press("slash", "enter")
        assert pager._match_line == 1  # Empty search reuses the last expression.
        await pilot.press("ctrl+q")
        await wait_until(lambda: not app.query_one(Shell)._busy)
        assert app.query_one(Shell).session.last_status == 0
