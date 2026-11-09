import sys
from pathlib import Path

import anyio
import pytest

from anyshell import Command, LocalFileSystem, MemoryFileSystem, Session, ShellError
from anyshell.engine import parse_pipeline


async def execute(session, source):
    events = []

    async def emit(event):
        events.append(event)

    result = await session.execute(source, emit)
    return result, [e.value for e in events if e.kind == "output"], events


@pytest.fixture(params=["memory", "local"])
def filesystem(request, tmp_path):
    if request.param == "memory":
        return MemoryFileSystem(
            {"notes.txt": "zebra\napple\napricot\n", "docs/hello.txt": "hello\n"}
        )
    (tmp_path / "notes.txt").write_text("zebra\napple\napricot\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/hello.txt").write_text("hello\n")
    return LocalFileSystem(tmp_path)


async def test_text_and_structured_pipelines(filesystem):
    session = Session(filesystem)
    result, values, _ = await execute(session, "cat notes.txt | grep '^a' | sort")
    assert result.status == 0
    assert values == ["apple", "apricot"]
    _, values, _ = await execute(session, "ls | where type file | select name | sort")
    assert values == ["notes.txt"]
    _, values, _ = await execute(session, "cat notes.txt | count")
    assert values == [3]


async def test_navigation_and_mutation(filesystem):
    session = Session(filesystem)
    original_cwd = Path.cwd()
    assert (await execute(session, "cd docs"))[0].status == 0
    assert session.cwd == "/docs"
    assert Path.cwd() == original_cwd
    assert (await execute(session, "pwd"))[1] == ["/docs"]
    assert (await execute(session, "write greeting.txt 'hello world'"))[0].status == 0
    assert (await execute(session, "cat greeting.txt"))[1] == ["hello world"]
    await execute(session, "rm greeting.txt")
    assert (await execute(session, "cat greeting.txt"))[0].status == 1
    await execute(session, "mkdir nested")
    assert filesystem.is_dir("/docs/nested")
    result, _, events = await execute(session, "rm nested")
    assert result.status == 1
    assert [event.value for event in events if event.kind == "error"] == [
        "rm: 'nested' is a directory; use rmdir to remove an empty directory"
    ]
    assert filesystem.is_dir("/docs/nested")
    assert (await execute(session, "rm /docs"))[0].status == 1
    assert filesystem.is_dir("/docs")
    assert (await execute(session, "rmdir nested"))[0].status == 0
    assert not filesystem.is_dir("/docs/nested")
    await execute(session, "cd /")
    assert session.cwd == "/"


async def test_errors_and_recovery(filesystem):
    session = Session(filesystem)
    for command in (
        "missing",
        "echo a |",
        "cat absent | count",
        "cd absent",
        "echo a | cd docs",
        "grep '['",
    ):
        result, _, events = await execute(session, command)
        assert result.status == 1
        assert any(event.kind == "error" for event in events)
        assert not session.running
    assert (await execute(session, "echo recovered"))[1] == ["recovered"]


async def test_completion_and_history(filesystem):
    session = Session(filesystem)
    assert await session.complete("ec") == ["echo"]
    assert await session.complete("cat not") == ["cat notes.txt"]
    assert await session.complete("cd do") == ["cd docs/"]
    assert await session.complete("cd ") == ["cd docs/"]
    assert await session.complete("cd not") == []
    assert await session.complete("cd docs/") == []
    assert await session.complete("cat docs/") == ["cat docs/hello.txt"]
    filesystem.mkdir("/docs/nested")
    assert await session.complete("cd docs/") == ["cd docs/nested/"]
    await execute(session, "cd docs")
    assert await session.complete("cd ") == ["cd nested/"]
    assert await session.complete("cd /not") == []
    await execute(session, "cd /")
    session.history.clear()
    assert await session.complete("cat notes.txt | co") == ["cat notes.txt | count"]
    await execute(session, "echo first")
    await execute(session, "echo first")
    await execute(session, "pwd")
    assert session.history == ["echo first", "pwd"]


async def test_cancel_busy_and_custom_commands():
    session = Session()
    started = anyio.Event()
    finished = anyio.Event()
    events = []
    results = []

    async def hold(*, ctx, stdin, stdout):
        started.set()
        await anyio.sleep_forever()

    async def emit(event):
        events.append(event)

    async def run():
        results.append(await session.execute("hold | count", emit))
        finished.set()

    session.register(Command("hold", "A cancellable Python command", hold))
    async with anyio.create_task_group() as group:
        group.start_soon(run)
        with anyio.fail_after(3):
            await started.wait()
            with pytest.raises(ShellError, match="already running"):
                await session.execute("echo overlap", emit)
            assert session.cancel()
            await finished.wait()
    assert results[0].cancelled
    assert results[0].status == 130
    assert not session.running
    assert (await execute(session, "echo after"))[1] == ["after"]


async def test_stream_backpressure_and_early_consumer():
    session = Session()
    sent = []

    async def produce(*, ctx, stdin, stdout):
        for number in range(1000):
            await stdout.send(number)
            sent.append(number)

    async def take_one(*, ctx, stdin, stdout):
        await stdout.send(await stdin.receive())

    session.register(Command("produce", "Produce values", produce))
    session.register(Command("one", "Take a value", take_one))
    with anyio.fail_after(3):
        result, values, _ = await execute(session, "produce | one")
    assert result.status == 0
    assert values == [0]
    assert len(sent) < 1000


async def test_engine_does_not_import_anytext_or_use_workers(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Engine attempted to start a thread")

    monkeypatch.setattr(anyio.to_thread, "run_sync", forbidden)
    before = set(sys.modules)
    assert (await execute(Session(), "echo browser | cat | count"))[1] == [1]
    assert not any(name.startswith("anytext") for name in set(sys.modules) - before)


def test_quoting_and_parser_errors():
    assert parse_pipeline("echo 'a|b' | grep 'a|b'") == [
        ["echo", "a|b"],
        ["grep", "a|b"],
    ]
    assert parse_pipeline(r"echo a\|b") == [["echo", "a|b"]]
    with pytest.raises(ShellError):
        parse_pipeline("echo 'unfinished")
    with pytest.raises(ShellError):
        parse_pipeline("echo a || cat")


def test_local_filesystem_workspace_boundary(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    fs = LocalFileSystem(root)
    with pytest.raises(PermissionError):
        fs.write_text("/../outside", "no")
    with pytest.raises(PermissionError):
        fs.remove("/")
    assert not (tmp_path / "outside").exists()


async def test_session_stays_busy_until_status_delivery_finishes():
    session = Session()
    blocked = anyio.Event()
    finish = anyio.Event()

    async def emit(event):
        if event.kind == "status":
            blocked.set()
            await finish.wait()

    async with anyio.create_task_group() as group:
        group.start_soon(session.execute, "echo first", emit)
        with anyio.fail_after(3):
            await blocked.wait()
            assert session.running
            with pytest.raises(ShellError):
                await session.execute("echo second", emit)
            finish.set()
    assert not session.running


async def test_newline_behavior_matches_filesystems(filesystem):
    filesystem.write_text("/unicode.txt", "a\r\nb\rc\nd\u2028e\n")
    assert list(filesystem.read_lines("/unicode.txt")) == ["a", "b", "c", "d\u2028e"]


def test_remove_symlink_preserves_target(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("keep")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")
    LocalFileSystem(tmp_path).remove("/link.txt")
    assert target.read_text() == "keep"
    assert not link.exists()


def test_engine_import_is_independent():
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import anyshell; assert 'anytext' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("backend", ["asyncio", "trio"])
def test_headless_cli(backend, tmp_path):
    import subprocess

    (tmp_path / "input.txt").write_text("hello\nworld\n")
    command = [sys.executable, "-m", "anyshell", "--backend", backend, "-c"]
    result = subprocess.run(
        command + ["cat input.txt | count"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "2\n"
    result = subprocess.run(
        command + ["not-a-command"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "command not found" in result.stderr


def test_headless_listing_is_readable(tmp_path):
    import subprocess

    (tmp_path / "file.txt").write_text("data")
    (tmp_path / "folder").mkdir()
    result = subprocess.run(
        [sys.executable, "-m", "anyshell", "--root", str(tmp_path), "-c", "ls"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["file.txt", "folder/"]


async def test_touch_and_rmdir(filesystem):
    session = Session(filesystem)
    result, _, _ = await execute(session, "touch new.txt another.txt")
    assert result.status == 0
    assert list(filesystem.read_lines("/new.txt")) == []
    assert filesystem.stat("/another.txt")["size"] == 0
    if isinstance(filesystem, MemoryFileSystem):
        filesystem._modified["/notes.txt"] = 1.0
    else:
        import os

        os.utime(filesystem.root / "notes.txt", (1, 1))
    before = filesystem.stat("/notes.txt")["modified"]
    assert (await execute(session, "touch notes.txt docs"))[0].status == 0
    assert list(filesystem.read_lines("/notes.txt")) == ["zebra", "apple", "apricot"]
    assert filesystem.stat("/notes.txt")["modified"] > before
    assert filesystem.is_dir("/docs")
    assert (await execute(session, "touch missing/child.txt"))[0].status == 1
    assert (await execute(session, "rmdir new.txt"))[0].status == 1
    assert list(filesystem.read_lines("/new.txt")) == []
    assert (await execute(session, "rmdir docs"))[0].status == 1
    assert filesystem.is_dir("/docs")
    for path in ("/", "missing"):
        assert (await execute(session, f"rmdir {path}"))[0].status == 1
    await execute(session, "mkdir empty")
    await execute(session, "mkdir other")
    assert await session.complete("rmdir ") == [
        "rmdir docs/",
        "rmdir empty/",
        "rmdir other/",
    ]
    assert (await execute(session, "rmdir empty other"))[0].status == 0
    assert not filesystem.is_dir("/empty")
    assert not filesystem.is_dir("/other")
    await execute(session, "cd docs")
    assert (await execute(session, "touch relative.txt"))[0].status == 0
    assert filesystem.stat("/docs/relative.txt")["size"] == 0
    for command in ("touch", "rmdir"):
        assert (await execute(session, command))[0].status == 1
        assert (await execute(session, f"{command} unused | count"))[0].status == 1


def test_local_rmdir_rejects_directory_symlink(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(NotADirectoryError):
        LocalFileSystem(tmp_path).rmdir("/link")
    assert target.is_dir() and link.is_symlink()


async def test_mkdir_reports_existing_path_type(filesystem):
    session = Session(filesystem)
    for path, kind in (("docs", "directory"), ("notes.txt", "file")):
        result, _, events = await execute(session, f"mkdir {path}")
        assert result.status == 1
        assert [event.value for event in events if event.kind == "error"] == [
            f"mkdir: {path!r}: {kind} already exists"
        ]
    assert filesystem.is_dir("/docs")
    assert list(filesystem.read_lines("/notes.txt")) == ["zebra", "apple", "apricot"]


async def test_rm_multiple_paths_and_continues_after_errors(filesystem):
    session = Session(filesystem)
    await execute(session, "touch foo bar 'with spaces' ./-dash")
    result, _, _ = await execute(session, "rm foo bar 'with spaces'")
    assert result.status == 0
    for path in ("/foo", "/bar", "/with spaces"):
        with pytest.raises(FileNotFoundError):
            filesystem.stat(path)
    assert (await execute(session, "rm -- -dash"))[0].status == 0
    result, _, events = await execute(session, "rm missing docs notes.txt")
    assert result.status == 1
    errors = [event.value for event in events if event.kind == "error"]
    assert any(
        "'missing': no such file" in error and "'docs' is a directory" in error
        for error in errors
    )
    assert filesystem.is_dir("/docs")
    with pytest.raises(FileNotFoundError):
        filesystem.stat("/notes.txt")
    assert (await execute(session, "rm"))[0].status == 1
