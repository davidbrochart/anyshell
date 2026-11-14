# Changelog

## Unreleased

## 0.2.0 — 2026-11-14

- Cancel pending prompt auto-scroll when scrolling up, and preserve manual
  scrollback during later layout or output updates.

- Acquire pager keyboard focus synchronously before reporting readiness, so its
  first navigation key is not lost during the view transition.

- Synchronize prompt cursor positioning and pager readiness with rendered layout,
  and wait for focus restoration in foreground-command tests.

- Open and check all pager files before showing `more` or `less`, reporting
  missing or unreadable files directly without flashing the pager.

- Keep the transcript width during pager teardown so missing-file errors do not
  wrap one character per line.

- Keep the active cursor focused when clicking terminal background, borders,
  prompt labels, or output.

- Highlight selection backgrounds while preserving original text colors and formatting.

- Support mouse selection, highlighting, and copying in the transcript and pager,
  including clipboard writes in the browser demo.

- Remove completed jobs after `jobs` reports them, without reusing job IDs.

- Anchor overflowing inline transcripts at the bottom when restoring the prompt,
  including late layout updates after large listings.

- Restore the terminal cursor to the prompt after long output scrolls the screen.

- Advance to a fresh prompt line when Enter submits empty or whitespace-only input.

- Support sequential commands separated by `;`, including trailing semicolons.

- Keep the foreground waiting cursor blinking while input remains read-only.

- Make `sleep` silent and show a blank cursor line while foreground commands run.

- Hide the prompt during foreground commands and restore it on completion or
  cancellation, keeping foreground and background execution visually distinct.

- Run background commands and pipelines with `&`; track, await, and cancel them
  with `jobs`, `wait`, and `kill` on desktop and in the browser.

- Support `rm -r` directory trees and `rm -f` missing paths, including `rm -rf`.

- Highlight regex search matches in `less`.

- Remove the filename header from `more` and `less`.

- Render transcript and pager text at their available width instead of letting
  Rich measurement wrap wide output at the process console width.

- Format command help at the current output width instead of a fixed 88 columns,
  including `-h`, `--help`, and `help COMMAND`.

- Disable xterm scrollback so the browser terminal does not reserve an unused
  scrollbar gutter; Anyshell retains its own scrollback.

- Use canvas rendering for continuous browser terminal borders and block glyphs
  at different display scales.

- Fit the browser terminal inside its padding and border so the footer remains
  fully visible, including in compact viewports.

- Process file-editor shortcuts in input order so saving includes preceding edits
  and closing the editor does not fall through to the application quit binding.
- Wait for rendered pager content in browser regression tests.
- Advertise pager readiness after rendering, wait for deferred focus and scroll
  updates in UI tests, and retain browser failure screenshots in CI.

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
