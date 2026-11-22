from typing import Annotated

import pytest
from cyclopts import App, Parameter

from anyshell import Command, Context, Session


@pytest.fixture
def tree():
    app = App(name="tools", help="Shared tools", version="1.2")
    kernel = App(name="kernel", help="Manage kernels")
    app.command(kernel)
    calls = []

    @app.command
    async def show(
        path: str,
        *,
        count: Annotated[int, Parameter(name=["--count", "-n"])] = 1,
        ctx: Annotated[Context, Parameter(parse=False)],
        stdout: Annotated[object, Parameter(parse=False)],
    ):
        calls.append(path)
        for _ in range(count):
            await stdout.send(ctx.resolve(path))

    @kernel.command(alias="ls")
    def list():
        return {"kernel": "python3"}

    @kernel.command
    async def start(name: str, *, quiet: bool = False):
        return {"name": name, "quiet": quiet}

    session = Session()
    session.register(Command("tools", "Shared tools", app))
    return session, app, calls


async def execute(session, source):
    events = []

    async def emit(event):
        events.append(event)

    result = await session.execute(source, emit)
    return (
        result.status,
        [event.value for event in events if event.kind == "output"],
        events,
    )


async def test_nested_commands_async_injection_and_pipelines(tree):
    session, _, calls = tree
    status, output, _ = await execute(session, "tools show note.txt -n 2 | count")
    assert status == 0 and output == [2]
    assert calls == ["note.txt"]
    assert (await execute(session, "tools kernel list | select kernel"))[1] == [
        "python3"
    ]
    assert (await execute(session, "tools kernel ls"))[1] == [{"kernel": "python3"}]
    await execute(session, "data = tools kernel start python3 --quiet")
    assert session.variables["data"] == [{"name": "python3", "quiet": True}]


@pytest.mark.parametrize(
    "source",
    [
        "tools --help",
        "tools kernel --help",
        "tools show --help",
        "help tools",
        "help tools kernel start",
    ],
)
async def test_help_is_captured_without_running_commands(tree, source, capsys):
    session, _, calls = tree
    status, output, _ = await execute(session, source)
    assert status == 0 and output
    text = "\n".join(output)
    assert "tools" in text
    assert "--ctx" not in text and "--stdout" not in text
    assert calls == []
    assert capsys.readouterr().out == ""


async def test_invalid_subcommand_arguments_preflight_whole_pipeline(tree):
    session, _, calls = tree
    status, _, events = await execute(
        session, "tools show first | tools show second -n invalid"
    )
    assert status == 1
    assert calls == []
    assert any(event.kind == "error" for event in events)


async def test_completes_nested_commands_aliases_and_options(tree):
    session, _, _ = tree
    assert await session.complete("tools k") == ["tools kernel "]
    assert await session.complete("tools kernel ") == [
        "tools kernel list ",
        "tools kernel ls ",
        "tools kernel start ",
    ]
    assert await session.complete("tools kernel start --q") == [
        "tools kernel start --quiet"
    ]
    assert await session.complete("tools show --c") == ["tools show --count"]
    assert "tools show --stdout" not in await session.complete("tools show --")


async def test_registration_preserves_standalone_app(tree):
    session, app, _ = tree
    before = app.default_parameter
    callback, bound, ignored = app.parse_args(["kernel", "list"])
    assert callback(*bound.args, **bound.kwargs) == {"kernel": "python3"}
    assert ignored == {}
    await execute(session, "tools show note.txt")
    assert app.default_parameter is before
    assert app.name == ("tools",)


async def test_version_is_captured(tree, capsys):
    session, _, _ = tree
    status, output, _ = await execute(session, "tools --version")
    assert status == 0
    assert output == ["1.2"]
    assert capsys.readouterr().out == ""


async def test_app_default_handler_is_awaited():
    app = App(name="defaulted")

    @app.default
    async def default(value: int = 42):
        return value

    session = Session()
    session.register(Command("defaulted", "Default handler", app))
    assert (await execute(session, "defaulted"))[1] == [42]
    assert (await execute(session, "defaulted 7"))[1] == [7]


@pytest.mark.parametrize("source", ["tools show --help", "help tools show"])
async def test_terminal_help_is_styled_but_pipes_captures_and_files_are_plain(
    tree, source, capsys
):
    from rich.text import Text

    session, _, calls = tree
    events = []

    async def emit(event):
        events.append(event)

    result = await session.execute(source, emit, is_terminal=True)
    output = [event.value for event in events if event.kind == "output"]
    assert result.status == 0
    assert output and all(isinstance(value, Text) for value in output)
    assert any(value.spans for value in output)
    assert "--count" in "\n".join(value.plain for value in output)
    events.clear()
    await session.execute(f"data = {source}", emit, is_terminal=True)
    assert all(isinstance(value, str) for value in session.variables["data"])
    assert "\x1b" not in "\n".join(session.variables["data"])
    events.clear()
    await session.execute(f"{source} | cat", emit, is_terminal=True)
    assert all(
        isinstance(event.value, str) for event in events if event.kind == "output"
    )
    await session.execute(f"{source} > help.txt", emit, is_terminal=True)
    assert "--count" in session.filesystem.read_text("/help.txt")
    assert "\x1b" not in session.filesystem.read_text("/help.txt")
    assert calls == []
    assert capsys.readouterr().out == ""
