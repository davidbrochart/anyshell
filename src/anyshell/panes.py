"""Resizable shell panes whose sessions remain mounted during layout changes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from anytext import events, on
from anytext.containers import Vertical
from anytext.widget import Widget
from anytext.widgets import SplitPane

from .editor import FileEditor
from .engine import Session
from .frontend import CommandInput, RunningCursor, Shell
from .pager import Pager


@dataclass
class _Split:
    first: Shell | _Split
    second: Shell | _Split
    widget: SplitPane


class ShellWorkspace(Widget):
    """Resizable shell panes which stay mounted when their ancestry changes."""

    DEFAULT_CSS = """
    ShellWorkspace { width: 1fr; height: 1fr; }
    ShellWorkspace Shell { min-width: 0; min-height: 0; }
    ShellWorkspace Shell.pane-inactive { border: round $primary 30%; }
    """

    def __init__(self, session: Session, *, prompt_position="inline"):
        super().__init__()
        self.prompt_position = prompt_position
        self.show_dividers = True
        self.active = Shell(session, prompt_position=prompt_position)
        self.panes = [self.active]
        self._tree: Shell | _Split = self.active

    def compose(self):
        yield self.active

    def _replace(self, original, replacement):
        def replace(node):
            if node is original:
                return replacement
            if isinstance(node, _Split):
                node.first, node.second = replace(node.first), replace(node.second)
            return node

        self._tree = replace(self._tree)

    async def _settle_layout(self):
        await self.wait_for_refresh()
        await self.wait_for_refresh()

    def _sync_tree(self):
        nodes = {}

        def collect(node):
            if isinstance(node, _Split):
                nodes[node.widget] = node
                collect(node.first)
                collect(node.second)

        collect(self._tree)
        panes = []

        def read(widget):
            if isinstance(widget, Shell):
                panes.append(widget)
                return widget
            if isinstance(widget, SplitPane):
                node = nodes[widget]
                node.first, node.second = read(widget.first), read(widget.second)
                return node
            return read(widget.children[0])

        root = self._tree.widget if isinstance(self._tree, _Split) else self._tree
        self._tree = read(root)
        self.panes = panes

    @on(SplitPane.Swapped)
    def panes_swapped(self, event: SplitPane.Swapped):
        self._sync_tree()
        for pane in self.panes:
            if pane is event.pane or event.pane in pane.ancestors:
                self.active = pane
                break
        self._focus_active()

    def on_descendant_focus(self, event: events.DescendantFocus):
        if self.screen.focused is not event.widget:
            return
        for pane in self.panes:
            if event.widget is pane or pane in event.widget.ancestors:
                self.active = pane
                break
        self._mark_active()

    def on_mouse_down(self, event: events.MouseDown):
        for pane in self.panes:
            if pane is not self.active and pane.region.contains(
                event.screen_x, event.screen_y
            ):
                self.active = pane
                self._focus_active()
                break

    def _mark_active(self):
        for pane in self.panes:
            pane.set_class(
                len(self.panes) > 1 and pane is not self.active, "pane-inactive"
            )

    def _focus_active(self):
        pane = self.active
        if pane.query(FileEditor):
            target = pane.query_one(FileEditor).query_one("#file-text")
        elif pane.query(Pager):
            target = pane.query_one(Pager)
        elif pane._busy and pane.prompt_position == "inline":
            target = pane.query_one(RunningCursor)
        else:
            target = pane.query_one(CommandInput)
        self.screen.set_focus(target, scroll_visible=False)
        self._mark_active()

    async def split_active(self, orientation: Literal["horizontal", "vertical"]):
        self._sync_tree()
        original = self.active
        session = Session(original.session.filesystem)
        session.cwd = original.session.cwd
        session.commands = original.session.commands.copy()
        new = Shell(session, prompt_position=self.prompt_position, focus_on_mount=False)
        first, second = Vertical(), Vertical()
        split = SplitPane(
            first,
            second,
            orientation=orientation,
            min_first=4,
            min_second=4,
            show_divider=self.show_dividers,
        )
        split.divider.tooltip = (
            "Drag or Alt+right-drag to resize; Alt+left-drag swaps panes; "
            "arrows adjust; Enter resets; Alt+D toggles lines"
        )
        await original.parent.mount(split, before=original)
        original.reparent(first)
        self._replace(original, _Split(original, new, split))
        self.panes.insert(self.panes.index(original) + 1, new)
        await second.mount(new)
        await self._settle_layout()
        self.active = new
        self._focus_active()

    def focus_pane(self, direction: Literal["up", "down", "left", "right"]):
        source = self.active.region
        horizontal = direction in {"left", "right"}
        candidates = []
        for pane in self.panes:
            if pane is self.active or not pane.region.width or not pane.region.height:
                continue
            target = pane.region
            gap = {
                "left": source.x - target.right,
                "right": target.x - source.right,
                "up": source.y - target.bottom,
                "down": target.y - source.bottom,
            }[direction]
            if gap < 0:
                continue
            start, end = (
                (source.y, source.bottom) if horizontal else (source.x, source.right)
            )
            other_start, other_end = (
                (target.y, target.bottom) if horizontal else (target.x, target.right)
            )
            overlap = min(end, other_end) - max(start, other_start)
            distance = abs((start + end) - (other_start + other_end))
            candidates.append(((overlap <= 0, gap, max(0, -overlap), distance), pane))
        if candidates:
            self.active = min(candidates, key=lambda candidate: candidate[0])[1]
            self._focus_active()

    def toggle_dividers(self):
        self.show_dividers = not self.show_dividers
        for split in self.query(SplitPane):
            split.show_divider = self.show_dividers

    async def close_active(self):
        await self.close_pane(self.active)

    async def close_pane(self, closing: Shell):
        self._sync_tree()
        if len(self.panes) == 1:
            return
        index = self.panes.index(closing)
        previous_active = self.active

        def find_parent(node):
            if isinstance(node, _Split):
                if node.first is closing or node.second is closing:
                    return node
                return find_parent(node.first) or find_parent(node.second)
            return None

        parent = find_parent(self._tree)
        sibling = parent.second if parent.first is closing else parent.first
        survivor = sibling.widget if isinstance(sibling, _Split) else sibling
        survivor.reparent(parent.widget.parent, before=parent.widget)
        self._replace(parent, sibling)
        self.panes.remove(closing)
        remaining = (
            self.panes[min(index, len(self.panes) - 1)]
            if previous_active is closing
            else previous_active
        )
        await parent.widget.remove()
        await self._settle_layout()
        self.active = remaining
        self._focus_active()
