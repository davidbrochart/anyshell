"""In-process Git commands backed by Dulwich porcelain."""

import os
import sys
from collections.abc import Callable
from contextlib import aclosing
from io import BytesIO, StringIO
from typing import Annotated

from cyclopts import App, Parameter
from rich.text import Text

from .engine import Command, Context, Event, PageRequest, ShellError
from .filesystem import LocalFileSystem

_DESCRIPTION = "Work with Git repositories."
_app = App(name="git", help=_DESCRIPTION, version_flags=())
GitContext = Annotated[Context, Parameter(parse=False)]
GitOutput = Annotated[object, Parameter(parse=False)]


def _local(ctx: Context) -> LocalFileSystem:
    if not isinstance(ctx.fs, LocalFileSystem):
        raise ShellError("requires a local filesystem")
    return ctx.fs


def _run(ctx: Context, command: str | Callable, *args, **kwargs):
    fs = _local(ctx)

    from dulwich import porcelain
    from dulwich.errors import NotGitRepository
    from dulwich.repo import Repo

    operation = getattr(porcelain, command) if isinstance(command, str) else command
    if command == "init":
        with operation(*args, **kwargs):
            return
    cwd = fs._path(ctx.session.cwd)
    ceilings = [fs.root.parent] if fs.root.parent != fs.root else []
    try:
        with Repo.discover(cwd, ceiling_dirs=ceilings) as repo:
            if sys.platform == "emscripten" and command == "commit":
                # Dulwich registers shell hooks even for a newly initialized repo.
                repo.hooks.clear()
            return operation(repo, *args, **kwargs)
    except NotGitRepository as error:
        raise ShellError(
            "no Git repository at or above the current directory"
        ) from error


@_app.command(name="init")
async def init(path: str = ".", *, bare: bool = False, ctx: GitContext):
    """Initialize a repository."""
    _run(ctx, "init", _local(ctx)._path(ctx.resolve(path)), bare=bare)


@_app.command(name="add")
async def add(path: str, *paths: str, ctx: GitContext):
    """Stage files or directories."""
    fs = _local(ctx)
    targets = []
    for name in (path, *paths):
        logical = ctx.resolve(name)
        parent, _, basename = logical.rpartition("/")
        # Resolve the parent so Dulwich can stage the symlink itself.
        targets.append(fs._path(parent) / basename)
    _run(ctx, "add", targets)


def _current_branch(repo):
    from dulwich.refs import HEADREF, LOCAL_BRANCH_PREFIX, SYMREF

    head = repo.refs.read_ref(HEADREF) or b""
    prefix = SYMREF + LOCAL_BRANCH_PREFIX
    return head[len(prefix) :] if head.startswith(prefix) else None


def _upstream_status(repo, branch, head):
    from dulwich.refs import LOCAL_BRANCH_PREFIX, Ref

    config = repo.get_config_stack()
    section = (b"branch", branch)
    try:
        remote = config.get(section, b"remote")
        merge = config.get(section, b"merge")
    except KeyError:
        return None
    if remote == b".":
        upstream = merge
        name = merge.removeprefix(LOCAL_BRANCH_PREFIX)
    else:
        name = remote + b"/" + merge.removeprefix(LOCAL_BRANCH_PREFIX)
        upstream = b"refs/remotes/" + name
    display = os.fsdecode(name)
    try:
        target = repo.refs[Ref(upstream)]
    except KeyError:
        return f"Your branch is based on '{display}', but the upstream is gone."
    if target == head:
        return f"Your branch is up to date with '{display}'."
    ahead = sum(1 for _ in repo.get_walker(include=[head], exclude=[target]))
    behind = sum(1 for _ in repo.get_walker(include=[target], exclude=[head]))
    if ahead and behind:
        return (
            f"Your branch and '{display}' have diverged, and have "
            f"{ahead} and {behind} different commits each, respectively."
        )
    count, direction = (ahead, "ahead of") if ahead else (behind, "behind")
    return f"Your branch is {direction} '{display}' by {count} commit{'s' if count != 1 else ''}."


def _status_lines(repo):
    from dulwich import porcelain

    result = porcelain.status(repo)
    branch = _current_branch(repo)
    try:
        head = repo.head()
    except KeyError:
        head = None
    lines = []
    if branch is not None:
        lines.append(f"On branch {os.fsdecode(branch)}")
        if head is not None and (upstream := _upstream_status(repo, branch, head)):
            lines.append(upstream)
    elif head is not None:
        lines.append(f"HEAD detached at {head[:7].decode('ascii')}")
    if head is None:
        lines.extend(["", "No commits yet"])

    staged = any(result.staged.values())
    if staged:
        lines.extend(["", "Changes to be committed:"])
        for kind, label in (
            ("add", "new file"),
            ("modify", "modified"),
            ("delete", "deleted"),
        ):
            for path in sorted(map(os.fsdecode, result.staged[kind])):
                lines.append(f"\t{label}:   {path}")
    if result.unstaged:
        lines.extend(
            [
                "",
                "Changes not staged for commit:",
                '  (use "git add <file>..." to update what will be committed)',
            ]
        )
        for path in sorted(map(os.fsdecode, result.unstaged)):
            label = (
                "modified"
                if os.path.lexists(os.path.join(repo.path, path))
                else "deleted"
            )
            lines.append(f"\t{label}:   {path}")
    if result.untracked:
        lines.extend(
            [
                "",
                "Untracked files:",
                '  (use "git add <file>..." to include in what will be committed)',
            ]
        )
        lines.extend(f"\t{path}" for path in sorted(map(os.fsdecode, result.untracked)))
    if not staged:
        if result.unstaged:
            lines.extend(
                [
                    "",
                    'no changes added to commit (use "git add" and/or "git commit -a")',
                ]
            )
        elif result.untracked:
            lines.extend(
                [
                    "",
                    'nothing added to commit but untracked files present (use "git add" to track)',
                ]
            )
        elif head is None:
            lines.extend(
                ["", 'nothing to commit (create/copy files and use "git add" to track)']
            )
        else:
            lines.append("nothing to commit, working tree clean")
    return lines


@_app.command(name="status")
async def status(*, ctx: GitContext, stdout: GitOutput):
    """Show the current branch, upstream and working tree changes."""
    for line in _run(ctx, _status_lines):
        await stdout.send(line)


@_app.command(name="commit")
async def commit(
    *,
    message: Annotated[str, Parameter(name=["--message", "-m"])],
    all: Annotated[bool, Parameter(name=["--all", "-a"])] = False,
    ctx: GitContext,
    stdout: GitOutput,
):
    """Commit staged changes with a message."""
    oid = _run(ctx, "commit", message=message, all=all)
    await stdout.send(oid.decode("ascii"))


async def _page_output(ctx, stdout, title, output, *, styled=None):
    job = ctx.session._control
    if output and stdout.is_terminal and (job is None or job.foreground):

        async def lines():
            for line in output if styled is None else styled:
                yield line

        async with aclosing(lines()) as stream:
            request = PageRequest(title, stream, searchable=True)
            await ctx.emit(Event("page", request))
        if request.handled:
            return
    for line in output:
        await stdout.send(line)


@_app.command(name="log")
async def log(
    *,
    max_count: Annotated[int | None, Parameter(name=["--max-count", "-n"])] = None,
    oneline: bool = False,
    ctx: GitContext,
    stdout: GitOutput,
):
    """Show commit history."""
    output = StringIO()
    _run(ctx, "log", outstream=output, max_entries=max_count, oneline=oneline)
    await _page_output(ctx, stdout, "git log", output.getvalue().splitlines())


def _color_diff(lines):
    in_hunk = False
    for line in lines:
        if line.startswith("diff --git "):
            in_hunk = False
        elif line.startswith("@@ "):
            in_hunk = True
        if in_hunk and line.startswith("+"):
            yield Text(line, style="green")
        elif in_hunk and line.startswith("-"):
            yield Text(line, style="red")
        else:
            yield line


@_app.command(name="diff")
async def diff(
    *,
    staged: Annotated[bool, Parameter(name=["--staged", "--cached"])] = False,
    ctx: GitContext,
    stdout: GitOutput,
):
    """Show unstaged or staged changes."""
    output = BytesIO()
    _run(ctx, "diff", staged=staged, outstream=output)
    lines = output.getvalue().decode("utf-8", errors="replace").splitlines()
    await _page_output(ctx, stdout, "git diff", lines, styled=_color_diff(lines))


@_app.command(name="ls-files")
async def ls_files(*, ctx: GitContext, stdout: GitOutput):
    """List tracked files."""
    for path in _run(ctx, "ls_files"):
        await stdout.send(os.fsdecode(path))


def _branch_listing(repo):
    from dulwich import porcelain

    branch = _current_branch(repo)
    return [
        ("* " if branch == ref else "  ") + os.fsdecode(ref)
        for ref in porcelain.branch_list(repo)
    ]


@_app.command(name="branch")
async def branch(name: str | None = None, *, ctx: GitContext, stdout: GitOutput):
    """List branches or create a branch at HEAD."""
    if name is None:
        for line in _run(ctx, _branch_listing):
            if stdout.is_terminal and line.startswith("* "):
                current = Text(line)
                current.stylize("green", 2)
                await stdout.send(current)
                continue
            await stdout.send(line)
    else:
        _run(ctx, "branch_create", name)


@_app.command(name="switch")
async def switch(target: str, *, ctx: GitContext):
    """Switch to an existing branch."""
    _run(ctx, "switch", target)


GIT_COMMAND = Command("git", _DESCRIPTION, _app)
