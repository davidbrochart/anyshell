# Changelog

## Unreleased

- Process file-editor shortcuts in input order so saving includes preceding edits
  and closing the editor does not fall through to the application quit binding.
- Wait for rendered pager content in browser regression tests.

## 0.1.0 — 2026-11-09

Initial release of Anyshell, a portable Python command shell for desktop and browser use.

- Independent async engine with bounded Python object pipelines, cooperative
  cancellation, and support for asyncio and Trio through AnyIO.
- Local and in-memory workspace filesystems with portable POSIX-style paths.
- Typed Python commands and options parsed with Cyclopts; help and errors are
  displayed inside the shell.
- File operations including `ls`, `cd`, `pwd`, `mkdir`, `rmdir`, `touch`, `write`,
  `rm`, `mv`, `cp`, recursive `cp -r`, and `find`.
- Streaming commands including `echo`, `cat`, `grep`, `where`, `select`, `sort`,
  and `count`.
- Optional Anytext frontend with inline or bottom prompts, scrollback, history,
  and cursor-adjacent completion menus for commands, paths, and options.
- Multiline input with quote, pipe, and backslash continuations and ordered
  execution of newline-separated commands.
- In-shell text editing with save shortcuts, dirty indicators, and confirmation
  before discarding edits.
- `more` and `less` pagers for files and pipeline output, with forward/backward
  regex search in `less`.
- Thread-free Pyodide browser demo and regression coverage for both prompt layouts.
- Native tests on asyncio and Trio, cross-platform CI, package builds, and
  tag-triggered publishing to PyPI through a trusted publisher.
