"""Typed Python commands with Cyclopts argument parsing."""

from __future__ import annotations

import codecs
import json
import math
import posixpath
import re
from collections import deque
from contextlib import AsyncExitStack, ExitStack, aclosing
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from io import IncrementalNewlineDecoder
from typing import Annotated, Literal

import anyio
from cyclopts import Parameter

from .engine import (
    Command,
    Context,
    EditRequest,
    Event,
    PageRequest,
    ShellError,
    _CommandStatus,
)
from .filesystem import FileHandle, format_filesystem_error

BUILTINS: list[Command] = []


def builtin(name: str, description: str, *, standalone: bool = False):
    def register(function):
        BUILTINS.append(Command(name, description, function, standalone))
        return function

    return register


def render(value) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


@builtin("help", "List commands or show a command's options.")
async def help_command(
    command: str | None = None, *subcommands: str, ctx: Context, stdout
):
    if command is not None:
        if command not in ctx.session.commands:
            raise ShellError(f"command not found: {command}")
        if stdout.is_terminal:
            await stdout.send(
                ctx.session.commands[command].help_display(
                    subcommands, width=ctx.output_width
                )
            )
            return
        for line in (
            ctx.session.commands[command]
            .help_text(subcommands, width=ctx.output_width)
            .splitlines()
        ):
            await stdout.send(line)
    else:
        for name, entry in sorted(ctx.session.commands.items()):
            await stdout.send(f"{name:10} {entry.description}")


@builtin("echo", "Output one text value.")
async def echo(*words: str, stdout):
    await stdout.send(" ".join(words))


@builtin("pwd", "Print the session's logical working directory.")
async def pwd(*, ctx: Context, stdout):
    await stdout.send(ctx.session.cwd)


@builtin("cd", "Change the working directory.", standalone=True)
async def cd(directory: str = "/", *, ctx: Context):
    path = ctx.resolve(directory)
    if not ctx.fs.is_dir(path):
        raise ShellError(f"not a directory: {path}")
    ctx.session.cwd = path


@builtin("ls", "List files or directory entries as structured records.")
async def ls(
    directory: str = ".",
    *,
    long: Annotated[
        bool, Parameter(name=["--long", "-l"], help="Show size and modification time.")
    ] = False,
    all: Annotated[
        bool, Parameter(name=["--all", "-a"], help="Include hidden entries.")
    ] = False,
    ctx: Context,
    stdout,
):
    from .presentation import file_listing

    rows = []

    async def output(row):
        if stdout.is_terminal:
            rows.append(row)
            if len(rows) >= 128:
                await stdout.send(file_listing(rows, long=long))
                rows.clear()
        else:
            await stdout.send(row)

    path = ctx.resolve(directory)
    if not ctx.fs.is_dir(path):
        metadata = ctx.fs.stat(path)  # Also validates existence for a plain listing.
        row = {"name": posixpath.basename(path), "type": "file"}
        if long:
            row.update(metadata)
        await output(row)
        if rows:
            await stdout.send(file_listing(rows, long=long))
        return
    for row in ctx.fs.listdir(path):
        if not all and row["name"].startswith("."):
            continue
        if long:
            row = {**row, **ctx.fs.stat(ctx.resolve(directory + "/" + row["name"]))}
        await output(row)
    if rows:
        await stdout.send(file_listing(rows, long=long))


@builtin("find", "Walk directory trees and filter entries by name or type.")
async def find(
    *paths: str,
    name: Annotated[
        str | None,
        Parameter(
            name=["-name", "--name"], help="Match the basename with a glob pattern."
        ),
    ] = None,
    iname: Annotated[
        str | None,
        Parameter(
            name=["-iname", "--iname"], help="Match the basename without letter case."
        ),
    ] = None,
    type: Annotated[
        Literal["f", "d", "l"] | None,
        Parameter(
            name=["-type", "--type"],
            help="Select files (f), directories (d), or symbolic links (l).",
        ),
    ] = None,
    max_depth: Annotated[
        int | None,
        Parameter(
            name=["-maxdepth", "--max-depth"],
            help="Descend at most this many levels; starting paths have depth zero.",
        ),
    ] = None,
    ctx: Context,
    stdout,
):
    if max_depth is not None and max_depth < 0:
        raise ShellError("maximum depth must be nonnegative")
    kinds = {"f": "file", "d": "directory", "l": "symlink"}
    errors = []
    for root in paths or (".",):
        stack = [(ctx.resolve(root), posixpath.normpath(root), 0)]
        while stack:
            await anyio.lowlevel.checkpoint()
            path, display, depth = stack.pop()
            try:
                kind = ctx.fs.entry_type(path)
            except FileNotFoundError:
                errors.append(f"{display!r}: no such file or directory")
                continue
            except OSError as error:
                errors.append(
                    f"{display!r}: {format_filesystem_error(error, ctx.fs, include_path=False)}"
                )
                continue
            basename = posixpath.basename(display.rstrip("/")) or "/"
            if (
                (name is None or fnmatchcase(basename, name))
                and (
                    iname is None or fnmatchcase(basename.casefold(), iname.casefold())
                )
                and (type is None or kind == kinds[type])
            ):
                await stdout.send({"name": basename, "path": display, "type": kind})
            if kind == "directory" and (max_depth is None or depth < max_depth):
                try:
                    entries = ctx.fs.listdir(path)
                except OSError as error:
                    errors.append(
                        f"{display!r}: {format_filesystem_error(error, ctx.fs, include_path=False)}"
                    )
                    continue
                for entry in reversed(entries):
                    stack.append(
                        (
                            posixpath.join(path, entry["name"]),
                            posixpath.join(display, entry["name"]),
                            depth + 1,
                        )
                    )
    if errors:
        raise ShellError("; ".join(errors))


@builtin("cat", "Stream file lines, or pass input through.")
async def cat(*files: str, ctx: Context, stdin, stdout):
    if not files:
        async for value in stdin:
            await stdout.send(value)
    else:
        for name in files:
            for line in ctx.fs.read_lines(ctx.resolve(name)):
                await stdout.send(line)


@dataclass
class _TailFile:
    """Track a followed file's byte offset and incomplete text line."""

    name: str
    file: FileHandle
    revision: int = 0
    pending: str = ""
    decoder: IncrementalNewlineDecoder = field(
        default_factory=lambda: IncrementalNewlineDecoder(
            codecs.getincrementaldecoder("utf-8")(), translate=True
        )
    )

    async def read(self):
        """Read to a snapshot of EOF, resetting partial data on replacements."""
        end = self.file.size
        while self.file.tell() < end or self.revision != self.file.revision:
            size = min(65536, max(0, end - self.file.tell())) or 65536
            chunk = await self.file.read_bytes(size)
            if self.revision != self.file.revision:
                self.pending = ""
                self.decoder.reset()
                self.revision = self.file.revision
            if not chunk:
                break
            text = self.pending + self.decoder.decode(chunk)
            *lines, self.pending = text.split("\n")
            for line in lines:
                yield line


async def _tail_changes(reader: _TailFile, send) -> None:
    """Forward a handle's latched notifications into a multi-file follower."""
    async with send:
        while True:
            await reader.file.wait_changed()
            await send.send(reader)


@builtin(
    "tail", "Print the last lines of input or files, optionally following changes."
)
async def tail(
    *files: Annotated[str, Parameter(allow_leading_hyphen=True)],
    lines: Annotated[
        int, Parameter(name=["--lines", "-n"], help="Number of trailing lines.")
    ] = 10,
    follow: Annotated[
        bool, Parameter(name=["--follow", "-f"], help="Keep watching named files.")
    ] = False,
    ctx: Context,
    stdin,
    stdout,
):
    if lines < 0:
        raise ShellError("line count must be nonnegative")
    readers = []
    multiple = len(files) > 1
    last_name = None
    async with AsyncExitStack() as stack:
        for name in files or ("-",):
            if multiple:
                await stdout.send(f"==> {name} <==")
            last_name = name
            trailing = deque(maxlen=lines)
            if name == "-":
                async for value in stdin:
                    trailing.append(value)
            elif not follow:
                source = ctx.fs.read_lines(ctx.resolve(name))
                try:
                    for line in source:
                        trailing.append(line)
                        await anyio.lowlevel.checkpoint()
                finally:
                    if close := getattr(source, "close", None):
                        close()
            else:
                file = await stack.enter_async_context(ctx.fs.open(ctx.resolve(name)))
                reader = _TailFile(name, file)
                if lines:
                    async for line in reader.read():
                        trailing.append(line)
                    if reader.pending and len(trailing) == lines:
                        trailing.popleft()
                else:
                    file.seek(0, 2)
                readers.append(reader)
            for value in trailing:
                await stdout.send(value)
        if not readers:
            return
        send, receive = anyio.create_memory_object_stream(len(readers))
        error = None
        async with anyio.create_task_group() as group:
            for reader in readers:
                group.start_soon(_tail_changes, reader, send.clone())
            try:
                async with send, receive:
                    async for reader in receive:
                        async for line in reader.read():
                            if multiple and reader.name != last_name:
                                await stdout.send(f"==> {reader.name} <==")
                                last_name = reader.name
                            await stdout.send(line)
            except Exception as exc:
                # Keep filesystem errors portable instead of wrapping them in
                # a task-group exception. Watchers only wait and forward events.
                error = exc
                group.cancel_scope.cancel()
        if error is not None:
            raise error


@builtin("grep", "Filter input or file lines using a Python regular expression.")
async def grep(
    pattern: str,
    *files: str,
    ignore_case: Annotated[
        bool, Parameter(name=["--ignore-case", "-i"], help="Ignore letter case.")
    ] = False,
    ctx: Context,
    stdin,
    stdout,
):
    expression = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    if files:
        for name in files:
            for line in ctx.fs.read_lines(ctx.resolve(name)):
                if expression.search(line):
                    await stdout.send(line)
                else:
                    await anyio.lowlevel.checkpoint()
    else:
        async for value in stdin:
            if expression.search(render(value)):
                await stdout.send(value)


@builtin("where", "Filter records by a field's text value.")
async def where(field: str, value: str, *, stdin, stdout):
    async for row in stdin:
        if not isinstance(row, dict) or field not in row:
            raise ShellError(f"input has no field {field!r}")
        if str(row[field]) == value:
            await stdout.send(row)


@builtin("select", "Extract one field from each input record.")
async def select(field: str, *, stdin, stdout):
    async for row in stdin:
        if not isinstance(row, dict) or field not in row:
            raise ShellError(f"input has no field {field!r}")
        await stdout.send(row[field])


@builtin("sort", "Buffer and sort input values or a record field.")
async def sort(
    field: str | None = None,
    *,
    reverse: Annotated[
        bool, Parameter(name=["--reverse", "-r"], help="Reverse the sort order.")
    ] = False,
    stdin,
    stdout,
):
    values = [value async for value in stdin]

    def key(value):
        if field is not None:
            if not isinstance(value, dict) or field not in value:
                raise ShellError(f"input has no field {field!r}")
            return render(value[field])
        return render(value)

    for value in sorted(values, key=key, reverse=reverse):
        await stdout.send(value)


@builtin("count", "Output the number of input values.")
async def count(*, stdin, stdout):
    total = 0
    async for _ in stdin:
        total += 1
    await stdout.send(total)


@builtin("mkdir", "Create one directory.", standalone=True)
async def mkdir(directory: str, *, ctx: Context):
    path = ctx.resolve(directory)
    try:
        ctx.fs.mkdir(path)
    except FileExistsError:
        kind = "directory" if ctx.fs.is_dir(path) else "file"
        raise ShellError(f"{directory!r}: {kind} already exists") from None


@builtin("rmdir", "Remove empty directories.", standalone=True)
async def rmdir(directory: str, *directories: str, ctx: Context):
    for name in (directory, *directories):
        ctx.fs.rmdir(ctx.resolve(name))
        await anyio.lowlevel.checkpoint()


@builtin(
    "touch",
    "Create files or update modification times without changing contents.",
    standalone=True,
)
async def touch(path: str, *paths: str, ctx: Context):
    for name in (path, *paths):
        ctx.fs.touch(ctx.resolve(name))
        await anyio.lowlevel.checkpoint()


@builtin("write", "Create or replace a UTF-8 file.", standalone=True)
async def write(path: str, *text: str, ctx: Context):
    if not text:
        raise ShellError("write requires text after the file name")
    ctx.fs.write_text(ctx.resolve(path), " ".join(text) + "\n")


@builtin("ln", "Create a symbolic link with -s.", standalone=True)
async def ln(
    target: str,
    link: str,
    *,
    symbolic: Annotated[
        bool, Parameter(name=["--symbolic", "-s"], help="Create a symbolic link.")
    ] = False,
    ctx: Context,
):
    if not symbolic:
        raise ShellError("hard links are not supported; use ln -s")
    destination = ctx.resolve(link)
    if ctx.fs.is_dir(destination):
        destination = posixpath.join(
            destination, posixpath.basename(target.rstrip("/"))
        )
    ctx.fs.symlink(target, destination)


@builtin("rm", "Remove files, or directory trees with -r.", standalone=True)
async def rm(
    *paths: str,
    recursive: Annotated[
        bool,
        Parameter(name=["--recursive", "-r", "-R"], help="Remove directory trees."),
    ] = False,
    force: Annotated[
        bool,
        Parameter(name=["--force", "-f"], help="Ignore missing paths and operands."),
    ] = False,
    ctx: Context,
):
    if not paths and not force:
        raise ShellError("missing path")
    errors = []
    for name in paths:
        try:
            if recursive:
                ctx.fs.remove(ctx.resolve(name), recursive=True)
            else:
                ctx.fs.remove(ctx.resolve(name))
        except IsADirectoryError:
            errors.append(
                f"{name!r} is a directory; use rmdir to remove an empty directory"
            )
        except FileNotFoundError:
            if not force:
                errors.append(f"{name!r}: no such file")
        except OSError as error:
            errors.append(
                f"{name!r}: {format_filesystem_error(error, ctx.fs, include_path=False)}"
            )
        await anyio.lowlevel.checkpoint()
    if errors:
        raise ShellError("; ".join(errors))


def _transfer_paths(ctx: Context, source: str, destination: str) -> tuple[str, str]:
    original = ctx.resolve(source)
    target = ctx.resolve(destination)
    if ctx.fs.is_dir(target):
        target = posixpath.join(target, posixpath.basename(original))
    return original, target


@builtin("mv", "Move or rename a file or directory.", standalone=True)
async def mv(source: str, destination: str, *, ctx: Context):
    original, target = _transfer_paths(ctx, source, destination)
    ctx.fs.move(original, target)
    if ctx.session.cwd == original or ctx.session.cwd.startswith(original + "/"):
        ctx.session.cwd = target + ctx.session.cwd[len(original) :]


@builtin("cp", "Copy a file, or a directory with -r.", standalone=True)
async def cp(
    source: str,
    destination: str,
    *,
    recursive: Annotated[
        bool,
        Parameter(
            name=["--recursive", "-r"], help="Copy directories and their contents."
        ),
    ] = False,
    ctx: Context,
):
    original, target = _transfer_paths(ctx, source, destination)
    ctx.fs.copy(original, target, recursive=recursive)


@builtin("more", "Read files or pipeline output in the interactive pager.")
async def more(*files: str, ctx: Context, stdin):
    await _page(files, ctx, stdin, searchable=False)


@builtin("less", "Read and search files or pipeline output in the interactive pager.")
async def less(*files: str, ctx: Context, stdin):
    await _page(files, ctx, stdin, searchable=True)


async def _page(files: tuple[str, ...], ctx: Context, stdin, *, searchable: bool):
    from .presentation import format_value

    def file_lines(name):
        try:
            yield from ctx.fs.read_lines(ctx.resolve(name))
        except FileNotFoundError:
            raise ShellError(f"{name!r}: no such file or directory") from None
        except IsADirectoryError:
            raise ShellError(f"{name!r}: is a directory") from None

    with ExitStack() as stack:
        sources = []
        for name in files:
            source = file_lines(name)
            if (close := getattr(source, "close", None)) is not None:
                stack.callback(close)
            # Opening and reading once catches missing paths, permissions and
            # initial decoding errors before a frontend can display the pager.
            sources.append((next(source, None), source))

        async def lines():
            if files:
                for first, source in sources:
                    if first is not None:
                        yield first
                    for line in source:
                        yield line
            else:
                async for value in stdin:
                    yield format_value(value)

        async with aclosing(lines()) as stream:
            request = PageRequest(
                " · ".join(files) if files else "Pipeline output",
                stream,
                searchable=searchable,
            )
            await ctx.emit(Event("page", request))
            if not request.handled:
                raise ShellError("an interactive frontend is required to page text")


@builtin("edit", "Open a text file in the interactive editor.", standalone=True)
async def edit(path: str, *, ctx: Context):
    request = EditRequest(ctx.resolve(path))
    await ctx.emit(Event("edit", request))
    if not request.handled:
        raise ShellError("an interactive frontend is required to edit files")


@builtin("clear", "Clear the frontend transcript.", standalone=True)
async def clear(*, ctx: Context):
    await ctx.emit(Event("clear"))


@builtin("exit", "Close this shell session or pane.", standalone=True)
async def exit(status: int | None = None, *, ctx: Context):
    ctx.session.exit_status = (
        ctx.session.last_status if status is None else status
    ) % 256


@builtin("sleep", "Wait asynchronously; Ctrl+C cancels.")
async def sleep(seconds: float, *, ctx: Context):
    if not math.isfinite(seconds) or seconds < 0:
        raise ShellError("seconds must be finite and nonnegative")
    await ctx.sleep(seconds)


@builtin("jobs", "List running and stopped background jobs.")
async def jobs(*, ctx: Context, stdout):
    for job in list(ctx.session.jobs.values()):
        if job.result is not None:
            ctx.session.jobs.pop(job.id, None)
            continue
        await stdout.send(f"[{job.id}] {job.state}  {job.source}")


@builtin("wait", "Wait for one or all background jobs.", standalone=True)
async def wait(job: int | None = None, *, ctx: Context):
    status = await ctx.session.wait_jobs(job)
    if status:
        raise ShellError(f"background job exited with status {status}")


@builtin("kill", "Cancel, stop, or resume a job.", standalone=True)
async def kill(
    job: int,
    *,
    stop: Annotated[bool, Parameter(name="-STOP")] = False,
    cont: Annotated[bool, Parameter(name="-CONT")] = False,
    ctx: Context,
):
    if stop and cont:
        raise ShellError("choose either -STOP or -CONT")
    entry = ctx.session.get_job(job)
    if entry.result is not None:
        raise ShellError(f"job has finished: {job}")
    if stop:
        entry.stop()
        if not entry.foreground:
            await ctx.emit(Event("job", entry))
    elif cont:
        entry.resume()
        await ctx.emit(Event("job", entry))
    elif entry.scope is not None:
        entry.scope.cancel()


@builtin("bg", "Resume a stopped job in the background.", standalone=True)
async def bg(job: int | None = None, *, ctx: Context):
    entry = ctx.session.select_job(job, stopped_only=True)
    entry.resume()
    await ctx.emit(Event("job", entry))


@builtin(
    "fg", "Bring a job to the foreground, resuming it if stopped.", standalone=True
)
async def fg(job: int | None = None, *, ctx: Context):
    if ctx.session._control is not None and not ctx.session._control.foreground:
        raise ShellError("fg cannot run in the background")
    result = await ctx.session.foreground_job(job, ctx.emit)
    if result.status:
        raise _CommandStatus(result.status)
