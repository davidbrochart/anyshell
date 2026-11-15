# Changelog

## Unreleased

## 0.3.0 — 2026-11-15

- Require the published Anytext 9.2 release for desktop and browser UI features,
  replacing the default editable sibling checkout.

- Add `tail` with `-n`, file and pipeline input, and cancellable `tail -f`
  following on local and in-memory filesystems, including truncation handling.

- Add async `fs.open()` read handles with independent cursors and latched change
  notifications. `tail -f` waits for Anyshell filesystem mutations without
  polling, OS watchers, or extra threads.

- Resize splits by dragging shell borders and shared corners, or with
  Alt+right-drag over pane content. Swap live panes with Alt+left-drag,
  preserving session state and keyboard navigation after swaps.

- Toggle split divider lines with Alt+D while retaining invisible drag handles.

- Focus neighboring panes spatially with Alt+Up/Down/Left/Right, and use
  Ctrl+Up/Down for whole-command history in the multiline editor.

- Add `exit [STATUS]` to close the issuing pane, quit the last pane, or end a
  headless session, cancelling its background jobs and skipping later commands.

- Use Anytext's `Widget.reparent()` for live shell panes, removing placeholder
  widgets and manual pane positioning. Allow browser builds to bundle a local
  Anytext checkout with `--anytext-source`.

- Add nested side-by-side and stacked shell panes with Anytext `SplitPane`,
  draggable dividers, pane navigation and closing, and independent sessions over
  a shared filesystem. Preserve live commands and drafts across layout changes.

- Add a Ctrl+R history-search popup with live substring matching, arrow navigation,
  accepting a command for editing, and cancellation that restores the draft.

- Support `>`, `>>`, and `<` file redirections in commands, pipelines, and
  background jobs on local and in-memory filesystems.

- Add `ln -s` / `ln --symbolic` with relative, absolute workspace, directory, and
  dangling symbolic links in both local and in-memory filesystems.

- Display `find` results as one path per line, preserving structured pipeline
  records and keeping `ls` output in columns.

## 0.2.1 — 2026-11-14

- Append a space after completed command names, ready for arguments.

- Preserve the current job list in background commands so `jobs &` lists
  existing running tasks.

- Skip pending editor repaints during teardown to avoid accessing cleared styles.

- Keep a space between the bottom prompt and cursor.

- List only running background tasks in `jobs`, keeping automatic launch and
  completion notifications.

- Keep the foreground cursor's normal themed color instead of the read-only
  warning color, in both prompt layouts and light and dark themes.

- Keep the bottom prompt row visible and read-only during foreground commands,
  preventing flicker and transcript resizing while preserving cursor blinking.

- Reject directories explicitly during local text I/O so Windows reports
  `is a directory` consistently, while preserving genuine permission errors.

- Format common filesystem errors centrally for all commands, using workspace
  paths and consistent messages instead of host paths and OS errno text.

- Use consistent missing-file and directory errors in `more` and `less` across
  desktop and browser filesystems, without exposing host filesystem paths.

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
