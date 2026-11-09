"""Portable file and directory transfers through shell commands."""

import pytest

from anyshell import LocalFileSystem, MemoryFileSystem, Session


@pytest.fixture(params=["memory", "local"])
def filesystem(request, tmp_path):
    fs = MemoryFileSystem() if request.param == "memory" else LocalFileSystem(tmp_path)
    fs.write_text("/source.txt", "héllo\r\n")
    fs.write_text("/existing.txt", "replace me")
    fs.mkdir("/tree")
    fs.mkdir("/tree/nested")
    fs.write_text("/tree/nested/file.txt", "contents")
    fs.write_text("/tree/.hidden", "hidden")
    fs.mkdir("/destination")
    return fs


async def execute(session, source):
    events = []

    async def emit(event):
        events.append(event)

    result = await session.execute(source, emit)
    return result, events


@pytest.mark.parametrize("command", ["cp", "mv"])
async def test_transfer_files_and_overwrite(filesystem, command):
    session = Session(filesystem)
    before = filesystem.stat("/source.txt")
    result, _ = await execute(session, f"{command} source.txt renamed.txt")
    assert result.status == 0
    assert filesystem.read_text("/renamed.txt") == "héllo\r\n"
    assert filesystem.stat("/renamed.txt") == before
    if command == "mv":
        with pytest.raises(FileNotFoundError):
            filesystem.stat("/source.txt")
    else:
        assert filesystem.read_text("/source.txt") == "héllo\r\n"
    assert (await execute(session, f"{command} renamed.txt existing.txt"))[
        0
    ].status == 0
    assert filesystem.read_text("/existing.txt") == "héllo\r\n"
    assert (await execute(session, f"{command} existing.txt destination"))[
        0
    ].status == 0
    assert filesystem.read_text("/destination/existing.txt") == "héllo\r\n"


@pytest.mark.parametrize("command", ["cp -r", "cp --recursive", "mv"])
async def test_transfer_directory_tree(filesystem, command):
    session = Session(filesystem)
    before = filesystem.stat("/tree/nested/file.txt")
    assert (await execute(session, f"{command} tree destination"))[0].status == 0
    assert filesystem.read_text("/destination/tree/nested/file.txt") == "contents"
    assert filesystem.read_text("/destination/tree/.hidden") == "hidden"
    assert filesystem.stat("/destination/tree/nested/file.txt") == before
    assert filesystem.is_dir("/destination/tree/nested")
    assert filesystem.is_dir("/tree") == (command != "mv")
    if command != "mv":
        filesystem.write_text("/destination/tree/nested/file.txt", "copy changed")
        assert filesystem.read_text("/tree/nested/file.txt") == "contents"


async def test_move_updates_working_directory(filesystem):
    session = Session(filesystem)
    await execute(session, "cd tree/nested")
    result, events = await execute(session, "mv /tree /renamed")
    assert result.status == 0
    assert session.cwd == "/renamed/nested"
    assert any(e.kind == "cwd" and e.value == session.cwd for e in events)
    assert (await execute(session, "cp file.txt local.txt"))[0].status == 0
    assert filesystem.read_text("/renamed/nested/local.txt") == "contents"
    assert (await execute(session, "mv /renamed /destination"))[0].status == 0
    assert session.cwd == "/destination/renamed/nested"


async def test_transfer_errors_preserve_sources(filesystem):
    session = Session(filesystem)
    for command in (
        "cp tree copy",
        "cp -r tree tree/nested",
        "mv tree tree/nested",
        "mv / destination",
        "cp -r / destination",
        "cp source.txt source.txt",
        "mv source.txt source.txt",
        "cp source.txt missing/file.txt",
        "mv source.txt missing/file.txt",
        "cp missing destination",
        "mv missing destination",
        "cp -r tree existing.txt",
        "mv tree existing.txt",
        "cp source.txt destination | count",
        "mv source.txt destination | count",
        "cp source.txt",
        "mv source.txt",
    ):
        result, events = await execute(session, command)
        assert result.status == 1, command
        assert any(e.kind == "error" for e in events), command
        assert filesystem.read_text("/source.txt") == "héllo\r\n"
        assert filesystem.read_text("/tree/nested/file.txt") == "contents"
        assert filesystem.read_text("/existing.txt") == "replace me"
        assert filesystem.listdir("/destination") == []
    assert not filesystem.is_dir("/copy")
    await execute(session, "mkdir destination/tree")
    for command in ("cp -r", "mv"):
        assert (await execute(session, f"{command} tree destination"))[0].status == 1
        assert filesystem.listdir("/destination/tree") == []
        assert filesystem.read_text("/tree/nested/file.txt") == "contents"


async def test_transfer_quoted_paths_and_completion(filesystem):
    filesystem.write_text("/space name.txt", "spaces")
    session = Session(filesystem)
    assert (await execute(session, "cp 'space name.txt' 'copy name.txt'"))[
        0
    ].status == 0
    assert (await execute(session, "mv 'copy name.txt' destination"))[0].status == 0
    assert filesystem.read_text("/destination/copy name.txt") == "spaces"
    assert await session.complete("cp --rec") == ["cp --recursive"]
    assert await session.complete("mv sou") == ["mv source.txt"]
    assert await session.complete("cp source.txt dest") == [
        "cp source.txt destination/"
    ]


def test_local_transfers_preserve_bytes_and_move_symlink(tmp_path):
    fs = LocalFileSystem(tmp_path)
    (tmp_path / "binary").write_bytes(b"\x00\xff\r\n")
    fs.copy("/binary", "/copied")
    fs.move("/copied", "/moved")
    assert (tmp_path / "moved").read_bytes() == b"\x00\xff\r\n"
    try:
        (tmp_path / "link").symlink_to(tmp_path / "binary")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    fs.move("/link", "/renamed-link")
    assert (tmp_path / "renamed-link").is_symlink()
    assert (tmp_path / "binary").read_bytes() == b"\x00\xff\r\n"


def test_local_transfer_rejects_escape_and_descendant_alias(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    fs = LocalFileSystem(root)
    fs.mkdir("/tree")
    fs.write_text("/tree/file", "keep")
    try:
        (root / "alias").symlink_to(root / "tree", target_is_directory=True)
        (root / "outside").symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    for transfer in (
        fs.move,
        lambda source, dest: fs.copy(source, dest, recursive=True),
    ):
        with pytest.raises(OSError):
            transfer("/tree", "/alias/child")
        with pytest.raises(PermissionError):
            transfer("/tree/file", "/outside/escaped")
        assert fs.read_text("/tree/file") == "keep"
    assert not (tmp_path / "escaped").exists()
