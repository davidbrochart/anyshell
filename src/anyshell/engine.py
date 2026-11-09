"""UI-independent sessions and streaming Python command pipelines."""

from __future__ import annotations

import posixpath
import shlex
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from inspect import BoundArguments, isawaitable
from io import StringIO
from typing import Any, Literal

import anyio
from cyclopts import App as ArgumentParser
from cyclopts import CycloptsError, Parameter
from rich.console import Console

from .filesystem import FileSystem, MemoryFileSystem, resolve_path


class ShellError(Exception):
    """A command or syntax error suitable for display to the shell user."""


@dataclass(frozen=True)
class Event:
    kind: Literal["output", "error", "clear", "cwd", "status", "edit", "page"]
    value: Any = None


@dataclass
class EditRequest:
    """An interactive frontend acknowledges and handles a file-edit request."""

    path: str
    handled: bool = False


@dataclass
class PageRequest:
    """A frontend displays streamed text and returns when its pager closes."""

    title: str
    lines: AsyncIterator[str]
    searchable: bool = False
    handled: bool = False


Emitter = Callable[[Event], Awaitable[None]]


@dataclass
class Context:
    session: Session
    emit: Emitter

    @property
    def fs(self) -> FileSystem:
        return self.session.filesystem

    def resolve(self, path: str) -> str:
        return resolve_path(self.session.cwd, path)


@dataclass(frozen=True)
class Command:
    """A typed Python command; ctx/context, stdin and stdout are injected by name.

    Injected parameters must be keyword-only. Other parameters are parsed by
    Cyclopts, including Annotated option aliases and validators.
    """

    name: str
    description: str
    run: Callable[..., Any]
    standalone: bool = False
    _parser: ArgumentParser = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        parser = ArgumentParser(
            name=self.name,
            help=self.description,
            version_flags=(),
            default_parameter=Parameter(parse=r"^(?!(ctx|context|stdin|stdout)$)"),
        )
        parser.default(self.run)
        object.__setattr__(self, "_parser", parser)

    def prepare(self, arguments: list[str]) -> _Invocation:
        output = StringIO()
        console = Console(file=output, width=88, color_system=None)
        try:
            callback, bound, ignored = self._parser.parse_args(
                arguments,
                console=console,
                error_console=console,
                print_error=False,
                exit_on_error=False,
                help_on_error=False,
            )
        except CycloptsError as error:
            raise ShellError(f"{self.name}: {error}") from error
        unknown = set(ignored) - {"ctx", "context", "stdin", "stdout"}
        if unknown:
            raise ShellError(
                f"unsupported injected parameters: {', '.join(sorted(unknown))}"
            )
        return _Invocation(
            self.name,
            callback,
            bound,
            ignored,
            output,
            callback == self._parser.help_print,
        )

    def option_names(self) -> list[str]:
        names = set(self._parser.help_flags)
        for argument in self._parser.assemble_argument_collection():
            if argument.parse and argument.show:
                names.update(argument.names)
        return sorted(name for name in names if name.startswith("-"))

    def help_text(self) -> str:
        output = StringIO()
        self._parser.help_print(
            console=Console(file=output, width=88, color_system=None)
        )
        return output.getvalue().rstrip()


@dataclass
class _Invocation:
    name: str
    callback: Callable[..., Any]
    bound: BoundArguments
    ignored: dict[str, Any]
    help_output: StringIO
    is_help: bool

    async def run(self, context: Context, stdin, stdout) -> None:
        injected = {
            "ctx": context,
            "context": context,
            "stdin": stdin,
            "stdout": stdout,
        }
        self.bound.arguments.update({name: injected[name] for name in self.ignored})
        result = self.callback(*self.bound.args, **self.bound.kwargs)
        if isawaitable(result):
            result = await result
        if result is not None:
            await stdout.send(result)
        # Help uses a per-invocation console, never a global stdout redirect.
        for line in self.help_output.getvalue().splitlines():
            await stdout.send(line)


@dataclass(frozen=True)
class Result:
    status: int
    cancelled: bool = False


def parse_pipeline(source: str) -> list[list[str]]:
    """Split unquoted pipes, then parse each stage with shell-style quoting."""
    parts: list[str] = []
    start = 0
    quote = ""
    escaped = False
    for index, char in enumerate(source):
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "|":
            parts.append(source[start:index])
            start = index + 1
    parts.append(source[start:])
    try:
        stages = [shlex.split(part) for part in parts]
    except ValueError as error:
        raise ShellError(str(error)) from error
    if len(stages) == 1 and not stages[0]:
        return []
    if any(not stage for stage in stages):
        raise ShellError("empty pipeline stage")
    return stages


def _scan_commands(
    source: str, *, keep_empty_last: bool = False
) -> tuple[list[str], bool]:
    commands: list[str] = []
    buffer: list[str] = []
    quote = ""
    pipe = False
    incomplete = False
    index = 0
    while index < len(source):
        char = source[index]
        incomplete = False
        if char == "\\" and quote != "'":
            if index + 1 == len(source):
                buffer.append(char)
                incomplete = True
                break
            following = source[index + 1]
            if following != "\n":
                buffer.extend((char, following))
                pipe = False
            index += 2
            continue
        if quote:
            buffer.append(char)
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
            pipe = False
            buffer.append(char)
        elif char == "\n" and not pipe:
            if "".join(buffer).strip():
                commands.append("".join(buffer))
            buffer.clear()
        else:
            buffer.append(char)
            if not char.isspace():
                pipe = char == "|"
        index += 1
    if "".join(buffer).strip() or keep_empty_last:
        commands.append("".join(buffer))
    return commands, bool(quote or pipe or incomplete)


def is_complete(source: str) -> bool:
    """Whether Enter can submit, rather than continue an unfinished command."""
    return not _scan_commands(source)[1]


def parse_commands(source: str) -> list[list[list[str]]]:
    """Parse newline-separated pipelines, preserving quoted newlines."""
    commands, incomplete = _scan_commands(source)
    if incomplete:
        raise ShellError(
            "unfinished command: close quotes or complete the continuation"
        )
    return [parse_pipeline(command) for command in commands]


class Session:
    """A shell session owns commands, history, working directory and cancellation.

    Commands exchange Python values through bounded AnyIO streams. One command
    line may run per session. Distinct sessions can run concurrently.
    """

    def __init__(self, filesystem: FileSystem | None = None) -> None:
        self.filesystem = filesystem if filesystem is not None else MemoryFileSystem()
        self.cwd = "/"
        self.history: list[str] = []
        self.commands: dict[str, Command] = {}
        self._scope: anyio.CancelScope | None = None
        self.last_status = 0
        self._executing = False
        from .commands import BUILTINS

        for command in BUILTINS:
            self.register(command)

    @property
    def running(self) -> bool:
        return self._executing

    def register(self, command: Command) -> None:
        if not command.name or any(
            char.isspace() or char == "|" for char in command.name
        ):
            raise ValueError("command names must be nonempty words")
        self.commands[command.name] = command

    def cancel(self) -> bool:
        """Cancel the active execution from the same event loop."""
        if self._scope is None:
            return False
        self._scope.cancel()
        return True

    async def execute(self, source: str, emit: Emitter) -> Result:
        if self.running:
            raise ShellError("this session is already running a command")
        source = source.strip() if "\n" not in source else source
        if not source.strip():
            return Result(0)
        self._executing = True
        try:
            return await self._execute(source, emit)
        finally:
            self._executing = False

    async def _execute(self, source: str, emit: Emitter) -> Result:
        if not self.history or self.history[-1] != source:
            self.history.append(source)
        status = 0
        previous_cwd = self.cwd
        with anyio.CancelScope() as scope:
            self._scope = scope
            try:
                pipelines = []
                for stages in parse_commands(source):
                    commands = []
                    for stage in stages:
                        if stage[0] not in self.commands:
                            raise ShellError(f"command not found: {stage[0]}")
                        command = self.commands[stage[0]]
                        invocation = command.prepare(stage[1:])
                        if (
                            len(stages) > 1
                            and command.standalone
                            and not invocation.is_help
                        ):
                            raise ShellError(
                                f"{command.name} must run outside a pipeline"
                            )
                        commands.append(invocation)
                    pipelines.append(commands)
                for commands in pipelines:
                    status = await self._pipeline(commands, emit)
            except (ShellError, OSError, ValueError) as error:
                status = 1
                await emit(Event("error", str(error)))
            finally:
                self._scope = None
        cancelled = scope.cancel_called
        self.last_status = 130 if cancelled else status
        if self.cwd != previous_cwd:
            await emit(Event("cwd", self.cwd))
        await emit(Event("status", self.last_status))
        return Result(self.last_status, cancelled)

    async def _pipeline(self, commands, emit: Emitter) -> int:
        streams = [
            anyio.create_memory_object_stream[Any](16) for _ in range(len(commands) + 1)
        ]
        await streams[0][0].aclose()  # First stage gets EOF on standard input.
        statuses = [0] * len(commands)
        context = Context(self, emit)

        async def run_stage(index: int) -> None:
            receive = streams[index][1]
            send = streams[index + 1][0]
            async with receive, send:
                try:
                    await commands[index].run(context, receive, send)
                except anyio.BrokenResourceError:
                    # A downstream command may deliberately stop consuming input.
                    pass
                except Exception as error:
                    statuses[index] = 1
                    await emit(Event("error", f"{commands[index].name}: {error}"))

        try:
            async with anyio.create_task_group() as group:
                for index in range(len(commands)):
                    group.start_soon(run_stage, index)
                async with streams[-1][1] as output:
                    async for value in output:
                        await emit(Event("output", value))
        finally:
            for send, receive in streams:
                await send.aclose()
                await receive.aclose()
        return int(any(statuses))  # A failure in any stage fails the pipeline.

    async def complete(self, source: str) -> list[str]:
        """Return whole-line replacements for command names and unquoted paths."""
        await anyio.lowlevel.checkpoint()
        commands, _ = _scan_commands(source, keep_empty_last=True)
        segment = commands[-1]
        segment = segment[segment.rfind("|") + 1 :]
        words = segment.lstrip().split()
        is_command = len(words) <= 1 and not segment.endswith(" ")
        prefix = words[-1] if words and not segment.endswith(" ") else ""
        start = len(source) - len(prefix)
        if prefix and not source.endswith(prefix):
            return []  # A word split by backslash-newline has no contiguous suffix.
        if is_command:
            return [
                source[:start] + name
                for name in sorted(self.commands)
                if name.startswith(prefix)
            ]
        if prefix.startswith("-") and words and words[0] in self.commands:
            return [
                source[:start] + option
                for option in self.commands[words[0]].option_names()
                if option.startswith(prefix)
            ]
        directory, name = posixpath.split(prefix)
        try:
            rows = self.filesystem.listdir(resolve_path(self.cwd, directory or "."))
        except OSError:
            return []
        return [
            source[:start]
            + shlex.quote(
                posixpath.join(directory, row["name"])
                + ("/" if row["type"] == "directory" else "")
            )
            for row in rows
            if row["name"].startswith(name)
            and (
                not words
                or words[0] not in {"cd", "rmdir"}
                or row["type"] == "directory"
            )
        ]
