"""Read cursors and lossless, path-aware notifications on both filesystems."""

import anyio
import pytest

from anyshell import LocalFileSystem, MemoryFileSystem


@pytest.fixture(params=["memory", "local"])
def fs(request, tmp_path):
    return (
        MemoryFileSystem() if request.param == "memory" else LocalFileSystem(tmp_path)
    )


def symlink(fs, target, path):
    try:
        fs.symlink(target, path)
    except (OSError, NotImplementedError):
        if isinstance(fs, LocalFileSystem):
            pytest.skip("symbolic links unavailable")
        raise


async def changed(handle):
    with anyio.fail_after(2):
        await handle.wait_changed()


async def unchanged(handle):
    with anyio.move_on_after(0.01) as scope:
        await handle.wait_changed()
    assert scope.cancel_called


async def test_independent_read_positions_and_utf8(fs):
    fs.write_text("/log", "héllo\n")
    async with fs.open("/log") as first, fs.open("/log") as second:
        assert await first.read() == "héllo\n"
        assert first.tell() == len("héllo\n".encode())
        assert await first.read() == ""
        assert await second.read() == "héllo\n"
        assert first.seek(1) == 1
        assert await first.read_bytes(1) == b"\xc3"
        assert await first.read_bytes(1) == b"\xa9"
        assert first.seek(-1, 2) == first.size - 1
        assert await first.read() == "\n"
        for args in [(-1,), (0, 3)]:
            with pytest.raises(ValueError):
                first.seek(*args)


async def test_latched_broadcasts_and_burst_coalescing(fs):
    fs.write_text("/log", "initial\n")
    async with fs.open("/log") as first, fs.open("/log") as second:
        assert await first.read() == "initial\n"
        # All writes precede wait_changed: notifications must remain pending.
        for value in ("a\n", "b\n", "c\n"):
            fs.append_text("/log", value)
        await changed(first)
        await changed(second)
        assert await first.read() == "a\nb\nc\n"
        assert await second.read() == "initial\na\nb\nc\n"
        await unchanged(first)
        fs.write_text("/other", "unrelated")
        await unchanged(first)


@pytest.mark.parametrize("operation", ["write", "copy", "move"])
async def test_replacements_restart_even_when_longer_than_original(fs, operation):
    fs.write_text("/log", "old\n")
    fs.write_text("/replacement", "replacement with more bytes\n")
    async with fs.open("/log") as file:
        await file.read()
        if operation == "write":
            fs.write_text("/log", "replacement with more bytes\n")
        else:
            getattr(fs, operation)("/replacement", "/log")
        await changed(file)
        assert file.revision > 0
        assert await file.read() == "replacement with more bytes\n"


@pytest.mark.parametrize("operation", ["remove", "move"])
async def test_removed_paths_wake_readers(fs, operation):
    fs.mkdir("/directory")
    fs.write_text("/directory/log", "old\n")
    async with fs.open("/directory/log") as file:
        await file.read()
        if operation == "remove":
            fs.remove("/directory", recursive=True)
        else:
            fs.move("/directory", "/renamed")
        await changed(file)
        with pytest.raises(FileNotFoundError):
            await file.read()


async def test_directory_replacement_wakes_nested_handle(fs):
    fs.mkdir("/directory")
    fs.write_text("/directory/log", "old\n")
    fs.mkdir("/replacement")
    fs.write_text("/replacement/log", "new\n")
    async with fs.open("/directory/log") as file:
        await file.read()
        fs.remove("/directory", recursive=True)
        fs.move("/replacement", "/directory")
        await changed(file)
        assert await file.read() == "new\n"


async def test_aliases_wake_each_other_without_notifying_unchanged_targets(fs):
    fs.mkdir("/directory")
    fs.write_text("/directory/log", "old\n")
    symlink(fs, "/directory", "/alias")
    async with fs.open("/directory/log") as real, fs.open("/alias/log") as alias:
        await real.read()
        await alias.read()
        fs.append_text("/alias/log", "through alias\n")
        await changed(real)
        await changed(alias)
        assert await real.read() == await alias.read() == "through alias\n"
        fs.append_text("/directory/log", "through target\n")
        await changed(real)
        await changed(alias)
        assert await real.read() == await alias.read() == "through target\n"
        symlink(fs, "/directory/log", "/other-link")
        fs.move("/other-link", "/moved-link")
        await unchanged(real)
        fs.remove("/alias")
        await changed(alias)
        with pytest.raises(FileNotFoundError):
            await alias.read()
        await unchanged(real)


async def test_touch_and_failed_writes(fs):
    fs.write_text("/log", "old\n")
    async with fs.open("/log") as file:
        await file.read()
        fs.touch("/log")
        await changed(file)
        assert await file.read() == ""
        with pytest.raises(OSError):
            fs.copy("/missing", "/log")
        await unchanged(file)


async def test_cancellation_and_failed_open_leave_no_subscriptions(fs):
    fs.write_text("/log", "text")
    with anyio.CancelScope() as scope:
        async with fs.open("/log") as file:
            scope.cancel()
            await file.wait_changed()
    assert file.closed and not fs._handles
    for path in ["/missing", "/"]:
        with pytest.raises(OSError):
            async with fs.open(path):
                pytest.fail("opening should fail")
        assert not fs._handles
    with pytest.raises(ValueError):
        await file.read()
    with pytest.raises(ValueError):
        await file.wait_changed()


async def test_closing_handle_wakes_waiter(fs):
    fs.write_text("/log", "text")
    entered, closed = anyio.Event(), anyio.Event()
    async with fs.open("/log") as file:

        async def wait():
            entered.set()
            with pytest.raises(ValueError):
                await file.wait_changed()
            closed.set()

        with anyio.fail_after(2):
            async with anyio.create_task_group() as group:
                group.start_soon(wait)
                await entered.wait()
                await file.aclose()
                await closed.wait()
    assert not fs._handles
