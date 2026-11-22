"""Human-readable output formatting, separate from pipeline values."""

from datetime import datetime, timezone

from .commands import render

LONG_FILE_HEADERS = ("Type", "Bytes", "Modified (UTC)", "Name")


def file_listing(rows, *, long=False):
    """Build ls's terminal presentation without changing its pipeline records."""
    from rich.columns import Columns
    from rich.table import Table
    from rich.text import Text

    def name(row):
        return Text(
            file_name(row), style="bold blue" if row["type"] == "directory" else ""
        )

    if not long:
        return Columns([name(row) for row in rows], padding=(0, 2))
    table = Table(box=None, padding=(0, 2))
    for header in LONG_FILE_HEADERS:
        table.add_column(header, justify="right" if header == "Bytes" else "left")
    for row in rows:
        kind, size, modified, _ = long_file_cells(row)
        table.add_row(kind, size, modified, name(row))
    return table


def is_file_record(value) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("name"), str)
        and value.get("type") in ("file", "directory", "symlink")
    )


def is_long_file_record(value) -> bool:
    return is_file_record(value) and "size" in value and "modified" in value


def file_name(value) -> str:
    if "path" in value:
        return value["path"]
    return value["name"] + ("/" if value["type"] == "directory" else "")


def long_file_cells(value) -> tuple[str, str, str, str]:
    modified = value["modified"]
    if isinstance(modified, (int, float)):
        try:
            modified = datetime.fromtimestamp(modified, timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except (OSError, OverflowError, ValueError):
            modified = "?"
    return (
        {"directory": "dir", "symlink": "link", "file": "file"}[value["type"]],
        str(value["size"]) if value["size"] is not None else "-",
        str(modified) if modified is not None else "-",
        file_name(value),
    )


def format_long_line(cells) -> str:
    kind, size, modified, name = cells
    return f"{kind:4} {size:>10} {modified:19} {name}"


def format_value(value) -> str:
    if is_long_file_record(value):
        return format_long_line(long_file_cells(value))
    if is_file_record(value):
        return file_name(value)
    return render(value)
