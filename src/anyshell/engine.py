"""UI-independent sessions and streaming Python command pipelines."""

from __future__ import annotations

import keyword
import posixpath
import re
import shlex
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, ExitStack, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from inspect import BoundArguments, isawaitable
from io import StringIO
from typing import Any, Literal

import anyio
from cyclopts import App as ArgumentParser
from cyclopts import CycloptsError, Parameter
from rich.console import Console
from rich.text import Text

from . import scripting
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
        "output",
        "error",
        "clear",
        "cwd",
        "status",
        "edit",
        "page",
        "view",
        "job",
        "exit",
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
    lines: AsyncIterator[str | Text]
    searchable: bool = False
    handled: bool = False


Emitter = Callable[[Event], Awaitable[None]]


@dataclass
class ViewRequest:
    """An interactive widget supplied by a command-owned async context manager.

    open(host_app) owns services for the widget's lifetime. The widget exposes
    an AnyIO closed event. Frontends mount it, wait for closure, then remove it.
    """

    open: Callable[[Any], AbstractAsyncContextManager[Any]]
    handled: bool = False


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

    async def open_view(self, factory):
        """Ask the frontend to host an interactive view and its services."""
        request = ViewRequest(factory)
        await self.emit(Event("view", request))
        if not request.handled:
            raise ShellError("interactive views require an Anytext frontend")

    async def checkpoint(self) -> None:
        """Yield to cancellation and wait while this command's job is stopped."""
        if self.session._control is not None:
            await self.session._control.checkpoint()
        else:
            await anyio.lowlevel.checkpoint()

    async def sleep(self, seconds: float) -> None:
        """Sleep for running time, excluding time spent stopped."""
        if self.session._control is None:
            await anyio.sleep(seconds)
        else:
            await self.session._control.sleep(seconds)


@dataclass(frozen=True)
class Command:
    """A typed Python command; ctx/context, stdin and stdout are injected by name.

    Injected parameters must be keyword-only. Other parameters are parsed by
    Cyclopts, including Annotated option aliases and validators. A Cyclopts App
    can be supplied instead of a function to expose a complete command tree;
    its injected parameters must use Parameter(parse=False).
    """

    name: str
    description: str
    run: Callable[..., Any] | ArgumentParser
    standalone: bool = False
    _parser: ArgumentParser = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if isinstance(self.run, ArgumentParser):
            object.__setattr__(self, "_parser", self.run)
            return
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
        console = Console(
            file=output,
            width=width,
            color_system="truecolor",
            force_terminal=True,
            no_color=False,
        )
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

    def option_names(self, arguments=()) -> list[str]:
        _, apps, _ = self._parser.parse_commands(arguments)
        app = apps[-1]
        names = set(app.help_flags)
        with self._parser.app_stack(apps):
            if app.default_command is not None:
                for argument in app.assemble_argument_collection():
                    if argument.parse and argument.show:
                        names.update(argument.names)
        return sorted(name for name in names if name.startswith("-"))

    def subcommand_names(self, arguments=()) -> list[str]:
        _, apps, unused = self._parser.parse_commands(arguments)
        if unused:
            return []
        app = apps[-1]
        return sorted(
            name for name in app if not name.startswith("-") and app[name].show
        )

    def help_text(self, arguments=(), *, width: int = 88) -> str:
        return self.help_display(arguments, width=width).plain

    def help_display(self, arguments=(), *, width: int = 88) -> Text:
        """Capture Cyclopts' chosen help presentation without printing globally."""
        output = StringIO()
        self._parser.help_print(
            arguments,
            console=Console(
                file=output,
                width=width,
                color_system="truecolor",
                force_terminal=True,
                no_color=False,
            ),
        )
        return Text.from_ansi(output.getvalue().rstrip())


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
        await context.checkpoint()
        if result is not None:
            await stdout.send(result)
        # Help uses a per-invocation console, never a global stdout redirect.
        help_output = Text.from_ansi(self.help_output.getvalue())
        if stdout.is_terminal and help_output:
            for line in help_output.split("\n"):
                await stdout.send(line)
        else:
            for line in help_output.plain.splitlines():
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
    stopped: bool = False
    foreground: bool = False
    retain_state: bool = False
    cancel_requested: bool = False
    session: Session | None = field(default=None, repr=False)
    changed: anyio.Event = field(default_factory=anyio.Event, repr=False)
    _sleepers: dict[anyio.CancelScope, float | None] = field(
        default_factory=dict, repr=False
    )

    @property
    def state(self) -> str:
        if self.result is not None:
            return f"done ({self.result.status})"
        return "stopped" if self.stopped else "running"

    def notify(self) -> None:
        self.changed.set()
        self.changed = anyio.Event()

    def stop(self) -> None:
        self.stopped = True
        for scope in self._sleepers:
            if not scope.cancel_called:
                self._sleepers[scope] = anyio.current_time()
                scope.cancel()
        self.notify()

    def resume(self) -> None:
        self.stopped = False
        self.notify()

    async def checkpoint(self) -> None:
        await anyio.lowlevel.checkpoint()
        while self.stopped:
            await self.changed.wait()

    async def sleep(self, seconds: float) -> None:
        remaining = seconds
        while True:
            await self.checkpoint()
            with anyio.CancelScope() as scope:
                self._sleepers[scope] = None
                start = anyio.current_time()
                try:
                    await anyio.sleep(remaining)
                finally:
                    stopped_at = self._sleepers.pop(scope)
                    end = anyio.current_time() if stopped_at is None else stopped_at
                    remaining = max(0, remaining - (end - start))
            if not scope.cancel_called:
                return


class _ControlledStream:
    """Pause before consuming input or producing output, including redirections."""

    def __init__(self, stream, context, *, is_terminal=False):
        self.stream = stream
        self.context = context
        self.is_terminal = is_terminal

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def __aiter__(self):
        return self

    async def __aenter__(self):
        await self.stream.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self.stream.__aexit__(*args)

    async def __anext__(self):
        try:
            return await self.receive()
        except anyio.EndOfStream:
            raise StopAsyncIteration from None

    async def receive(self):
        await self.context.checkpoint()
        value = await self.stream.receive()
        await self.context.checkpoint()
        return value

    async def send(self, value):
        await self.context.checkpoint()
        await self.stream.send(value)


class _CommandStatus(Exception):
    def __init__(self, status):
        self.status = status


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


def is_complete(source: str, commands=None) -> bool:
    """Whether Enter can submit, rather than continue an unfinished statement."""
    if commands is None:
        from .commands import BUILTINS

        commands = {command.name: command for command in BUILTINS}
    return scripting.is_complete(source, commands)


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


class _ScriptExit(BaseException):
    """Stop the whole script after the shell's exit command."""


_script_runtime: ContextVar = ContextVar("anyshell_script_runtime")


class _RuntimeProxy:
    # Functions defined in earlier inputs retain their globals. Resolve the
    # runtime per task so a resumed script cannot use another input's emitter.
    def __getattr__(self, name):
        return getattr(_script_runtime.get(), name)


class _ScriptRuntime:
    def __init__(self, session, emit, output_width, is_terminal):
        self.session = session
        self.emit = emit
        self.output_width = output_width
        self.is_terminal = is_terminal

    async def checkpoint(self):
        if self.session._control is not None:
            await self.session._control.checkpoint()
        else:
            await anyio.lowlevel.checkpoint()

    async def show(self, value):
        if value is not None:
            await self.emit(Event("output", value))

    async def lookup(self, name):
        if name not in self.session.commands:
            if name in self.session._namespace:
                return await self.show(self.session._namespace[name])
            builtins = self.session._namespace["__builtins__"]
            if name in builtins:
                return await self.show(builtins[name])
        await self.run(name, name, "", (), capture=False)

    async def run(self, source, masked, marker, values, *, capture):
        pattern = re.compile(re.escape(marker) + r"(\d+)__") if values else None

        def resolve(word):
            if pattern is None:
                return word
            return pattern.sub(lambda match: str(values[int(match[1])]), word)

        plans = self.session._prepare_pipelines(masked, self.output_width, resolve)
        if capture and any(background for _, _, background in plans):
            raise ShellError("cannot capture background commands")
        captured = []
        errors = []

        async def collect(event):
            if event.kind == "output":
                captured.append(event.value)
            elif event.kind == "error":
                errors.append(event.value)
            elif event.kind in {"edit", "page", "view", "clear"}:
                raise ShellError("interactive commands cannot be captured")
            else:
                await self.emit(event)

        for commands, _, background in plans:
            if background:
                await self.session._start_job(
                    source.rstrip().removesuffix("&").rstrip(),
                    commands,
                    self.emit,
                    self.output_width,
                    self.is_terminal,
                )
                status = 0
            else:
                status = await self.session._pipeline(
                    commands,
                    collect if capture else self.emit,
                    output_width=self.output_width,
                    is_terminal=self.is_terminal and not capture,
                )
            self.session.last_status = status
            if self.session.exit_status is not None:
                raise _ScriptExit
            if capture and status:
                raise ShellError("\n".join(errors) or "captured command failed")
        return captured if capture else None


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
        self._namespace = scripting.Namespace(lambda: self.commands)
        dict.__setitem__(self._namespace, "__anyshell__", _RuntimeProxy())
        self._scope: anyio.CancelScope | None = None
        self.last_status = 0
        self.exit_status: int | None = None
        self._executing = False
        self.jobs: dict[int, Job] = {}
        self._next_job_id = 1
        self._job_group = None
        self._owner = self
        self._control: Job | None = None
        self._foreground_job: Job | None = None
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
            return self._owner.jobs[job_id]
        except KeyError:
            raise ShellError(f"no such job: {job_id}") from None

    def _child_session(self, *, foreground=False):
        child = Session(self.filesystem)
        child.cwd = self.cwd
        child.last_status = self.last_status
        child.commands = self.commands.copy()
        child._owner = self._owner
        child._job_group = self._owner._job_group
        if foreground:
            child.commands = self.commands
            child._namespace = self._namespace
            child.history = self.history
            child.jobs = self._owner.jobs
        else:
            child._namespace.update(self.variables)
            child.jobs = self._owner.jobs.copy()
        return child

    def _register_job(self, job):
        owner = self._owner
        if not job.id:
            job.id = owner._next_job_id
            owner._next_job_id += 1
            owner.jobs[job.id] = job

    async def _launch(self, job, function, emit):
        async def run(*, task_status=anyio.TASK_STATUS_IGNORED):
            result = Result(1)
            with anyio.CancelScope() as scope:
                job.scope = scope
                if job.cancel_requested:
                    scope.cancel()
                task_status.started()
                try:
                    result = await function()
                except anyio.get_cancelled_exc_class():
                    result = Result(130, True)
                    raise
                except Exception as error:
                    await emit(Event("error", str(error)))
                finally:
                    job.result = result
                    job.scope = None
                    job.done.set()
                    job.notify()
            if job.id and not job.foreground:
                await emit(Event("job", job))

        await self._owner._job_group.start(run)

    async def _start_job(self, source, commands, emit, output_width, is_terminal):
        job = Job(0, source)
        child = self._child_session()
        child._control = job
        job.session = child

        async def output(event):
            if event.kind in {"edit", "page", "view", "clear"} and not job.foreground:
                raise ShellError("interactive commands cannot run in the background")
            await emit(event)

        async def pipeline():
            status = await child._pipeline(
                commands, output, output_width=output_width, is_terminal=is_terminal
            )
            return Result(status)

        self._register_job(job)
        await emit(Event("job", job))
        await self._launch(job, pipeline, emit)

    def select_job(self, job_id=None, *, stopped_only=False):
        if job_id is not None:
            job = self._owner.get_job(job_id)
            if job.result is not None:
                raise ShellError(f"job has finished: {job.id}")
            if stopped_only and not job.stopped:
                raise ShellError(f"job is already running: {job.id}")
            return job
        for job in reversed(list(self._owner.jobs.values())):
            if job.result is None and (not stopped_only or job.stopped):
                return job
        raise ShellError("no stopped job" if stopped_only else "no current job")

    def suspend(self) -> bool:
        """Stop the foreground execution at its next cooperative checkpoint."""
        owner = self._owner
        job = owner._foreground_job
        if job is None or job.result is not None:
            return False
        owner._register_job(job)
        job.stop()
        return True

    async def _wait_foreground(self, job, emit):
        owner = self._owner
        previous = owner._foreground_job
        owner._foreground_job = job
        job.foreground = True
        try:
            while job.result is None and not job.stopped:
                await job.changed.wait()
            if job.retain_state and job.session is not None:
                self.cwd = job.session.cwd
                self.exit_status = job.session.exit_status
            if job.result is None:
                await emit(Event("job", job))
                return Result(148)
            return job.result
        finally:
            job.foreground = False
            owner._foreground_job = previous

    async def foreground_job(self, job_id, emit):
        job = self.select_job(job_id)
        job.resume()
        return await self._wait_foreground(job, emit)

    @property
    def running(self) -> bool:
        return self._executing

    @property
    def variables(self) -> dict[str, Any]:
        """A snapshot of the session's Python variables, without shell internals."""
        return {
            name: value
            for name, value in self._namespace.items()
            if name not in scripting.INTERNAL_NAMES
        }

    def register(self, command: Command) -> None:
        if not command.name or any(
            char.isspace() or char in "|&;" for char in command.name
        ):
            raise ValueError("command names must be nonempty words")
        if keyword.iskeyword(command.name) or command.name in scripting.INTERNAL_NAMES:
            raise ValueError(f"reserved shell name: {command.name}")
        if command.name in self._namespace:
            raise ValueError(f"command name conflicts with variable {command.name!r}")
        self.commands[command.name] = command

    def cancel(self) -> bool:
        """Cancel the active execution from the same event loop."""
        job = self._owner._foreground_job
        if job is not None and job.result is None:
            job.cancel_requested = True
            if job.session is not None and job.session._scope is not None:
                job.session._scope.cancel()
            elif job.scope is not None:
                job.scope.cancel()
            return True
        if self._scope is None:
            return False
        self._scope.cancel()
        return True

    async def execute(
        self,
        source: str,
        emit: Emitter,
        *,
        output_width: int = 88,
        is_terminal: bool = False,
    ) -> Result:
        """Execute source, optionally allowing terminal presentation.

        Commands see stdout.is_terminal only on the final, unredirected stage
        when is_terminal is true. Assignments always capture raw output.
        """
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
            if self._job_group is not None:
                child = self._child_session(foreground=True)
                job = Job(0, source, foreground=True, retain_state=True, session=child)
                child._control = job
                self._foreground_job = job
                try:
                    await self._launch(
                        job,
                        lambda: child._execute(
                            source,
                            emit,
                            output_width=output_width,
                            is_terminal=is_terminal,
                        ),
                        emit,
                    )
                    result = await self._wait_foreground(job, emit)
                    self.last_status = result.status
                    if job.result is None:
                        await emit(Event("status", result.status))
                    return result
                except BaseException:
                    if job.scope is not None:
                        job.scope.cancel()
                    raise
                finally:
                    self._foreground_job = None
            return await self._execute(
                source, emit, output_width=output_width, is_terminal=is_terminal
            )
        finally:
            self._executing = False

    async def _execute(
        self, source: str, emit: Emitter, *, output_width: int, is_terminal: bool
    ) -> Result:
        if not self.history or self.history[-1] != source:
            self.history.append(source)
        status = 0
        previous_cwd = self.cwd
        runtime_token = _script_runtime.set(
            _ScriptRuntime(self, emit, output_width, is_terminal)
        )
        with anyio.CancelScope() as scope:
            self._scope = scope
            try:
                script = scripting.compile_script(
                    source, self.commands, self._namespace
                )
                if script.simple:
                    pipelines = self._prepare_pipelines(source, output_width)
                    for commands, text, background in pipelines:
                        if background:
                            await self._start_job(
                                text.strip(), commands, emit, output_width, is_terminal
                            )
                            status = 0
                        else:
                            status = await self._pipeline(
                                commands,
                                emit,
                                output_width=output_width,
                                is_terminal=is_terminal,
                            )
                        self.last_status = status
                        if self.exit_status is not None:
                            break
                else:
                    self.last_status = 0
                    result = eval(script.code, self._namespace, self._namespace)
                    if isawaitable(result):
                        await result
                    status = self.last_status
            except _ScriptExit:
                status = self.exit_status
            except Exception as error:
                status = 1
                await emit(
                    Event("error", format_filesystem_error(error, self.filesystem))
                )
            finally:
                _script_runtime.reset(runtime_token)
                self._scope = None
        cancelled = scope.cancel_called
        self.last_status = 130 if cancelled else status
        if self.cwd != previous_cwd:
            await emit(Event("cwd", self.cwd))
        await emit(Event("status", self.last_status))
        if self.exit_status is not None:
            await emit(Event("exit", self.exit_status))
        return Result(self.last_status, cancelled)

    def _prepare_pipelines(self, source, output_width, resolve=None):
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
                if resolve is not None:
                    stage = [resolve(word) for word in stage]
                    redirections = [
                        (operator, resolve(path)) for operator, path in redirections
                    ]
                if stage[0] not in self.commands:
                    raise ShellError(f"command not found: {stage[0]}")
                command = self.commands[stage[0]]
                invocation = command.prepare(stage[1:], width=output_width)
                invocation.redirections = redirections
                if len(stages) > 1 and command.standalone and not invocation.is_help:
                    raise ShellError(f"{command.name} must run outside a pipeline")
                commands.append(invocation)
            pipelines.append((commands, text, background))
        return pipelines

    async def _pipeline(
        self,
        commands,
        emit: Emitter,
        *,
        output_width: int = 88,
        is_terminal: bool = False,
    ) -> int:
        async def emit_context(event):
            if event.kind == "view" and (
                len(commands) > 1 or any(command.redirections for command in commands)
            ):
                raise ShellError("interactive views cannot be piped or redirected")
            await emit(event)

        context = Context(self, emit_context, output_width=output_width)
        await context.checkpoint()
        streams = [
            anyio.create_memory_object_stream[Any](16) for _ in range(len(commands) + 1)
        ]
        await streams[0][0].aclose()  # First stage gets EOF on standard input.
        statuses = [0] * len(commands)

        async def run_stage(index: int) -> None:
            receive = streams[index][1]
            send = streams[index + 1][0]
            async with receive, send:
                try:
                    await context.checkpoint()
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
                        await context.checkpoint()
                        await commands[index].run(
                            context,
                            _ControlledStream(stdin, context),
                            _ControlledStream(
                                stdout,
                                context,
                                is_terminal=(
                                    is_terminal
                                    and index == len(commands) - 1
                                    and stdout is send
                                ),
                            ),
                        )
                except _CommandStatus as error:
                    statuses[index] = error.status
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
                        await context.checkpoint()
                        await emit(Event("output", value))
        finally:
            for send, receive in streams:
                await send.aclose()
                await receive.aclose()
        if self.exit_status is not None:
            return self.exit_status
        return next((status for status in reversed(statuses) if status), 0)

    async def complete(self, source: str) -> list[str]:
        """Return whole-line replacements for command names and unquoted paths."""
        await anyio.lowlevel.checkpoint()
        commands, _ = _scan_commands(source, keep_empty_last=True)
        segment = commands[-1]
        segment = segment[segment.rfind("|") + 1 :]
        segment = scripting.completion_segment(segment)
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
                for option in self.commands[words[0]].option_names(words[1:-1])
                if option.startswith(prefix)
            ]
        if words and words[0] in self.commands:
            arguments = words[1:] if segment.endswith(" ") else words[1:-1]
            matches = [
                source[:start] + name + " "
                for name in self.commands[words[0]].subcommand_names(arguments)
                if name.startswith(prefix)
            ]
            if matches:
                return matches
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
