# Anyshell

An async shell whose commands are Python functions. The engine is independent of
its optional Anytext widget: both can run locally on the desktop or in Pyodide.
No shell command starts an OS process or needs a worker thread.

## Run it

```sh
uv sync --extra anytext --extra trio
uv run --extra anytext anyshell
uv run --extra anytext --extra trio anyshell --backend trio
uv run anyshell -c 'ls | where type file | select name | sort'
```

`/` maps to the selected workspace directory, which defaults to your current
working directory. Use `--root /path/to/workspace` to choose another directory.
The session never changes Python's process working directory. Paths use `/` on
all platforms. Local symlinks must resolve inside the workspace; this is a path
boundary, not a sandbox for arbitrary Python commands or untrusted code.

The prompt is inline by default: it follows the latest output in a shared scrollback
area. Typing or completing a command brings it back into view. For a fixed bottom
editor, run `uv run --extra anytext anyshell --prompt-position bottom` or construct
`Shell(session, prompt_position="bottom")`. Both `Shell` and `ShellApp` accept
`prompt_position="inline"` or `prompt_position="bottom"`.

The widget provides a scrollable transcript, Up/Down history, Tab completion for
command names, options, and unquoted paths, Ctrl+C cancellation, and Ctrl+Q to quit the demo.
Drag across command output to select text. Ctrl+C copies selected output when no
foreground command is running; Ctrl+Shift+C copies it while a command is running.
Mouse selection and copying also work in the pager. Browser copying uses the
browser clipboard when permission is available.

The prompt is replaced by a blank cursor line while a foreground command runs
and returns when it finishes
or is cancelled with Ctrl+C. Background commands return the prompt immediately.
Tab first extends the shared prefix when several completions match. If no more
characters can be filled in, Tab opens a menu beside the cursor. Use Up/Down or
Tab to select, Enter to insert, and Escape to dismiss; typing also dismisses it.
Long choices remain on one line and can be read using the horizontal scrollbar.

## Multiline commands

Shift+Enter inserts a newline without running the command. Enter submits a
complete command, or inserts a continuation line when a quote is still open or
the command ends with a pipe or backslash. Ctrl+O also inserts a newline on
terminals that cannot distinguish Shift+Enter. Ctrl+Enter forces submission,
including syntax errors. The editor grows up to eight rows, then scrolls.

```sh
echo 'hello
world'
cat notes.txt |
    grep Python |
    count
```

Unquoted newlines and semicolons separate commands, which execute in order.
For example, `sleep 1; sleep 1` waits twice in sequence. A backslash followed
by a newline joins lines without inserting a character. Newlines inside quotes
remain part of the argument. All commands are parsed and validated before any
runs; a runtime failure does not prevent later commands from running, and the
final status is that of the last command. Ctrl+C cancels the active sequence or
clears unfinished input. History keeps a multiline submission as one entry.
Up/Down moves within a multiline editor; Alt+Up/Down navigates command history.
The same syntax works through `Session.execute()` and headless `-c` execution.

## Commands and pipelines

```sh
help
ls
ls -la
help ls
ls | where type file | select name | sort
cat README.md | grep Python | count
echo 'a quoted | character'
mkdir notes
touch empty.txt
rmdir notes
mkdir notes
cd notes
write hello.txt 'Hello from Python'
cat hello.txt
edit hello.txt
cp hello.txt backup.txt
mv backup.txt renamed.txt
rm hello.txt
cd /
sleep 30
clear
```

Pipelines exchange Python values through bounded AnyIO streams. `ls` emits
records, displayed as names in columns in the widget (directories have a trailing
`/` and blue styling). Headless output lists one name per line. Pipeline values
remain records; `select` extracts fields; `count` emits an integer. `cat` and `grep`
stream text lines. `sort` buffers its input. A failure in any stage fails the
pipeline. Filesystem mutations, `cd`, and `clear` run outside pipelines.

`ls -l` (`--long`) displays type, byte size, modification time in UTC, and name.
Its pipeline records include `size` and `modified` (Unix seconds); directory size
is `None`. `ls -a` (`--all`) includes hidden entries, and flags combine as `-la`.
`grep -i` ignores case and `sort -r` reverses the order.
`touch PATH...` creates missing files or updates modification times without
changing existing contents. Existing directories can also be touched.
`rm PATH...` removes files, continuing with remaining paths if one fails.
`rm -r DIRECTORY...` removes directory trees, including hidden entries.
`rm -f` ignores missing paths and accepts no operands; combine the flags as
`rm -rf DIRECTORY...`. Force does not suppress other errors. Recursive removal
does not follow symbolic links and cannot remove the workspace root.
`rmdir DIRECTORY...` removes empty directories and rejects files and nonempty
directories; its completion suggests only directories.

This is an initial shell language: quoting, multiline commands, and pipes are supported. Bash scripts,
redirection, environment expansion, glob expansion, process execution, foregrounding or suspending jobs, and persistent history
are not implemented. Commands are trusted Python
code, not a sandbox. Browser filesystem changes are in-memory and reset on reload.

## Background commands

Append `&` to run a command or pipeline in the background and return to the prompt:

```sh
sleep 30 &
cat notes.txt | grep Python &
jobs
wait 1
kill 1
```

`jobs` lists job IDs and states. `wait ID` waits for a job; `wait` waits for all
jobs still in the job list. `kill ID` cancels a job cooperatively. Ctrl+C cancels the
foreground command (including `wait`), leaving background jobs running. Job
output appears in the transcript, with launch and completion notifications.
Completed jobs are shown once by `jobs`, then removed from the job list.
Job IDs are not reused during the session.

An unquoted `&` also separates commands: `sleep 30 & echo ready`. Quote or escape
it to pass a literal ampersand. `&&` is not supported. Each job inherits the
working directory and command registry at launch and shares the filesystem;
changing directory in one job does not affect the prompt or other jobs.
Interactive commands such as `edit`, `more`, and `less` cannot run in the
background. Closing the shell cancels unfinished jobs. Headless `-c` execution
waits for background jobs before exiting; its exit status is the last submitted
foreground command's status (or zero for a background launch).

Engine callers provide a lifetime for background tasks explicitly:

```python
async with session.background():
    await session.execute("sleep 1 &", receive)
    await session.wait_jobs()
```

Leaving this context cancels unfinished jobs. `Session.jobs` maps IDs to `Job`
objects with `source`, `result`, and `done`; launch and completion events have
kind `job`. Background completion does not change `Session.last_status`.

## Find files

```sh
find
find docs -name '*.txt'
find . -iname '*.py' -type f
find . -type d -maxdepth 2
find docs other-directory -type f | select path
find . -type f | count
```

`find [PATH...]` walks starting paths recursively, including the starting entries
and hidden names. It defaults to `.`. `-name`/`--name` matches a basename using
shell-style wildcard patterns; `-iname`/`--iname` ignores letter case.
`-type`/`--type` accepts `f` (file), `d` (directory), or `l` (symbolic link).
`-maxdepth`/`--max-depth` limits traversal, with starting paths at depth zero.
Filters combine with AND. Quote wildcard patterns to pass them unchanged.

Output records contain `name` (basename), `path` (relative to the current working
directory, or absolute when the starting path is absolute), and `type` (`file`,
`directory`, or `symlink`). The UI displays paths with the usual directory suffix;
`select path` extracts plain paths. Symbolic links, including starting paths,
are listed without following them. Errors are reported while traversal continues
with other entries and starting paths; any error makes the command fail.
Custom filesystem implementations provide `entry_type()` without following the
final symbolic link, alongside the existing directory-listing methods. Filesystems supporting recursive
removal also accept `remove(path, recursive=True)`.

## Move and copy files

```sh
mv old.txt new.txt
mv notes archive
cp file.txt backup.txt
cp file.txt existing-directory/
cp -r notes notes-backup
```

`mv SOURCE DESTINATION` moves or renames a file or a whole directory.
`cp SOURCE DESTINATION` copies a file; `cp -r` (or `--recursive`) also copies
whole directory trees, including hidden entries. If the destination is an
existing directory, the source keeps its name inside it. Existing files are
replaced; existing directory trees are not merged or replaced. Contents and
modification times are preserved. Both commands accept one source and one
destination and run outside pipelines. Moving the current directory or one of
its ancestors updates the session's working path.

## Page through text

```sh
more README.md
more first.txt second.txt
cat notes.txt | grep Python | more
ls -l | more
```

`more` fills the Shell widget with a read-only, scrollable view, then restores the
transcript and prompt when closed. Space/Page Down moves forward a page;
`b`/Page Up moves back. Enter/Down advances one line, Up moves back one line,
and Home/End jumps to the beginning/end. `q`, Ctrl+Q, or Escape closes the pager.
Mouse scrolling also works. Shortcuts appear in the footer.

The pager accepts files or pipeline input, formatting Python values for display.
File arguments are opened and checked before the pager appears; missing or
unreadable files report an error directly in the shell.
It streams input into scrollback; closing it stops consuming the pipeline.
Headless execution reports that an interactive frontend is required. Other
frontends can handle the `page` event's `PageRequest`, consume its `lines`, mark
it `handled`, and return when their pager closes.

## Search with less

```sh
less README.md
less first.txt second.txt
cat notes.txt | grep Python | less
```

`less` uses the same in-shell pager and navigation shortcuts as `more`, adding
Python regex search. Press `/` to search forward or `?` to search backward,
then enter a pattern and press Enter. `n` finds the next match in that direction;
Shift+N finds the previous one. Searches wrap around the text. An empty search
reuses the last pattern, and Escape cancels the search field. Invalid patterns
and missing matches appear as notifications. The title shows the matching line.
The search field disappears after submission, leaving no permanent bottom line.

## Edit files

`edit PATH` opens a plain text editor that fills the Shell widget, restoring the
transcript and prompt when closed. It loads an
existing UTF-8 file or starts empty for a new file. Ctrl+S saves without closing;
Ctrl+Q closes the editor and returns to the shell. Modified files show `*` beside the path. Closing with unsaved
changes updates the footer shortcuts: a second Ctrl+Q discards and closes (Ctrl+D
also works), Ctrl+S saves,
and Escape keeps editing. Save errors appear as notifications and leave the
editor and its text open. The title marks unsaved edits with `*`; shortcuts appear
in the app footer. The editor has no bottom status line or button rows. When embedding in
another Anytext app, give its app-wide Ctrl+Q quit binding `priority=False` so
the focused editor can handle that key first, as `ShellApp` does.

The editor uses the session filesystem on desktop and in the thread-free browser.
New files are created when saved. Browser files remain in memory until reload.
Headless `-c` execution reports that an interactive frontend is required; `edit`
cannot run in a pipeline. Other frontends can handle the `edit` event's
`EditRequest`, mark it `handled`, and return after their editor closes.

## Use the engine

```python
import anyio
from anyshell import Event, MemoryFileSystem, Session

session = Session(MemoryFileSystem({"hello.txt": "hello\nworld\n"}))


async def receive(event: Event):
    if event.kind == "output":
        print(event.value)


async def main():
    await session.execute("cat hello.txt | count", receive)


anyio.run(main)
```

Help uses the current transcript or terminal width. Engine callers can set
`Session.execute(..., output_width=100)` when supplying their own frontend.

Commands use Cyclopts to parse typed Python parameters. `--help` (or `-h`)
and `help COMMAND` write help to the transcript. Invalid arguments produce shell
errors without exiting the app. All pipeline arguments are validated before any
stage starts. Use `--` before a filename beginning with a dash.

Register a `Command` with a synchronous or asynchronous function. Parameters
named `ctx` (or `context`), `stdin`, and `stdout` are injected by the engine and
must be keyword-only. Other parameters become command arguments and options.
Input and output are AnyIO object streams; a returned value is also emitted as
one pipeline value. Command functions should stay cooperative for cancellation.
`Session.cancel()` must be called from the session's event loop.

```python
from typing import Annotated

from cyclopts import Parameter
from anyshell import Command


async def upper(
    *,
    prefix: Annotated[str, Parameter(name=["--prefix", "-p"])] = "",
    stdin,
    stdout,
):
    async for value in stdin:
        await stdout.send(prefix + str(value).upper())


session.register(Command("upper", "Uppercase input values", upper))
# Now: echo hello | upper -p '> '
```

`ctx.fs` exposes the filesystem and `ctx.emit` emits frontend events. Parsing
and help never expose the injected parameters as command options.

For the frontend, import `Shell` from `anyshell.frontend` and yield
`Shell(session)` in any Anytext app. History and working directory live in the
session, so the engine and UI can evolve independently.

## Browser demo

The deployment workflow publishes the demo to https://davidbrochart.github.io/anyshell/.
GitHub Actions builds and deploys it from `main` using
`.github/workflows/pages.yml`; it can also be deployed manually from Actions.

To run it locally:

```sh
uv run python tools/build_browser.py
uv run python -m http.server --directory .cache/browser-site 8000
```

Open http://localhost:8000. The Prompt selector switches between inline and bottom
layouts by reloading the demo (`?prompt=inline` or `?prompt=bottom`). The demo downloads Pyodide and Anytext from public
package servers and installs this checkout's Anyshell wheel. It uses an in-memory
workspace and disables threads. Internet access is needed on first load.

## Development

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

For the real browser regression test:

```sh
uv run python tools/build_browser.py
npm ci --prefix tests/browser
cd tests/browser
npx playwright install chromium
npm test
```

Async engine and widget tests run on both asyncio and Trio. The engine depends
on AnyIO and Cyclopts; install `anyshell[anytext]` for the UI and `anyshell[trio]` for Trio.

The browser terminal scaffolding is adapted from Anytext's MIT-licensed browser demo.

## Releasing

Set `project.version` in `pyproject.toml`, update the lockfile with `uv lock`, and
commit the release changes. Push a matching version tag, without a `v` prefix:

```sh
git tag 0.1.0
git push origin 0.1.0
```

The `release.yml` workflow checks that the tag matches the package version,
builds and validates the wheel and source distribution, then publishes them to
PyPI using the configured trusted publisher. No PyPI API token is needed.
