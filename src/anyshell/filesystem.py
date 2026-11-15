"""Workspace filesystems with a common POSIX-style path namespace."""

from __future__ import annotations

import codecs
import errno
import posixpath
import shutil
import time
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from typing import Literal, Protocol

import anyio


class FileSystem(Protocol):
    def open(self, path: str) -> FileHandle: ...
    def listdir(self, path: str) -> list[dict]: ...
    def stat(self, path: str) -> dict: ...
    def entry_type(self, path: str) -> Literal["file", "directory", "symlink"]: ...
    def is_dir(self, path: str) -> bool: ...
    def read_lines(self, path: str) -> Iterator[str]: ...
    def read_text(self, path: str) -> str: ...
    def read_bytes(self, path: str, offset: int, size: int) -> bytes: ...
    def write_text(self, path: str, text: str) -> None: ...
    def append_text(self, path: str, text: str) -> None: ...
    def mkdir(self, path: str) -> None: ...
    def rmdir(self, path: str) -> None: ...
    def touch(self, path: str) -> None: ...
    def symlink(self, target: str, path: str) -> None: ...
    def remove(self, path: str, *, recursive: bool = False) -> None: ...
    def move(self, source: str, destination: str) -> None: ...
    def copy(
        self, source: str, destination: str, *, recursive: bool = False
    ) -> None: ...


def _validate_transfer(fs: FileSystem, source: str, destination: str) -> bool:
    """Validate exact transfer paths before changing either filesystem."""
    fs.stat(source)
    if source == "/" or destination == "/":
        raise PermissionError("cannot copy or move the workspace root")
    if source == destination:
        raise OSError("source and destination are the same path")
    directory = fs.is_dir(source)
    if directory and destination.startswith(source + "/"):
        raise OSError("cannot copy or move a directory into itself")
    if fs.is_dir(destination):
        raise FileExistsError(f"{destination!r}: directory already exists")
    if directory:
        try:
            fs.stat(destination)
        except FileNotFoundError:
            pass
        else:
            raise NotADirectoryError(f"{destination!r} is a file")
    return directory


def resolve_path(cwd: str, path: str) -> str:
    """Normalize a logical path without changing the process working directory."""
    return "/" + posixpath.normpath(posixpath.join(cwd, path)).lstrip("/")


class FileHandle:
    """An async, read-only path handle with a byte cursor and change subscription.

    Reads return immediately with available data. Changes made through this
    filesystem wake waiters; destructive changes restart reading at byte zero.
    The handle follows a path, including symlinks, rather than an inode.
    """

    def __init__(self, filesystem: _ObservableFileSystem, path: str) -> None:
        self._fs = filesystem
        self.path = resolve_path("/", path)
        self._target = ""
        self._offset = 0
        self._active = False
        self.closed = False
        self.revision = 0
        self._restart = False
        self._changed: anyio.Event | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")()

    async def __aenter__(self) -> FileHandle:
        """Validate and subscribe atomically before the first read."""
        if self.closed or self._active:
            raise ValueError("file handle is closed or already open")
        self._fs.read_bytes(self.path, 0, 0)
        self._target = self._fs._watch_path(self.path)
        self._changed = anyio.Event()
        self._fs._handles.add(self)
        self._active = True
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    def _check_open(self) -> None:
        if not self._active or self.closed:
            raise ValueError("file handle is not open")

    @property
    def size(self) -> int:
        """Return the current byte length of the file at this path."""
        self._check_open()
        size = self._fs.stat(self.path)["size"]
        if size is None:
            raise IsADirectoryError(self.path)
        return size

    def tell(self) -> int:
        """Return the byte position of the next read."""
        self._check_open()
        return self._offset

    def seek(self, offset: int, whence: int = 0) -> int:
        """Move the byte cursor relative to the start, cursor, or EOF."""
        self._check_open()
        if whence not in (0, 1, 2):
            raise ValueError("invalid seek origin")
        position = offset + (
            0 if whence == 0 else self._offset if whence == 1 else self.size
        )
        if position < 0:
            raise ValueError("negative seek position")
        self._offset = position
        self._decoder.reset()
        self._restart = False
        return position

    async def read_bytes(self, size: int = -1) -> bytes:
        """Read available bytes, advancing this handle's independent cursor."""
        await anyio.lowlevel.checkpoint()
        self._check_open()
        end = self.size
        if self._restart or end < self._offset:
            self.seek(0)
        count = max(0, end - self._offset) if size < 0 else size
        data = self._fs.read_bytes(self.path, self._offset, count)
        self._offset += len(data)
        return data

    async def read(self) -> str:
        """Read currently available UTF-8 text, retaining incomplete characters."""
        return self._decoder.decode(await self.read_bytes())

    async def wait_changed(self) -> None:
        """Wait for a latched notification, including writes before this call."""
        self._check_open()
        changed = self._changed
        assert changed is not None
        await changed.wait()
        self._check_open()
        if self._changed is changed:
            self._changed = anyio.Event()

    async def aclose(self) -> None:
        """Unsubscribe even when the surrounding command has been cancelled."""
        self._fs._handles.discard(self)
        self._active = False
        self.closed = True
        if self._changed is not None:
            self._changed.set()


class _ObservableFileSystem:
    """Share path notifications between handles using this filesystem instance."""

    def __init__(self) -> None:
        self._handles: set[FileHandle] = set()

    def open(self, path: str) -> FileHandle:
        """Create a read handle; enter its async context to subscribe."""
        return FileHandle(self, path)

    def _watch_path(self, path: str) -> str:
        raise NotImplementedError

    def _notify(self, *paths: str, reset: bool = False, resolve: bool = True) -> None:
        """Coalesce successful mutations without blocking synchronous writers."""
        if not self._handles:
            return
        changed = {resolve_path("/", path) for path in paths}
        if resolve:
            for path in paths:
                try:
                    changed.add(self._watch_path(path))
                except OSError:
                    pass
        for handle in tuple(self._handles):
            watched = {handle.path, handle._target}
            try:
                watched.add(self._watch_path(handle.path))
            except OSError:
                pass
            if any(
                key == path or key.startswith(path.rstrip("/") + "/")
                for key in watched
                for path in changed
            ):
                if reset:
                    handle.revision += 1
                    handle._restart = True
                assert handle._changed is not None
                handle._changed.set()


class LocalFileSystem(_ObservableFileSystem):
    """Map logical / to a workspace directory, including on Windows.

    Resolved symlinks must stay in that workspace. This is a convenience boundary,
    not a security sandbox for Python commands or concurrent filesystem changes.
    """

    def __init__(self, root: str | Path = ".") -> None:
        super().__init__()
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise NotADirectoryError(str(root))

    def _path(self, path: str) -> Path:
        try:
            candidate = (self.root / path.lstrip("/")).resolve()
        except RuntimeError as error:
            # Python 3.11 reports symlink loops as RuntimeError.
            raise OSError(
                errno.ELOOP, "too many levels of symbolic links", path
            ) from error
        if not candidate.is_relative_to(self.root):
            raise PermissionError("path leaves the workspace")
        return candidate

    def _watch_path(self, path: str) -> str:
        return resolve_path("/", self._path(path).relative_to(self.root).as_posix())

    def _file_path(self, path: str) -> Path:
        candidate = self._path(path)
        # Windows reports opening a directory as PermissionError, unlike Unix.
        if candidate.is_dir():
            raise IsADirectoryError(path)
        return candidate

    def is_dir(self, path: str) -> bool:
        return self._path(path).is_dir()

    def listdir(self, path: str) -> list[dict]:
        return [
            {
                "name": child.name,
                "type": "symlink"
                if child.is_symlink()
                else "directory"
                if child.is_dir()
                else "file",
            }
            for child in sorted(self._path(path).iterdir(), key=lambda p: p.name)
        ]

    def entry_type(self, path: str) -> Literal["file", "directory", "symlink"]:
        if path == "/":
            return "directory"
        entry = self.root / path.lstrip("/")
        parent = entry.parent.resolve()
        if not parent.is_relative_to(self.root):
            raise PermissionError("path leaves the workspace")
        entry = parent / entry.name
        if entry.is_symlink():
            return "symlink"
        entry.stat()  # Distinguish missing entries from regular files.
        return "directory" if entry.is_dir() else "file"

    def stat(self, path: str) -> dict:
        candidate = self._path(path)
        metadata = candidate.stat()
        return {
            "size": None if candidate.is_dir() else metadata.st_size,
            "modified": metadata.st_mtime,
        }

    def read_text(self, path: str) -> str:
        with self._file_path(path).open(encoding="utf-8", newline="") as stream:
            return stream.read()

    def read_lines(self, path: str) -> Iterator[str]:
        return _TextLines(self._file_path(path).open(encoding="utf-8"))

    def read_bytes(self, path: str, offset: int, size: int) -> bytes:
        """Read a bounded byte range without loading the whole file."""
        with self._file_path(path).open("rb") as stream:
            stream.seek(offset)
            return stream.read(size)

    def write_text(self, path: str, text: str) -> None:
        self._file_path(path).write_text(text, encoding="utf-8", newline="")
        self._notify(path, reset=True)

    def mkdir(self, path: str) -> None:
        self._path(path).mkdir()

    def append_text(self, path: str, text: str) -> None:
        with self._file_path(path).open("a", encoding="utf-8", newline="") as stream:
            stream.write(text)
        self._notify(path)

    def rmdir(self, path: str) -> None:
        # Preserve rmdir semantics: a symlink to a directory is not a directory.
        unresolved = self.root / path.lstrip("/")
        candidate = self._path(path)
        if candidate == self.root:
            raise PermissionError("cannot remove the workspace root")
        if unresolved.is_symlink():
            raise NotADirectoryError(path)
        candidate.rmdir()

    def touch(self, path: str) -> None:
        self._path(path).touch()
        self._notify(path)

    def symlink(self, target: str, path: str) -> None:
        if not target:
            raise FileNotFoundError(target)
        entry = self.root / path.lstrip("/")
        parent = self._path(posixpath.dirname(path))
        entry = parent / entry.name
        logical_target = resolve_path(posixpath.dirname(path), target)
        candidate = self._path(logical_target)
        # Absolute shell paths refer to the workspace, not the host root.
        if target.startswith("/"):
            stored_target = str(self.root / target.lstrip("/"))
        else:
            physical_target = (parent / target).resolve()
            if not physical_target.is_relative_to(self.root):
                raise PermissionError("path leaves the workspace")
            stored_target = target
            candidate = physical_target
        entry.symlink_to(stored_target, target_is_directory=candidate.is_dir())
        self._notify(path, reset=True, resolve=False)

    def remove(self, path: str, *, recursive: bool = False) -> None:
        unresolved = self.root / path.lstrip("/")
        if unresolved.is_symlink():
            if not unresolved.parent.resolve().is_relative_to(self.root):
                raise PermissionError("path leaves the workspace")
            unresolved.unlink()
            self._notify(path, reset=True)
            return
        candidate = self._path(path)
        if candidate == self.root:
            raise PermissionError("cannot remove the workspace root")
        if candidate.is_dir():
            if not recursive:
                raise IsADirectoryError(path)
            shutil.rmtree(candidate)
        else:
            candidate.unlink()
        self._notify(path, reset=True)

    def move(self, source: str, destination: str) -> None:
        source_is_link = self.entry_type(source) == "symlink"
        if source_is_link:
            if source == destination:
                raise OSError("source and destination are the same path")
            if self.is_dir(destination):
                raise IsADirectoryError(destination)
        else:
            _validate_transfer(self, source, destination)
        # Rename the entry itself, rather than a symlink's target. Resolve parent
        # directories to keep both endpoints inside the workspace.
        entries = []
        for path in (source, destination):
            entry = self.root / path.lstrip("/")
            parent = entry.parent.resolve()
            if not parent.is_relative_to(self.root):
                raise PermissionError("path leaves the workspace")
            entries.append(parent / entry.name)
        original, target = entries
        if target.exists() and original.samefile(target):
            raise OSError("source and destination are the same file")
        if (
            original.is_dir()
            and not original.is_symlink()
            and target.is_relative_to(original.resolve())
        ):
            raise OSError("cannot move a directory into itself")
        original.replace(target)
        self._notify(source, destination, reset=True, resolve=not source_is_link)

    def copy(self, source: str, destination: str, *, recursive: bool = False) -> None:
        directory = _validate_transfer(self, source, destination)
        original, target = self._path(source), self._path(destination)
        if directory:
            if not recursive:
                raise IsADirectoryError(f"{source!r} is a directory; use cp -r")
            if target.is_relative_to(original):
                raise OSError("cannot copy a directory into itself")
            # Preserve nested links rather than following them outside the workspace.
            shutil.copytree(original, target, symlinks=True)
        else:
            shutil.copy2(original, target)
        self._notify(destination, reset=True)


class _TextLines:
    """An eagerly opened, explicitly closable iterator of text lines."""

    def __init__(self, stream):
        self.stream = stream

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self.stream).rstrip("\r\n")
        except BaseException:
            self.close()
            raise

    def close(self):
        self.stream.close()

    def __del__(self):
        self.close()


class MemoryFileSystem(_ObservableFileSystem):
    """An in-memory workspace; no disk, network, processes, or threads required."""

    def __init__(self, files: dict[str, str] | None = None) -> None:
        super().__init__()
        self.files: dict[str, str] = {}
        self.symlinks: dict[str, str] = {}
        self.directories = {"/"}
        self._modified = {"/": time.time()}
        for name, content in (files or {}).items():
            path = resolve_path("/", name)
            if path in self.directories:
                raise IsADirectoryError(path)
            parent = posixpath.dirname(path)
            while parent != "/":
                if parent in self.files:
                    raise NotADirectoryError(parent)
                self.directories.add(parent)
                self._modified.setdefault(parent, time.time())
                parent = posixpath.dirname(parent)
            self.files[path] = content
            self._modified[path] = time.time()

    def _watch_path(self, path: str) -> str:
        return self._resolve(path)

    def _resolve(self, path: str, *, follow: bool = True) -> str:
        parts = path.strip("/").split("/") if path != "/" else []
        resolved = "/"
        links = 0
        while parts:
            part = parts.pop(0)
            resolved = resolve_path(resolved, part)
            if resolved in self.symlinks and (follow or parts):
                links += 1
                if links > 40:
                    raise OSError(
                        errno.ELOOP, "too many levels of symbolic links", path
                    )
                target = resolve_path(
                    posixpath.dirname(resolved), self.symlinks[resolved]
                )
                parts = target.strip("/").split("/") + parts
                resolved = "/"
            if (
                parts
                and resolved not in self.directories
                and resolved not in self.symlinks
            ):
                if resolved in self.files:
                    raise NotADirectoryError(resolved)
                raise FileNotFoundError(resolved)
        return resolved

    def is_dir(self, path: str) -> bool:
        try:
            return self._resolve(path) in self.directories
        except FileNotFoundError:
            return False

    def listdir(self, path: str) -> list[dict]:
        path = self._resolve(path)
        if path not in self.directories:
            raise NotADirectoryError(path)
        rows = [
            {"name": posixpath.basename(item), "type": kind}
            for items, kind in (
                (self.files, "file"),
                (self.directories, "directory"),
                (self.symlinks, "symlink"),
            )
            for item in items
            if item != path and posixpath.dirname(item) == path
        ]
        return sorted(rows, key=lambda row: row["name"])

    def entry_type(self, path: str) -> Literal["file", "directory", "symlink"]:
        path = self._resolve(path, follow=False)
        if path in self.symlinks:
            return "symlink"
        if path in self.directories:
            return "directory"
        if path in self.files:
            return "file"
        raise FileNotFoundError(path)

    def stat(self, path: str) -> dict:
        path = self._resolve(path)
        if path not in self.files and path not in self.directories:
            raise FileNotFoundError(path)
        return {
            "size": len(self.files[path].encode("utf-8"))
            if path in self.files
            else None,
            "modified": self._modified[path],
        }

    def _touch(self, path: str) -> None:
        self._modified[path] = time.time()
        self._modified[posixpath.dirname(path)] = time.time()

    def read_text(self, path: str) -> str:
        path = self._resolve(path)
        if path in self.directories:
            raise IsADirectoryError(path)
        try:
            return self.files[path]
        except KeyError:
            raise FileNotFoundError(path) from None

    def read_lines(self, path: str) -> Iterator[str]:
        path = self._resolve(path)
        if path in self.directories:
            raise IsADirectoryError(path)
        if path not in self.files:
            raise FileNotFoundError(path)

        def lines():
            yield from (
                line.rstrip("\r\n") for line in StringIO(self.files[path], newline=None)
            )

        return lines()

    def read_bytes(self, path: str, offset: int, size: int) -> bytes:
        """Read a byte range using the same UTF-8 offsets as local files."""
        return self.read_text(path).encode("utf-8")[offset : offset + size]

    def _check_parent(self, path: str) -> None:
        if posixpath.dirname(path) not in self.directories:
            raise FileNotFoundError(posixpath.dirname(path))

    def write_text(self, path: str, text: str) -> None:
        path = self._resolve(path)
        self._check_parent(path)
        if path in self.directories:
            raise IsADirectoryError(path)
        self.files[path] = text
        self._touch(path)
        self._notify(path, reset=True)

    def mkdir(self, path: str) -> None:
        path = self._resolve(path, follow=False)
        self._check_parent(path)
        if path in self.files or path in self.directories or path in self.symlinks:
            raise FileExistsError(path)
        self.directories.add(path)
        self._touch(path)

    def append_text(self, path: str, text: str) -> None:
        path = self._resolve(path)
        self._check_parent(path)
        if path in self.directories:
            raise IsADirectoryError(path)
        self.files[path] = self.files.get(path, "") + text
        self._touch(path)
        self._notify(path)

    def rmdir(self, path: str) -> None:
        path = self._resolve(path, follow=False)
        if path in self.symlinks:
            raise NotADirectoryError(path)
        if path in self.files:
            raise NotADirectoryError(path)
        if path not in self.directories:
            raise FileNotFoundError(path)
        if path == "/":
            raise PermissionError("cannot remove the workspace root")
        if self.listdir(path):
            raise OSError("directory is not empty")
        self.directories.remove(path)
        self._modified.pop(path, None)
        self._modified[posixpath.dirname(path)] = time.time()

    def touch(self, path: str) -> None:
        path = self._resolve(path)
        if path in self.files or path in self.directories:
            self._modified[path] = time.time()
            self._notify(path)
        else:
            self.write_text(path, "")

    def symlink(self, target: str, path: str) -> None:
        if not target:
            raise FileNotFoundError(target)
        path = self._resolve(path, follow=False)
        self._check_parent(path)
        if path in self.files or path in self.directories or path in self.symlinks:
            raise FileExistsError(path)
        self.symlinks[path] = target
        self._touch(path)
        self._notify(path, reset=True, resolve=False)

    def remove(self, path: str, *, recursive: bool = False) -> None:
        path = self._resolve(path, follow=False)
        if path == "/":
            raise PermissionError("cannot remove the workspace root")
        if path in self.symlinks:
            del self.symlinks[path]
        elif path in self.files:
            del self.files[path]
        elif path in self.directories:
            if not recursive:
                raise IsADirectoryError(path)
            for child in list(self._modified):
                if child.startswith(path + "/"):
                    self.files.pop(child, None)
                    self.symlinks.pop(child, None)
                    self.directories.discard(child)
                    self._modified.pop(child, None)
            self.directories.remove(path)
        else:
            raise FileNotFoundError(path)
        self._modified.pop(path, None)
        self._modified[posixpath.dirname(path)] = time.time()
        self._notify(path, reset=True)

    def _transfer(
        self, source: str, destination: str, *, move: bool, recursive: bool
    ) -> None:
        source = self._resolve(source, follow=not move)
        destination = self._resolve(destination, follow=not move)
        if move and source in self.symlinks:
            if source == destination:
                raise OSError("source and destination are the same path")
            self._check_parent(destination)
            if destination in self.directories:
                raise IsADirectoryError(destination)
            self.files.pop(destination, None)
            self.symlinks[destination] = self.symlinks.pop(source)
            self._modified.pop(source, None)
            self._touch(destination)
            return
        directory = _validate_transfer(self, source, destination)
        if directory and not recursive:
            raise IsADirectoryError(f"{source!r} is a directory; use cp -r")
        self._check_parent(destination)
        paths = {
            path
            for path in self._modified
            if path == source or (directory and path.startswith(source + "/"))
        }
        replacements = {path: destination + path[len(source) :] for path in paths}
        files = {
            target: self.files[path]
            for path, target in replacements.items()
            if path in self.files
        }
        directories = {
            target for path, target in replacements.items() if path in self.directories
        }
        symlinks = {
            target: self.symlinks[path]
            for path, target in replacements.items()
            if path in self.symlinks
        }
        modified = {
            target: self._modified[path] for path, target in replacements.items()
        }
        if move:
            for path in paths:
                self.files.pop(path, None)
                self.symlinks.pop(path, None)
                self.directories.discard(path)
                self._modified.pop(path, None)
            self.symlinks.pop(destination, None)
        self.files.update(files)
        self.symlinks.update(symlinks)
        self.directories.update(directories)
        self._modified.update(modified)
        now = time.time()
        self._modified[posixpath.dirname(destination)] = now
        if move:
            self._modified[posixpath.dirname(source)] = now

    def move(self, source: str, destination: str) -> None:
        source_is_link = self.entry_type(source) == "symlink"
        self._transfer(source, destination, move=True, recursive=True)
        self._notify(source, destination, reset=True, resolve=not source_is_link)

    def copy(self, source: str, destination: str, *, recursive: bool = False) -> None:
        self._transfer(source, destination, move=False, recursive=recursive)
        self._notify(destination, reset=True)


def format_filesystem_error(
    error: Exception, filesystem: FileSystem, *, include_path: bool = True
) -> str:
    """Format OS errors using portable reasons and workspace paths."""
    if not isinstance(error, OSError):
        return str(error)
    reasons = {
        errno.ENOENT: "no such file or directory",
        errno.EEXIST: "already exists",
        errno.ENOTDIR: "not a directory",
        errno.EISDIR: "is a directory",
        errno.EACCES: "permission denied",
        errno.EPERM: "operation not permitted",
        errno.ENOTEMPTY: "directory is not empty",
        errno.EROFS: "read-only filesystem",
        errno.ELOOP: "too many levels of symbolic links",
    }
    reason = reasons.get(error.errno)
    if reason is None:
        for kind, text in (
            (FileNotFoundError, "no such file or directory"),
            (FileExistsError, "already exists"),
            (NotADirectoryError, "not a directory"),
            (IsADirectoryError, "is a directory"),
            (PermissionError, "permission denied"),
        ):
            if isinstance(error, kind):
                reason = text
                break
    if reason is None:
        return str(error)
    path = error.filename
    if path is None:
        if (
            len(error.args) == 1
            and isinstance(error.args[0], str)
            and error.args[0].startswith("/")
        ):
            path = error.args[0]
        else:
            # Preserve explicit explanations such as workspace-boundary errors.
            return str(error) if error.errno is None else reason
    elif isinstance(filesystem, LocalFileSystem):
        try:
            path = resolve_path("/", Path(path).relative_to(filesystem.root).as_posix())
        except (TypeError, ValueError):
            return reason
    if not include_path:
        return reason
    if error.filename2 is not None:
        target = error.filename2
        if isinstance(filesystem, LocalFileSystem):
            try:
                target = resolve_path(
                    "/", Path(target).relative_to(filesystem.root).as_posix()
                )
            except (TypeError, ValueError):
                return reason
        return f"{path!r} -> {target!r}: {reason}"
    return f"{path!r}: {reason}"
