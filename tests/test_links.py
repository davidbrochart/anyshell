"""Symbolic links behave consistently in disk and browser workspaces."""

import anyio
import pytest

from anyshell import LocalFileSystem, MemoryFileSystem, Session


@pytest.fixture(params=["memory", "local"])
def fs(request, tmp_path):
    if request.param == "memory":
        return MemoryFileSystem()
    try:
        probe = tmp_path / "probe"
        probe.symlink_to("missing")
        probe.unlink()
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links unavailable")
    return LocalFileSystem(tmp_path)


async def run(fs, source):
    events = []

    async def emit(event):
        events.append(event)

    result = await Session(fs).execute(source, emit)
    return result, events


async def test_relative_links_read_write_and_remove(fs):
    fs.mkdir("/docs")
    fs.write_text("/original", "hello")
    result, events = await run(fs, "ln -s ../original docs/link")
    assert result.status == 0
    assert not [e for e in events if e.kind == "output"]
    assert fs.entry_type("/docs/link") == "symlink"
    assert fs.read_text("/docs/link") == "hello"
    fs.write_text("/docs/link", "changed")
    assert fs.read_text("/original") == "changed"
    fs.remove("/docs/link", recursive=True)
    assert fs.read_text("/original") == "changed"


async def test_absolute_directory_and_dangling_links(fs):
    fs.mkdir("/docs")
    fs.write_text("/docs/file", "data")
    assert (await run(fs, "ln --symbolic /docs shortcut"))[0].status == 0
    assert fs.is_dir("/shortcut")
    assert fs.read_text("/shortcut/file") == "data"
    fs.write_text("/shortcut/new", "new")
    assert fs.read_text("/docs/new") == "new"
    assert (await run(fs, "ln -s missing dangling"))[0].status == 0
    assert fs.entry_type("/dangling") == "symlink"
    with pytest.raises(FileNotFoundError):
        fs.read_text("/dangling")
    fs.write_text("/dangling", "created")
    assert fs.read_text("/missing") == "created"
    fs.remove("/missing")
    fs.remove("/dangling")
    with pytest.raises(NotADirectoryError):
        fs.rmdir("/shortcut")
    fs.remove("/shortcut")
    assert fs.read_text("/docs/file") == "data"


async def test_links_errors_and_directory_destination(fs):
    fs.mkdir("/docs")
    fs.write_text("/original", "keep")
    assert (await run(fs, "ln -s ../original docs"))[0].status == 0
    assert fs.read_text("/docs/original") == "keep"
    for command in (
        "ln original other",
        "ln -s missing original",
        "ln -s target absent/link",
        "ln -s target",
    ):
        assert (await run(fs, command))[0].status == 1
    assert fs.read_text("/original") == "keep"
    assert (await run(fs, "ln -s missing dangling"))[0].status == 0
    assert (await run(fs, "ln -s other dangling"))[0].status == 1


async def test_link_cycles_and_find(fs):
    fs.mkdir("/docs")
    fs.symlink("docs", "/shortcut")
    fs.symlink("missing", "/dangling")
    fs.symlink("second", "/first")
    fs.symlink("first", "/second")
    with anyio.fail_after(3):
        result, events = await run(fs, "cat first")
        assert result.status == 1
        result, events = await run(fs, "find . -type l")
    assert result.status == 0
    assert {e.value["path"] for e in events if e.kind == "output"} == {
        "./shortcut",
        "./dangling",
        "./first",
        "./second",
    }
    assert {r["name"] for r in fs.listdir("/") if r["type"] == "symlink"} == {
        "shortcut",
        "dangling",
        "first",
        "second",
    }


async def test_links_survive_moves_and_directory_copies(fs):
    fs.mkdir("/docs")
    fs.write_text("/docs/file", "hello")
    fs.symlink("file", "/docs/link")
    fs.symlink("missing", "/dangling")
    fs.move("/docs", "/renamed")
    assert fs.read_text("/renamed/link") == "hello"
    fs.copy("/renamed", "/copied", recursive=True)
    assert fs.entry_type("/copied/link") == "symlink"
    assert fs.read_text("/copied/link") == "hello"
    fs.remove("/renamed", recursive=True)
    assert fs.read_text("/copied/link") == "hello"


async def test_move_dangling_link_and_replace_link(fs):
    fs.symlink("missing", "/dangling")
    fs.move("/dangling", "/moved")
    assert fs.entry_type("/moved") == "symlink"
    with pytest.raises(FileNotFoundError):
        fs.entry_type("/dangling")
    fs.write_text("/file", "data")
    fs.move("/file", "/moved")
    assert fs.entry_type("/moved") == "file"
    assert fs.read_text("/moved") == "data"


async def test_directory_link_parent_and_touch(fs):
    fs.mkdir("/docs")
    fs.symlink("docs", "/alias")
    fs.symlink("file", "/alias/link")
    fs.touch("/alias/link")
    assert fs.read_text("/docs/file") == ""
    fs.mkdir("/alias/nested")
    assert fs.is_dir("/docs/nested")
    fs.remove("/alias/nested", recursive=True)
    assert not fs.is_dir("/docs/nested")
