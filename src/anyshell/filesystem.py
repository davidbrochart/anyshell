"""Workspace filesystems with a common POSIX-style path namespace."""

from __future__ import annotations

import errno
import posixpath
import shutil
import time
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from typing import Literal, Protocol


class FileSystem(Protocol):
    def listdir(self, path: str) -> list[dict]: ...
    def stat(self, path: str) -> dict: ...
    def entry_type(self, path: str) -> Literal["file", "directory", "symlink"]: ...
    def is_dir(self, path: str) -> bool: ...
    def read_lines(self, path: str) -> Iterator[str]: ...
    def read_text(self, path: str) -> str: ...
    def write_text(self, path: str, text: str) -> None: ...
    def mkdir(self, path: str) -> None: ...
    def rmdir(self, path: str) -> None: ...
    def touch(self, path: str) -> None: ...
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


class LocalFileSystem:
    """Map logical / to a workspace directory, including on Windows.

    Resolved symlinks must stay in that workspace. This is a convenience boundary,
    not a security sandbox for Python commands or concurrent filesystem changes.
    """

    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise NotADirectoryError(str(root))

    def _path(self, path: str) -> Path:
        candidate = (self.root / path.lstrip("/")).resolve()
        if not candidate.is_relative_to(self.root):
            raise PermissionError("path leaves the workspace")
        return candidate

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
            {"name": child.name, "type": "directory" if child.is_dir() else "file"}
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
        with self._file_path(path).open(encoding="utf-8") as stream:
            for line in stream:
                yield line.rstrip("\r\n")

    def write_text(self, path: str, text: str) -> None:
        self._file_path(path).write_text(text, encoding="utf-8", newline="")

    def mkdir(self, path: str) -> None:
        self._path(path).mkdir()

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

    def remove(self, path: str, *, recursive: bool = False) -> None:
        unresolved = self.root / path.lstrip("/")
        if unresolved.is_symlink():
            if not unresolved.parent.resolve().is_relative_to(self.root):
                raise PermissionError("path leaves the workspace")
            unresolved.unlink()
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

    def move(self, source: str, destination: str) -> None:
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


class MemoryFileSystem:
    """An in-memory workspace; no disk, network, processes, or threads required."""

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files: dict[str, str] = {}
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

    def is_dir(self, path: str) -> bool:
        return path in self.directories

    def listdir(self, path: str) -> list[dict]:
        if path not in self.directories:
            raise NotADirectoryError(path)
        rows = [
            {"name": posixpath.basename(item), "type": kind}
            for items, kind in ((self.files, "file"), (self.directories, "directory"))
            for item in items
            if item != path and posixpath.dirname(item) == path
        ]
        return sorted(rows, key=lambda row: row["name"])

    def entry_type(self, path: str) -> Literal["file", "directory", "symlink"]:
        if path in self.directories:
            return "directory"
        if path in self.files:
            return "file"
        raise FileNotFoundError(path)

    def stat(self, path: str) -> dict:
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
        if path in self.directories:
            raise IsADirectoryError(path)
        try:
            return self.files[path]
        except KeyError:
            raise FileNotFoundError(path) from None

    def read_lines(self, path: str) -> Iterator[str]:
        if path in self.directories:
            raise IsADirectoryError(path)
        try:
            return (
                line.rstrip("\r\n") for line in StringIO(self.files[path], newline=None)
            )
        except KeyError:
            raise FileNotFoundError(path) from None

    def _check_parent(self, path: str) -> None:
        if posixpath.dirname(path) not in self.directories:
            raise FileNotFoundError(posixpath.dirname(path))

    def write_text(self, path: str, text: str) -> None:
        self._check_parent(path)
        if path in self.directories:
            raise IsADirectoryError(path)
        self.files[path] = text
        self._touch(path)

    def mkdir(self, path: str) -> None:
        self._check_parent(path)
        if path in self.files or path in self.directories:
            raise FileExistsError(path)
        self.directories.add(path)
        self._touch(path)

    def rmdir(self, path: str) -> None:
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
        if path in self.files or path in self.directories:
            self._modified[path] = time.time()
        else:
            self.write_text(path, "")

    def remove(self, path: str, *, recursive: bool = False) -> None:
        if path == "/":
            raise PermissionError("cannot remove the workspace root")
        if path in self.files:
            del self.files[path]
        elif path in self.directories:
            if not recursive:
                raise IsADirectoryError(path)
            for child in list(self._modified):
                if child.startswith(path + "/"):
                    self.files.pop(child, None)
                    self.directories.discard(child)
                    self._modified.pop(child, None)
            self.directories.remove(path)
        else:
            raise FileNotFoundError(path)
        self._modified.pop(path, None)
        self._modified[posixpath.dirname(path)] = time.time()

    def _transfer(
        self, source: str, destination: str, *, move: bool, recursive: bool
    ) -> None:
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
        modified = {
            target: self._modified[path] for path, target in replacements.items()
        }
        if move:
            for path in paths:
                self.files.pop(path, None)
                self.directories.discard(path)
                self._modified.pop(path, None)
        self.files.update(files)
        self.directories.update(directories)
        self._modified.update(modified)
        now = time.time()
        self._modified[posixpath.dirname(destination)] = now
        if move:
            self._modified[posixpath.dirname(source)] = now

    def move(self, source: str, destination: str) -> None:
        self._transfer(source, destination, move=True, recursive=True)

    def copy(self, source: str, destination: str, *, recursive: bool = False) -> None:
        self._transfer(source, destination, move=False, recursive=recursive)


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
