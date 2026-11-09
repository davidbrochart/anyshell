# Changelog

## Unreleased

- Initial independent async shell engine, structured streaming pipelines, Python
  built-ins, local and in-memory workspace filesystems, and cancellable sessions.
- Optional Anytext shell widget with input, transcript, completion and history.
- Desktop CLI, browser demo, and asyncio/Trio regression tests.
- Display file listings as names in columns, with styled directory suffixes,
  while preserving structured pipeline records.
- Default to an inline prompt in the transcript scrollback, with an optional
  fixed bottom prompt and browser demo selector.
- Parse typed command arguments with Cyclopts, capture help and errors in shell
  output, and complete option names.
- Add long and hidden file listings (`ls -l`, `ls -a`), case-insensitive grep,
  reverse sorting, and filesystem size/modification metadata.
- Show ambiguous completions in a cursor-adjacent menu with keyboard and mouse selection.
- Accept explicit file paths in plain and long `ls` listings, including hidden files.
- Add a horizontal scrollbar for completion entries wider than the popup.
- Add `touch` and `rmdir` with local and in-memory filesystem support.
- Make `rm` reject directories; use `rmdir` to remove empty directories.
- Complete the shared prefix before opening the menu for ambiguous matches.
- Add `edit PATH` with a text editor, save shortcuts, unsaved-change confirmation, and desktop/browser filesystem support.
- Make the editor replace the Shell widget’s contents instead of opening a modal window.
- Replace editor buttons with shortcuts in a single bottom status line, including unsaved-change confirmation.
- Use Ctrl+Q to close the editor, with Escape to cancel unsaved-change confirmation.
- Allow a second Ctrl+Q to discard unsaved edits and return to the shell.
- Show editor shortcuts only in the footer, updating close/discard and keep-editing labels during confirmation.
- Remove the editor status line; use the title’s dirty indicator and notifications for save errors.
- Add `mv`, `cp`, and recursive `cp -r` for local and in-memory filesystems, preserving contents and modification times.
- Accept multiple paths in `rm`, reporting failures while continuing with remaining files.
- Add `more` for files and pipeline output with in-shell paging, navigation shortcuts, and early pipeline exit.
- Add `less` for files and pipeline output with forward/backward regex search and `n`/`N` match navigation.
- Add multiline command input, quote/pipe/backslash continuations, newline-separated execution, and multiline history/completion.
- Add `find` with name/type/depth filters, structured paths, hidden entries, and traversal without following symlinks.
