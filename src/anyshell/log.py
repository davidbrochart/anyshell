"""Selectable rendered output for the shell and pager."""

from anytext.selection import Selection
from anytext.strip import Strip
from anytext.widgets import RichLog
from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style


class SelectableLog(RichLog):
    """Expose rendered log lines to Anytext's mouse selection machinery."""

    def get_selection(self, selection: Selection) -> tuple[str, str]:
        if not self.lines or (
            selection.start is not None and selection.start.y >= len(self.lines)
        ):
            return "", "\n"
        text = "\n".join(line.text.rstrip() for line in self.lines)
        return selection.extract(text), "\n"

    def _render_line(self, y: int, scroll_x: int, width: int) -> Strip:
        if y >= len(self.lines):
            return Strip.blank(width, self.rich_style)
        line = self.lines[y]
        selection = self.text_selection
        span = selection.get_span(y) if selection is not None else None
        if span is not None:
            start, end = span
            text = self.lines[y].text
            first = cell_len(text[:start])
            last = line.cell_length if end == -1 else cell_len(text[:end])
            if first < last:
                highlight = self.screen.get_component_rich_style("screen--selection")
                line = Strip.join(
                    [
                        line.crop(0, first),
                        Strip(
                            Segment.apply_style(
                                line.crop(first, last),
                                post_style=Style(bgcolor=highlight.bgcolor),
                            ),
                            last - first,
                        ),
                        line.crop(last),
                    ]
                )
        start_offset = len(line.crop(0, scroll_x).text)
        return line.crop_extend(
            scroll_x, scroll_x + width, self.rich_style
        ).apply_offsets(start_offset, y)
