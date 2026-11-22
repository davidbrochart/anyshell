"""Command registration hooks supplied by external packages."""

from importlib.metadata import entry_points

from .engine import Session, ShellError


def load_plugins(session: Session, names=None, *, disabled=()) -> None:
    """Load anyshell.commands entry points, each a callable(session).

    Hooks run synchronously before the shell starts. Newly split panes inherit
    the resulting command registry. With names=None, load all installed hooks
    in name order. An explicit selection loads only those hooks. Disabled hooks
    are skipped without importing their modules.
    """
    available = entry_points(group="anyshell.commands")
    if names is None:
        names = sorted({entry.name for entry in available})
    disabled = set(disabled)
    selected = []
    for name in dict.fromkeys(names):
        if name in disabled:
            continue
        matches = [entry for entry in available if entry.name == name]
        if not matches:
            raise ShellError(f"command plugin not installed: {name}")
        if len(matches) != 1:
            raise ShellError(f"multiple command plugins named {name}")
        selected.append(matches[0])
    for entry in selected:
        try:
            entry.load()(session)
        except Exception as error:
            raise ShellError(
                f"could not load command plugin {entry.name}: {error}"
            ) from error
