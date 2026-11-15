"""UI-independent sessions and streaming Python command pipelines."""

from __future__ import annotations

import posixpath
import shlex
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from inspect import BoundArguments, isawaitable
from io import StringIO
from typing import Any, Literal

import anyio
from cyclopts import App as ArgumentParser
from cyclopts import CycloptsError, Parameter
from rich.console import Console

from .filesystem import (
    FileSystem,
    MemoryFileSystem,
    format_filesystem_error,
    resolve_path,
)


class ShellError(Exception):
    """A command or syntax error suitable for display to the shell user."""


@dataclass(frozen=True)
class Event:
    kind: Literal[
        "output", "error", "clear", "cwd", "status", "edit", "page", "job", "exit"
    ]
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
    output_width: int = 88

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

    def prepare(self, arguments: list[str], *, width: int = 88) -> _Invocation:
        output = StringIO()
        console = Console(file=output, width=width, color_system=None)
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

    def help_text(self, *, width: int = 88) -> str:
        output = StringIO()
        self._parser.help_print(
            console=Console(file=output, width=width, color_system=None)
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
    redirections: list[tuple[str, str]] = field(default_factory=list)

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


@dataclass
class Job:
    id: int
    source: str
    result: Result | None = None
    scope: anyio.CancelScope | None = field(default=None, repr=False)
    done: anyio.Event = field(default_factory=anyio.Event, repr=False)


def _pipeline_parts(source: str) -> list[str]:
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
    return parts


def parse_pipeline(source: str) -> list[list[str]]:
    parts = _pipeline_parts(source)
    try:
        stages = [shlex.split(part) for part in parts]
    except ValueError as error:
        raise ShellError(str(error)) from error
    if len(stages) == 1 and not stages[0]:
        return []
    if any(not stage for stage in stages):
        raise ShellError("empty pipeline stage")
    return stages


def _parse_redirections(source: str) -> tuple[list[str], list[tuple[str, str]]]:
    tokens = []
    start = 0
    quote = ""
    escaped = False
    index = 0
    while index < len(source):
        char = source[index]
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in "<>":
            chunk = source[start:index]
            if chunk and chunk[-1].isdigit() and chunk.split()[-1].isdigit():
                raise ShellError("file descriptor redirection is not supported")
            tokens.extend((word, False) for word in shlex.split(chunk))
            operator = char
            if index + 1 < len(source) and source[index + 1] == char:
                operator += char
                index += 1
            if operator == "<<":
                raise ShellError("here-documents are not supported")
            tokens.append((operator, True))
            start = index + 1
        index += 1
    tokens.extend((word, False) for word in shlex.split(source[start:]))
    arguments = []
    redirections = []
    index = 0
    while index < len(tokens):
        token, operator = tokens[index]
        if operator:
            if index + 1 == len(tokens) or tokens[index + 1][1]:
                raise ShellError(f"missing filename after {token}")
            redirections.append((token, tokens[index + 1][0]))
            index += 2
        else:
            arguments.append(token)
            index += 1
    if not arguments:
        raise ShellError("redirection requires a command")
    return arguments, redirections


class _FileInput:
    def __init__(self, lines):
        self.lines = lines

    async def receive(self):
        await anyio.lowlevel.checkpoint()
        try:
            return next(self.lines)
        except StopIteration:
            raise anyio.EndOfStream from None

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.receive()
        except anyio.EndOfStream:
            raise StopAsyncIteration from None


class _FileOutput:
    def __init__(self, filesystem, path):
        self.filesystem = filesystem
        self.path = path

    async def send(self, value):
        from .presentation import format_value

        self.filesystem.append_text(self.path, format_value(value) + "\n")
        await anyio.lowlevel.checkpoint()


def _scan_jobs(
    source: str, *, keep_empty_last: bool = False
) -> tuple[list[tuple[str, bool]], bool]:
    commands: list[tuple[str, bool]] = []
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
        elif char == "&":
            commands.append(("".join(buffer), True))
            buffer.clear()
            pipe = False
        elif char == ";":
            commands.append(("".join(buffer), False))
            buffer.clear()
            pipe = False
        elif char == "\n" and not pipe:
            if "".join(buffer).strip():
                commands.append(("".join(buffer), False))
            buffer.clear()
        else:
            buffer.append(char)
            if not char.isspace():
                pipe = char == "|"
        index += 1
    if "".join(buffer).strip() or keep_empty_last:
        commands.append(("".join(buffer), False))
    return commands, bool(quote or pipe or incomplete)


def _scan_commands(source: str, *, keep_empty_last: bool = False):
    commands, incomplete = _scan_jobs(source, keep_empty_last=keep_empty_last)
    return [text for text, _ in commands], incomplete


def is_complete(source: str) -> bool:
    """Whether Enter can submit, rather than continue an unfinished command."""
    return not _scan_commands(source)[1]


def parse_commands(source: str) -> list[list[list[str]]]:
    """Parse pipelines separated by newlines, ; or &, preserving quoted text."""
    commands, incomplete = _scan_jobs(source)
    if incomplete:
        raise ShellError(
            "unfinished command: close quotes or complete the continuation"
        )
    if any(background and not command.strip() for command, background in commands):
        raise ShellError("empty background command; && is not supported")
    if any(not command.strip() for command, _ in commands):
        raise ShellError("empty command")
    return [parse_pipeline(command) for command, _ in commands]


class Session:
    """A shell session owns commands, history, working directory and cancellation.

    Commands exchange Python values through bounded AnyIO streams. One foreground
    command line may run alongside background jobs owned by background().
    """

    def __init__(self, filesystem: FileSystem | None = None) -> None:
        self.filesystem = filesystem if filesystem is not None else MemoryFileSystem()
        self.cwd = "/"
        self.history: list[str] = []
        self.commands: dict[str, Command] = {}
        self._scope: anyio.CancelScope | None = None
        self.last_status = 0
        self.exit_status: int | None = None
        self._executing = False
        self.jobs: dict[int, Job] = {}
        self._next_job_id = 1
        self._job_group = None
        from .commands import BUILTINS

        for command in BUILTINS:
            self.register(command)

    @asynccontextmanager
    async def background(self):
        """Own background tasks; leaving this context cancels unfinished jobs."""
        if self._job_group is not None:
            raise ShellError("background task context is already open")
        async with anyio.create_task_group() as group:
            self._job_group = group
            try:
                yield self
            finally:
                self._job_group = None
                group.cancel_scope.cancel()

    async def wait_jobs(self, job_id: int | None = None) -> int:
        jobs = list(self.jobs.values()) if job_id is None else [self.get_job(job_id)]
        status = 0
        for job in jobs:
            await job.done.wait()
            status = job.result.status
        return status

    def get_job(self, job_id: int) -> Job:
        try:
            return self.jobs[job_id]
        except KeyError:
            raise ShellError(f"no such job: {job_id}") from None

    async def _start_job(self, source, commands, emit, output_width):
        job = Job(self._next_job_id, source)
        self._next_job_id += 1
        child = Session(self.filesystem)
        child.cwd = self.cwd
        child.last_status = self.last_status
        child.commands = self.commands.copy()
        child.jobs = self.jobs.copy()

        async def output(event):
            if event.kind in {"edit", "page", "clear"}:
                raise ShellError("interactive commands cannot run in the background")
            await emit(event)

        async def run(*, task_status=anyio.TASK_STATUS_IGNORED):
            status = 1
            cancelled = False
            with anyio.CancelScope() as scope:
                job.scope = scope
                task_status.started()
                try:
                    status = await child._pipeline(
                        commands, output, output_width=output_width
                    )
                except anyio.get_cancelled_exc_class():
                    cancelled = True
                    raise
                finally:
                    job.result = Result(130 if cancelled else status, cancelled)
                    job.scope = None
                    job.done.set()
            await emit(Event("job", job))

        self.jobs[job.id] = job
        await emit(Event("job", job))
        await self._job_group.start(run)

    @property
    def running(self) -> bool:
        return self._executing

    def register(self, command: Command) -> None:
        if not command.name or any(
            char.isspace() or char in "|&;" for char in command.name
        ):
            raise ValueError("command names must be nonempty words")
        self.commands[command.name] = command

    def cancel(self) -> bool:
        """Cancel the active execution from the same event loop."""
        if self._scope is None:
            return False
        self._scope.cancel()
        return True

    async def execute(
        self, source: str, emit: Emitter, *, output_width: int = 88
    ) -> Result:
        if self.exit_status is not None:
            raise ShellError("this session has exited")
        if output_width < 1:
            raise ValueError("output_width must be positive")
        if self.running:
            raise ShellError("this session is already running a command")
        source = source.strip() if "\n" not in source else source
        if not source.strip():
            return Result(0)
        self._executing = True
        try:
            return await self._execute(source, emit, output_width=output_width)
        finally:
            self._executing = False

    async def _execute(
        self, source: str, emit: Emitter, *, output_width: int
    ) -> Result:
        if not self.history or self.history[-1] != source:
            self.history.append(source)
        status = 0
        previous_cwd = self.cwd
        with anyio.CancelScope() as scope:
            self._scope = scope
            try:
                pipelines = []
                parsed = parse_commands(source)
                plans, _ = _scan_jobs(source)
                for stages, (text, background) in zip(parsed, plans):
                    if background and self._job_group is None:
                        raise ShellError(
                            "background commands require a Session.background() context"
                        )
                    commands = []
                    for part in _pipeline_parts(text):
                        stage, redirections = _parse_redirections(part)
                        if stage[0] not in self.commands:
                            raise ShellError(f"command not found: {stage[0]}")
                        command = self.commands[stage[0]]
                        invocation = command.prepare(stage[1:], width=output_width)
                        invocation.redirections = redirections
                        if (
                            len(stages) > 1
                            and command.standalone
                            and not invocation.is_help
                        ):
                            raise ShellError(
                                f"{command.name} must run outside a pipeline"
                            )
                        commands.append(invocation)
                    pipelines.append((commands, text, background))
                for commands, text, background in pipelines:
                    if background:
                        await self._start_job(
                            text.strip(), commands, emit, output_width
                        )
                        status = 0
                    else:
                        status = await self._pipeline(
                            commands, emit, output_width=output_width
                        )
                    self.last_status = status
                    if self.exit_status is not None:
                        break
            except (ShellError, OSError, ValueError) as error:
                status = 1
                await emit(
                    Event("error", format_filesystem_error(error, self.filesystem))
                )
            finally:
                self._scope = None
        cancelled = scope.cancel_called
        self.last_status = 130 if cancelled else status
        if self.cwd != previous_cwd:
            await emit(Event("cwd", self.cwd))
        await emit(Event("status", self.last_status))
        if self.exit_status is not None:
            await emit(Event("exit", self.exit_status))
        return Result(self.last_status, cancelled)

    async def _pipeline(
        self, commands, emit: Emitter, *, output_width: int = 88
    ) -> int:
        streams = [
            anyio.create_memory_object_stream[Any](16) for _ in range(len(commands) + 1)
        ]
        await streams[0][0].aclose()  # First stage gets EOF on standard input.
        statuses = [0] * len(commands)
        context = Context(self, emit, output_width=output_width)

        async def run_stage(index: int) -> None:
            receive = streams[index][1]
            send = streams[index + 1][0]
            async with receive, send:
                try:
                    with ExitStack() as stack:
                        stdin, stdout = receive, send
                        for operator, name in commands[index].redirections:
                            path = context.resolve(name)
                            if operator == "<":
                                # Preflight the file even when the command ignores stdin.
                                lines = iter(self.filesystem.read_lines(path))
                                if (close := getattr(lines, "close", None)) is not None:
                                    stack.callback(close)
                                stdin = _FileInput(lines)
                                await receive.aclose()
                            else:
                                if operator == ">":
                                    self.filesystem.write_text(path, "")
                                else:
                                    self.filesystem.append_text(path, "")
                                stdout = _FileOutput(self.filesystem, path)
                        await commands[index].run(context, stdin, stdout)
                except anyio.BrokenResourceError:
                    # A downstream command may deliberately stop consuming input.
                    pass
                except Exception as error:
                    statuses[index] = 1
                    message = format_filesystem_error(error, self.filesystem)
                    await emit(Event("error", f"{commands[index].name}: {message}"))

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
        if self.exit_status is not None:
            return self.exit_status
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
                source[:start] + name + " "
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
