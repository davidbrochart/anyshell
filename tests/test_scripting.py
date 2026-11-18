import anyio
import pytest

from anyshell import Command, LocalFileSystem, MemoryFileSystem, Session
from anyshell.engine import is_complete


async def execute(session, source):
    events = []

    async def emit(event):
        events.append(event)

    result = await session.execute(source, emit)
    return (
        result,
        [event.value for event in events if event.kind == "output"],
        [event.value for event in events if event.kind == "error"],
    )


@pytest.fixture(params=["memory", "local"])
def session(request, tmp_path):
    files = {"docs/a.txt": "one\ntwo\n", "docs/b b.txt": "three\n"}
    if request.param == "memory":
        return Session(MemoryFileSystem(files))
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return Session(LocalFileSystem(tmp_path))


async def test_variables_expressions_and_capture_persist(session):
    source = """directory = "docs"
names = ["a.txt", "b b.txt"]
for name in names:
    data = cat {f"{directory}/{name}"}
    echo {data[0].upper()}
"""
    result, output, errors = await execute(session, source)
    assert result.status == 0 and not errors
    assert output == ["ONE", "THREE"]
    assert session.variables["data"] == ["three"]
    assert session.variables["name"] == "b b.txt"
    assert session.history == [source]
    assert "__anyshell__" not in session.variables
    assert (await execute(session, "data"))[1] == [["three"]]
    assert (await execute(session, 'text = "ls"; echo {text}'))[1] == ["ls"]
    result, _, _ = await execute(Session(), "echo {directory}")
    assert result.status == 1  # Each pane/session has its own namespace.


async def test_capture_retains_structured_pipeline_values(session):
    result, output, errors = await execute(
        session, "entries = ls docs | where type file"
    )
    assert result.status == 0 and output == [] and errors == []
    entries = session.variables["entries"]
    assert {entry["name"] for entry in entries} == {"a.txt", "b b.txt"}
    assert all(entry["type"] == "file" for entry in entries)
    assert (await execute(session, 'names = [entry["name"] for entry in entries]'))[
        0
    ].status == 0
    assert set(session.variables["names"]) == {"a.txt", "b b.txt"}
    marker = object()

    async def objects(*, stdout):
        await stdout.send(marker)

    session.register(Command("objects", "Produce an opaque Python value", objects))
    assert (await execute(session, "values = objects"))[0].status == 0
    assert session.variables["values"][0] is marker


async def test_nested_control_flow_and_loop_else(session):
    source = """seen = []
for outer in range(3):
    i = 0
    while i < 3:
        i += 1
        if i == 2:
            continue
        elif outer == 2:
            break
        seen.append((outer, i))
        echo {f"{outer}:{i}"}
    else:
        echo inner-done
else:
    echo outer-done
"""
    result, output, errors = await execute(session, source)
    assert result.status == 0 and not errors
    assert output == [
        "0:1",
        "0:3",
        "inner-done",
        "1:1",
        "1:3",
        "inner-done",
        "outer-done",
    ]
    assert session.variables["seen"] == [(0, 1), (0, 3), (1, 1), (1, 3)]
    assert (await execute(session, "for i in range(3): echo {i}"))[1] == ["0", "1", "2"]
    assert (await execute(session, "if False: sleep invalid"))[0].status == 0


@pytest.mark.parametrize(
    "source, expected",
    [
        ('echo {{"a": {"b": "nested"}}["a"]["b"]}', "nested"),
        ('echo {"| ; & < >"} | cat', "| ; & < >"),
        ('echo {f"{{{1 + 2}}}"}', "{3}"),
        ("echo '{not Python}'", "{not Python}"),
        (r"echo \{literal\}", "{literal}"),
        ('echo "before {1 + 2} after"', "before 3 after"),
        ('echo {"a b"}', "a b"),
        ('echo {"a\\nb"}', "a\nb"),
        ('echo {"quoted \\"value\\""}', 'quoted "value"'),
        ('echo {("first", "second")[1]}', "second"),
    ],
)
async def test_python_arguments_preserve_quoting_and_shell_boundaries(source, expected):
    result, output, errors = await execute(Session(), source)
    assert result.status == 0, errors
    assert output == [expected]


async def test_python_arguments_are_single_arguments_and_redirection_paths(session):
    calls = []

    async def record(*words: str):
        calls.append(words)

    session.register(Command("record", "Record exact arguments", record))
    result, _, errors = await execute(
        session,
        'value = "a b; echo injected | count & >out"\nrecord {value} ""',
    )
    assert result.status == 0 and not errors
    assert calls == [("a b; echo injected | count & >out", "")]
    with pytest.raises(FileNotFoundError):
        session.filesystem.stat("/out")
    result, output, errors = await execute(
        session,
        """for name in ["a b.txt", "semi;.txt"]:
    echo {name.upper()} > {name}
    lines = cat < {name}
    echo {lines[0]}
""",
    )
    assert result.status == 0 and not errors
    assert output == ["A B.TXT", "SEMI;.TXT"]
    assert session.filesystem.read_text("/a b.txt") == "A B.TXT\n"


async def test_failed_capture_does_not_assign_partial_output(session):
    result, output, errors = await execute(
        session, 'data = ["keep"]; data = cat absent | count'
    )
    assert result.status == 1 and output == []
    assert len(errors) == 1 and errors[0].startswith("cat:")
    assert session.variables["data"] == ["keep"]
    assert (await execute(session, "echo recovered"))[1] == ["recovered"]


@pytest.mark.parametrize(
    "source",
    [
        "ls = []",
        "ls: list = []",
        "ls += []",
        "for ls in [1]: pass",
        "values = [ls for ls in [1]]",
        "import math as ls",
        "from math import sqrt as ls",
        "def ls(): pass",
        "def fn(ls): pass",
        "class ls: pass",
        "echo {(ls := 1)}",
        "del ls",
        "try:\n    raise ValueError()\nexcept Exception as ls:\n    pass",
        "match 1:\n    case ls:\n        pass",
        "globals().update(ls=1)",
        "globals()['ls'] = 1",
    ],
)
async def test_command_names_are_protected_in_python_bindings(source):
    session = Session()
    result, _, errors = await execute(session, source)
    assert result.status == 1
    assert any("reserved command name" in error for error in errors), errors
    assert "ls" not in session.variables
    assert (await execute(session, "echo recovered"))[1] == ["recovered"]


async def test_registration_rejects_variable_conflicts_and_star_imports_are_checked():
    session = Session()
    await execute(session, "custom = 1")
    with pytest.raises(ValueError, match="conflicts with variable"):
        session.register(Command("custom", "Conflict", lambda: None))
    session.register(Command("sqrt", "Protected import target", lambda: None))
    result, _, errors = await execute(session, "from math import *")
    assert result.status == 1 and "reserved command name" in errors[0]
    assert "sqrt" not in session.variables


async def test_background_loop_arguments_and_namespaces_are_snapshots():
    session = Session()
    events = []

    async def report(value: str, *, ctx, stdout):
        await anyio.sleep(0)
        await stdout.send((value, ctx.session.variables["i"]))

    async def emit(event):
        events.append(event)

    session.register(Command("report", "Inspect a job's variable snapshot", report))
    async with session.background():
        result = await session.execute("for i in range(3): report {i} &", emit)
        assert result.status == 0
        await session.wait_jobs()
        assert sorted(event.value for event in events if event.kind == "output") == [
            ("0", 0),
            ("1", 1),
            ("2", 2),
        ]
        assert all("__anyshell" not in job.source for job in session.jobs.values())
        result, _, errors = await execute(session, "data = echo bad &")
        assert result.status == 1 and "cannot capture background" in errors[0]
        assert "data" not in session.variables


@pytest.mark.parametrize(
    "loop",
    [
        "for i in range(1000000):\n    pass",
        "i = 0\nwhile i < 1000000:\n    i += 1",
    ],
)
async def test_python_only_loops_yield_and_can_be_cancelled(loop):
    session = Session()
    results = []
    finished = anyio.Event()

    async def run():
        results.append(await execute(session, loop))
        finished.set()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(run)
        await anyio.sleep(0.01)
        assert session.running
        assert session.cancel()
        with anyio.fail_after(2):
            await finished.wait()
    assert results[0][0].cancelled and results[0][0].status == 130
    assert not session.running
    assert (await execute(session, "echo recovered"))[1] == ["recovered"]


async def test_exit_stops_the_whole_script():
    session = Session()
    result, output, errors = await execute(
        session, "for i in range(3):\n    echo {i}\n    exit 7\n    echo unreachable"
    )
    assert result.status == 7 and output == ["0"] and not errors
    assert session.exit_status == 7


async def test_async_functions_use_the_current_execution_context():
    session = Session()
    assert (await execute(session, "async def greet(name):\n    echo {name}"))[
        0
    ].status == 0
    assert (await execute(session, 'await greet("later")'))[1] == ["later"]
    assert (await execute(session, 'await greet("again")'))[1] == ["again"]


@pytest.mark.parametrize(
    "source, complete",
    [
        ("for i in range(3):", False),
        ("while True:", False),
        ("if True:\n    echo yes", False),
        ("if True:\n    echo yes\n    ", True),
        ("for i in range(3): echo {i}", True),
        ("echo {1 +", False),
        ('echo {{"key": [1, 2]}', False),
        ("echo {1 + }", True),
        ("values = [1,", False),
        ("values = [1,\n2]", True),
        ("if True:\n    echo yes\nelse:", False),
    ],
)
def test_scripting_completeness(source, complete):
    assert is_complete(source) is complete


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_frontend_indents_and_submits_python_blocks(prompt_position):
    from anyshell.frontend import CommandInput, Shell, ShellApp

    session = Session()
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test() as pilot:
        editor = app.query_one(CommandInput)
        editor.value = "for i in range(2):"
        await pilot.press("enter")
        assert editor.value == "for i in range(2):\n    "
        assert session.history == []
        editor.value += "echo {i}"
        await pilot.press("enter")
        assert editor.value.endswith("echo {i}\n    ")
        assert session.history == []
        await pilot.press("enter")
        with anyio.fail_after(5):
            while app.query_one(Shell)._busy:
                await anyio.sleep(0.01)
        assert [
            event.value
            for event in app.query_one(Shell).output
            if event.kind == "output"
        ] == ["0", "1"]
        assert editor.value == ""


async def test_completion_in_capture_assignments_and_indented_commands(session):
    assert await session.complete("entries = ec") == ["entries = echo "]
    assert await session.complete("data = cat docs/a") == ["data = cat docs/a.txt"]
    assert await session.complete("for i in range(3):\n    ec") == [
        "for i in range(3):\n    echo "
    ]


async def test_python_syntax_errors_and_multiline_expressions(session):
    result, output, errors = await execute(session, "value = 1 +")
    assert result.status == 1 and not output
    assert "invalid syntax" in errors[0]
    assert "value" not in session.variables
    result, output, errors = await execute(
        session, "values = [1,\n    2]\necho {sum(\n    values\n)}"
    )
    assert result.status == 0 and output == ["3"] and not errors
