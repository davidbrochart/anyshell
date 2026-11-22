from contextlib import asynccontextmanager

import anyio
import pytest
from anytext.widget import Widget
from anytext.widgets import Footer
from anytext.widgets._footer import FooterKey

from anyshell import Command, Session
from anyshell.frontend import CommandInput, Shell, ShellApp, ShellFooter


class DemoView(Widget, can_focus=True):
    BINDINGS = [("q", "close", "Close view")]

    def __init__(self):
        super().__init__()
        self.closed = anyio.Event()

    def on_mount(self):
        self.focus()

    def action_close(self):
        self.closed.set()


def register(session, lifecycle, *, fail=False, view_type=DemoView):
    @asynccontextmanager
    async def open_view(host):
        lifecycle.append("start")
        try:
            if fail:
                raise ValueError("startup failed")
            view = view_type()
            yield view
            assert view.parent is None  # Remove the UI before stopping services.
        finally:
            lifecycle.append("stop")

    async def demo(*, ctx):
        await ctx.open_view(open_view)

    session.register(Command("demo", "Open a view", demo))


async def wait_for(check):
    with anyio.fail_after(5):
        while not check():
            await anyio.sleep(0.01)


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_view_lifetime_and_prompt_restoration(prompt_position, cancel):
    lifecycle = []
    session = Session()
    register(session, lifecycle)
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test() as pilot:
        shell = app.query_one(Shell)
        editor = shell.query_one(CommandInput)
        editor.value = "demo"
        await pilot.press("enter")
        await wait_for(lambda: bool(shell.query(DemoView)))
        assert not shell.query_one("#shell-terminal").display
        if cancel:
            shell.cancel()
        else:
            await pilot.press("q")
        await wait_for(lambda: not shell._busy)
        assert not shell.query(DemoView)
        assert shell.query_one("#shell-terminal").display
        await wait_for(lambda: editor.has_focus)
        assert lifecycle == ["start", "stop"]
        assert session.last_status == (130 if cancel else 0)
        assert app.is_running


async def test_view_startup_failure_keeps_shell_available():
    lifecycle = []
    session = Session()
    register(session, lifecycle, fail=True)
    app = ShellApp(session)
    async with app.run_test() as pilot:
        shell = app.query_one(Shell)
        editor = shell.query_one(CommandInput)
        editor.value = "demo"
        await pilot.press("enter")
        await wait_for(lambda: not shell._busy)
        assert session.last_status == 1
        assert editor.has_focus
        assert lifecycle == ["start", "stop"]


@pytest.mark.parametrize("view_footer", [False, True])
async def test_host_footer_keeps_host_shortcuts_when_view_has_its_own(view_footer):
    class FooteredView(DemoView):
        def compose(self):
            if view_footer:
                yield Footer()

    session = Session()
    register(session, [], view_type=FooteredView)
    app = ShellApp(session)
    async with app.run_test() as pilot:
        editor = app.query_one(CommandInput)
        editor.value = "demo"
        await pilot.press("enter")
        await wait_for(lambda: bool(app.query(FooteredView)))
        await pilot.pause()
        global_footer = app.query_one(ShellFooter)
        assert global_footer.display
        keys = {key.key for key in global_footer.query(FooterKey)}
        assert "alt+v" in keys
        assert ("q" in keys) == (not view_footer)
        if view_footer:
            local_footer = app.query_one(FooteredView).query_one(Footer)
            assert local_footer.display
            assert "q" in {key.key for key in local_footer.query(FooterKey)}
        await pilot.press("q")


async def test_closing_pane_stops_its_view_without_exiting_other_panes():
    lifecycle = []
    session = Session()
    register(session, lifecycle)
    app = ShellApp(session)
    async with app.run_test() as pilot:
        workspace = app.query_one("ShellWorkspace")
        original = workspace.active
        await pilot.press("alt+v")
        closing = workspace.active
        closing.query_one(CommandInput).value = "demo"
        await pilot.press("enter")
        await wait_for(lambda: bool(closing.query(DemoView)))
        await pilot.press("alt+w")
        await wait_for(lambda: not closing._busy)
        assert workspace.panes == [original]
        await wait_for(lambda: original.query_one(CommandInput).has_focus)
        assert lifecycle == ["start", "stop"]
        assert app.is_running


@pytest.mark.parametrize(
    "source", ["demo", "data = demo", "demo | cat", "demo > result", "demo &"]
)
async def test_views_rejected_without_an_interactive_foreground_frontend(source):
    lifecycle = []
    session = Session()
    register(session, lifecycle)

    async def emit(event):
        pass

    async with session.background():
        result = await session.execute(source, emit)
        if source.endswith("&"):
            assert await session.wait_jobs() == 1
        else:
            assert result.status == 1
    assert lifecycle == []
