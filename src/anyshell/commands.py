"""Typed Python commands with Cyclopts argument parsing."""

from __future__ import annotations

import json
import math
import posixpath
import re
from contextlib import aclosing
from fnmatch import fnmatchcase
from typing import Annotated, Literal

import anyio
from cyclopts import Parameter

from .engine import Command, Context, EditRequest, Event, PageRequest, ShellError

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
async def help_command(command: str | None = None, *, ctx: Context, stdout):
    if command is not None:
        if command not in ctx.session.commands:
            raise ShellError(f"command not found: {command}")
        for line in ctx.session.commands[command].help_text().splitlines():
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
    path = ctx.resolve(directory)
    if not ctx.fs.is_dir(path):
        metadata = ctx.fs.stat(path)  # Also validates existence for a plain listing.
        row = {"name": posixpath.basename(path), "type": "file"}
        if long:
            row.update(metadata)
        await stdout.send(row)
        return
    for row in ctx.fs.listdir(path):
        if not all and row["name"].startswith("."):
            continue
        if long:
            row = {**row, **ctx.fs.stat(ctx.resolve(directory + "/" + row["name"]))}
        await stdout.send(row)


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
                errors.append(f"{display!r}: {error}")
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
                    errors.append(f"{display!r}: {error}")
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


@builtin("rm", "Remove files. Use rmdir for empty directories.", standalone=True)
async def rm(path: str, *paths: str, ctx: Context):
    errors = []
    for name in (path, *paths):
        try:
            ctx.fs.remove(ctx.resolve(name))
        except IsADirectoryError:
            errors.append(
                f"{name!r} is a directory; use rmdir to remove an empty directory"
            )
        except FileNotFoundError:
            errors.append(f"{name!r}: no such file")
        except OSError as error:
            errors.append(f"{name!r}: {error}")
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

    async def lines():
        if files:
            for name in files:
                for line in ctx.fs.read_lines(ctx.resolve(name)):
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


@builtin("sleep", "Wait asynchronously; Ctrl+C cancels.")
async def sleep(seconds: float, *, stdout):
    if not math.isfinite(seconds) or seconds < 0:
        raise ShellError("seconds must be finite and nonnegative")
    await anyio.sleep(seconds)
    await stdout.send(f"slept {seconds:g}s")
