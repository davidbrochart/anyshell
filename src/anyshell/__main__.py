"""Desktop frontend and headless command-line entry points."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from shutil import get_terminal_size

import anyio

from .engine import Event, Session
from .filesystem import LocalFileSystem
from .presentation import (
    LONG_FILE_HEADERS,
    format_long_line,
    format_value,
    is_long_file_record,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="A portable Python command shell")
    parser.add_argument(
        "--root", type=Path, default=Path.cwd(), help="Workspace directory mapped to /"
    )
    parser.add_argument("--backend", choices=["asyncio", "trio"], default="asyncio")
    parser.add_argument("-c", "--command", help="Execute one command line without a UI")
    parser.add_argument(
        "--prompt-position",
        choices=["inline", "bottom"],
        default="inline",
        help="Interactive prompt position (default: inline)",
    )
    args = parser.parse_args()
    try:
        session = Session(LocalFileSystem(args.root))
    except OSError as error:
        parser.error(str(error))
    if args.command is not None:
        long_header = False

        async def emit(event: Event) -> None:
            nonlocal long_header
            if event.kind == "job":
                job = event.value
                state = (
                    "running" if job.result is None else f"done ({job.result.status})"
                )
                print(f"[{job.id}] {state}  {job.source}", file=sys.stderr)
            if event.kind in {"output", "error"}:
                if event.kind == "output" and is_long_file_record(event.value):
                    if not long_header:
                        print(format_long_line(LONG_FILE_HEADERS))
                    long_header = True
                else:
                    long_header = False
                print(
                    format_value(event.value),
                    file=sys.stderr if event.kind == "error" else sys.stdout,
                )

        async def execute() -> int:
            async with session.background():
                result = await session.execute(
                    args.command, emit, output_width=get_terminal_size((88, 24)).columns
                )
                await session.wait_jobs()
                return result.status

        try:
            status = anyio.run(execute, backend=args.backend)
        except KeyboardInterrupt:
            status = 130
        raise SystemExit(status)
    try:
        from .frontend import ShellApp
    except ModuleNotFoundError as error:
        if error.name != "anytext":
            raise
        parser.error("Install the frontend with: pip install 'anyshell[anytext]'")
    ShellApp(session, prompt_position=args.prompt_position).run(backend=args.backend)


if __name__ == "__main__":
    main()
