"""Recursive search preserves Python records and portable workspace paths."""

import subprocess
import sys

import anyio
import pytest

from anyshell import LocalFileSystem, MemoryFileSystem, Session
from anyshell.presentation import format_value


@pytest.fixture(params=["memory", "local"])
def filesystem(request, tmp_path):
    fs = MemoryFileSystem() if request.param == "memory" else LocalFileSystem(tmp_path)
    fs.mkdir("/docs")
    fs.mkdir("/docs/nested")
    fs.write_text("/docs/a.txt", "a")
    fs.write_text("/docs/nested/B.TXT", "b")
    fs.write_text("/docs/.hidden.txt", "hidden")
    fs.write_text("/root.py", "python")
    return fs


async def execute(session, source):
    events = []

    async def emit(event):
        events.append(event)

    result = await session.execute(source, emit)
    return result, [e.value for e in events if e.kind == "output"], events


async def test_find_default_traversal_and_records(filesystem):
    result, rows, _ = await execute(Session(filesystem), "find")
    assert result.status == 0
    assert [r["path"] for r in rows] == [
        ".",
        "./docs",
        "./docs/.hidden.txt",
        "./docs/a.txt",
        "./docs/nested",
        "./docs/nested/B.TXT",
        "./root.py",
    ]
    assert rows[0] == {"name": ".", "path": ".", "type": "directory"}
    assert rows[2] == {
        "name": ".hidden.txt",
        "path": "./docs/.hidden.txt",
        "type": "file",
    }
    assert format_value(rows[2]) == "./docs/.hidden.txt"
    assert format_value(rows[1]) == "./docs/"


async def test_find_filters_depth_and_pipelines(filesystem):
    session = Session(filesystem)
    for flag in ("-name", "--name"):
        result, rows, _ = await execute(session, f"find . {flag} '*.txt' -type f")
        assert result.status == 0
        assert [r["path"] for r in rows] == ["./docs/.hidden.txt", "./docs/a.txt"]
    assert [
        r["path"] for r in (await execute(session, "find docs -iname '*.txt'"))[1]
    ] == ["docs/.hidden.txt", "docs/a.txt", "docs/nested/B.TXT"]
    assert (await execute(session, "find . -type f | count"))[1] == [4]
    assert (await execute(session, "find docs -name '*.txt' | select path"))[1] == [
        "docs/.hidden.txt",
        "docs/a.txt",
    ]
    assert [r["path"] for r in (await execute(session, "find . --type d"))[1]] == [
        ".",
        "./docs",
        "./docs/nested",
    ]
    assert [r["path"] for r in (await execute(session, "find . -maxdepth 0"))[1]] == [
        "."
    ]
    assert [
        r["path"] for r in (await execute(session, "find docs --max-depth 1"))[1]
    ] == ["docs", "docs/.hidden.txt", "docs/a.txt", "docs/nested"]
    assert (await execute(session, "find . -name 'nothing*'"))[1] == []


async def test_find_start_paths_and_errors(filesystem):
    session = Session(filesystem)
    assert [
        r["path"] for r in (await execute(session, "find root.py docs/nested"))[1]
    ] == ["root.py", "docs/nested", "docs/nested/B.TXT"]
    await execute(session, "cd docs")
    assert [
        r["path"] for r in (await execute(session, "find /docs -maxdepth 0"))[1]
    ] == ["/docs"]
    assert (await execute(session, "find . -type f | count"))[1] == [3]
    result, rows, events = await execute(session, "find missing a.txt")
    assert result.status == 1
    assert [r["path"] for r in rows] == ["a.txt"]
    assert any(
        e.kind == "error" and "no such file or directory" in e.value for e in events
    )
    for source in (
        "find . -maxdepth -1",
        "find . -type invalid",
        "find . -name",
        "find . --unknown",
    ):
        result, rows, _ = await execute(session, source)
        assert result.status == 1 and not rows
    assert await session.complete("find --max") == ["find --max-depth"]


async def test_find_does_not_follow_symlinks(tmp_path):
    fs = LocalFileSystem(tmp_path)
    fs.mkdir("/docs")
    fs.write_text("/docs/file.txt", "contents")
    try:
        (tmp_path / "docs/loop").symlink_to(tmp_path, target_is_directory=True)
        (tmp_path / "external").symlink_to(tmp_path.parent, target_is_directory=True)
        (tmp_path / "broken").symlink_to(tmp_path / "missing")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with anyio.fail_after(3):
        result, rows, _ = await execute(Session(fs), "find .")
    assert result.status == 0 and len(rows) == 6
    assert [r["path"] for r in rows if r["type"] == "symlink"] == [
        "./broken",
        "./docs/loop",
        "./external",
    ]
    result, rows, _ = await execute(Session(fs), "find external -type l")
    assert result.status == 0
    assert rows == [{"name": "external", "path": "external", "type": "symlink"}]
    assert format_value(rows[0]) == "external"


async def test_find_can_quit_early_in_pipeline(filesystem):
    from anyshell import Command

    async def first(*, stdin, stdout):
        await stdout.send(await stdin.receive())

    session = Session(filesystem)
    session.register(Command("first", "Take one", first))
    result, rows, _ = await execute(session, "find . | first")
    assert result.status == 0
    assert rows == [{"name": ".", "path": ".", "type": "directory"}]


def test_find_headless_paths(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/file.txt").write_text("data")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "anyshell",
            "--root",
            str(tmp_path),
            "-c",
            "find . -type f",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout == "./nested/file.txt\n"
    assert not result.stderr
