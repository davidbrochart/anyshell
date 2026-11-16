"""Split panes preserve sessions while layout and focus change."""

import anyio
import pytest
from anytext.widgets import SplitPane

from anyshell import Command, MemoryFileSystem, Session
from anyshell.frontend import CommandInput, Shell, ShellApp
from anyshell.panes import ShellWorkspace


async def wait_until(predicate):
    with anyio.fail_after(5):
        while not predicate():
            await anyio.sleep(0.01)


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_split_nested_layout_and_independent_sessions(prompt_position):
    session = Session(MemoryFileSystem({"docs/file": "data"}))
    session.cwd = "/docs"
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test(size=(120, 40)) as pilot:
        workspace = app.query_one(ShellWorkspace)
        first = workspace.active
        first_task = first._task
        first.query_one(CommandInput).value = "unfinished draft"
        await pilot.press("alt+v")
        await pilot.pause()
        second = workspace.active
        assert len(workspace.panes) == 2 and first is not second
        assert first.is_mounted
        assert first._task is first_task
        split = workspace.query_one(SplitPane)
        assert split.first.query_one(Shell) is first
        assert split.second.query_one(Shell) is second
        assert first.query_one(CommandInput).value == "unfinished draft"
        assert second.session is not first.session
        assert second.session.cwd == "/docs"
        assert second.session.filesystem is first.session.filesystem
        assert second.session.history == []
        assert second.region.x > first.region.x
        assert first.region.right <= second.region.x
        assert first.region.height == second.region.height
        await pilot.press("alt+h")
        await pilot.pause()
        third = workspace.active
        assert len(workspace.panes) == 3
        assert third.region.y >= second.region.bottom
        assert third.region.x == second.region.x
        assert third.region.width == second.region.width
        await pilot.press("alt+up")
        assert workspace.active is second
        assert second.query_one(CommandInput).has_focus
        await pilot.press("alt+down")
        assert workspace.active is third
        await pilot.press("alt+w")
        await pilot.pause()
        assert workspace.active is second
        assert len(workspace.panes) == 2
        assert second.region.height == first.region.height
        await pilot.press("alt+left", "alt+w")
        await pilot.pause()
        assert workspace.panes == [second]
        assert second.parent is workspace
        assert second.region == workspace.content_region
        await pilot.press("alt+w")
        assert workspace.panes == [second] and app.is_running


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_split_keeps_foreground_running_and_completion_keeps_focus(
    prompt_position,
):
    gate = anyio.Event()

    async def hold():
        await gate.wait()

    session = Session()
    session.register(Command("hold", "Wait for test", hold))
    app = ShellApp(session, prompt_position=prompt_position)
    async with app.run_test(size=(100, 30)) as pilot:
        workspace = app.query_one(ShellWorkspace)
        first = workspace.active
        first.query_one(CommandInput).value = "hold"
        await pilot.press("enter")
        await wait_until(lambda: session.running)
        await pilot.press("alt+v")
        await pilot.pause()
        second = workspace.active
        assert first._busy and session.running
        assert second.query_one(CommandInput).has_focus
        gate.set()
        await wait_until(lambda: not first._busy)
        await pilot.pause()
        assert workspace.active is second
        assert second.query_one(CommandInput).has_focus
        await pilot.press("alt+left")
        assert first.query_one(CommandInput).has_focus


async def test_close_cancels_only_closed_panes_jobs():
    app = ShellApp()
    async with app.run_test() as pilot:
        workspace = app.query_one(ShellWorkspace)
        first = workspace.active
        first.query_one(CommandInput).value = "sleep 30 &"
        await pilot.press("enter")
        await wait_until(lambda: bool(first.session.jobs))
        await pilot.press("alt+v")
        second = workspace.active
        second.query_one(CommandInput).value = "sleep 30 &"
        await pilot.press("enter")
        await wait_until(lambda: bool(second.session.jobs))
        closed_job = second.session.jobs[1]
        await pilot.press("alt+w")
        await wait_until(closed_job.done.is_set)
        assert closed_job.result.cancelled
        assert first.session.jobs[1].result is None
        assert workspace.active is first


async def test_divider_keyboard_resize_and_window_resize():
    app = ShellApp()
    async with app.run_test(size=(100, 30)) as pilot:
        workspace = app.query_one(ShellWorkspace)
        await pilot.press("alt+v")
        await pilot.pause()
        first, second = workspace.panes
        split = workspace.query_one(SplitPane)
        width = first.region.width
        split.divider.focus()
        await pilot.press("right", "right", "right")
        await pilot.pause()
        assert first.region.width == width + 3
        assert first.region.right < second.region.x
        await pilot.resize_terminal(120, 40)
        await pilot.pause()
        assert first.region.height == second.region.height
        assert first.region in workspace.content_region
        assert second.region in workspace.content_region
        await pilot.press("alt+left")
        assert workspace.active.query_one(CommandInput).has_focus


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_closing_foreground_pane_cancels_it(prompt_position):
    app = ShellApp(prompt_position=prompt_position)
    async with app.run_test() as pilot:
        workspace = app.query_one(ShellWorkspace)
        original = workspace.active
        await pilot.press("alt+v")
        closing = workspace.active
        closing.query_one(CommandInput).value = "sleep 30"
        await pilot.press("enter")
        await wait_until(lambda: closing.session.running)
        await pilot.press("alt+w")
        await wait_until(lambda: not closing.session.running)
        assert workspace.panes == [original]
        assert original.query_one(CommandInput).has_focus
        assert app.is_running


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_typing_after_split_targets_new_pane(prompt_position):
    app = ShellApp(prompt_position=prompt_position)
    async with app.run_test(size=(100, 30)) as pilot:
        workspace = app.query_one(ShellWorkspace)
        original = workspace.active
        await pilot.press("alt+v", "e", "c", "h", "o", "space", "x")
        await pilot.pause()
        assert workspace.active is not original
        assert workspace.active.query_one(CommandInput).value == "echo x"
        assert original.query_one(CommandInput).value == ""


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_clicking_empty_pane_focuses_its_prompt(prompt_position):
    app = ShellApp(prompt_position=prompt_position)
    async with app.run_test(size=(100, 30)) as pilot:
        workspace = app.query_one(ShellWorkspace)
        first = workspace.active
        await pilot.press("alt+v")
        await pilot.pause()
        assert workspace.active is not first
        await pilot.click(
            first, offset=(first.region.width // 2, first.region.height // 2)
        )
        assert workspace.active is first
        assert first.query_one(CommandInput).has_focus


@pytest.mark.parametrize("shortcut", ["alt+v", "alt+h"])
async def test_divider_drag_keeps_real_shells_inside_panes(shortcut):
    app = ShellApp()
    async with app.run_test(size=(100, 30)) as pilot:
        workspace = app.query_one(ShellWorkspace)
        await pilot.press(shortcut)
        await pilot.pause()
        split = workspace.query_one(SplitPane)
        initial = split.first_size
        await pilot.mouse_down(split.divider)
        for delta in (3, 6):
            x = split.divider.region.x - workspace.region.x
            y = split.divider.region.y - workspace.region.y
            if split.orientation == "horizontal":
                x += delta
            else:
                y += delta
            await pilot.hover(workspace, offset=(x, y))
            await pilot.pause()
            assert split.first.query_one(Shell).region == split.first.content_region
            assert split.second.query_one(Shell).region == split.second.content_region
            frame = "\n".join(
                strip.text for strip in app.screen._compositor.render_strips()
            )
            assert "_PaneSlot" not in frame
        await pilot.mouse_up(workspace, offset=(x, y))
        assert split.first_size > initial


async def test_closing_outer_pane_promotes_live_nested_split():
    app = ShellApp()
    async with app.run_test(size=(100, 40)) as pilot:
        workspace = app.query_one(ShellWorkspace)
        first = workspace.active
        await pilot.press("alt+v")
        second = workspace.active
        second.query_one(CommandInput).value = "sleep 30 &"
        await pilot.press("enter")
        await wait_until(lambda: bool(second.session.jobs))
        await pilot.press("alt+h")
        third = workspace.active
        third.query_one(CommandInput).value = "sleep 30 &"
        await pilot.press("enter")
        await wait_until(lambda: bool(third.session.jobs))
        second_task, third_task = second._task, third._task
        await pilot.press("alt+left")
        assert workspace.active is first
        await pilot.press("alt+w")
        await pilot.pause()
        assert workspace.panes == [second, third]
        assert workspace.query_one(SplitPane).parent is workspace
        assert second._task is second_task and third._task is third_task
        assert second.session.jobs[1].result is None
        assert third.session.jobs[1].result is None


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_all_arrow_keys_focus_spatial_neighbors_without_wrapping(prompt_position):
    app = ShellApp(prompt_position=prompt_position)
    async with app.run_test(size=(120, 40)) as pilot:
        workspace = app.query_one(ShellWorkspace)
        left = workspace.active
        await pilot.press("alt+v")
        top_right = workspace.active
        await pilot.press("alt+h")
        bottom_right = workspace.active
        await pilot.press("alt+up")
        assert workspace.active is top_right
        await pilot.press("alt+up", "alt+right")
        assert workspace.active is top_right
        await pilot.press("alt+down")
        assert workspace.active is bottom_right
        await pilot.press("alt+down", "alt+right")
        assert workspace.active is bottom_right
        await pilot.press("alt+left")
        assert workspace.active is left
        await pilot.press("alt+left", "alt+up", "alt+down")
        assert workspace.active is left
        await pilot.press("alt+right")
        assert workspace.active is top_right
        assert top_right.query_one(CommandInput).has_focus


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_divider_toggle_applies_to_nested_and_future_splits(prompt_position):
    app = ShellApp(prompt_position=prompt_position)
    async with app.run_test(size=(120, 40)) as pilot:
        workspace = app.query_one(ShellWorkspace)
        first = workspace.active
        first.query_one(CommandInput).value = "keep this draft"
        task = first._task
        await pilot.press("alt+d", "alt+v", "alt+h")
        await pilot.pause()
        first, second, third = workspace.panes
        splits = list(workspace.query(SplitPane))
        positions = [split.position for split in splits]
        assert len(splits) == 2
        assert all(not split.show_divider for split in splits)
        assert first.region.right == second.region.x
        assert second.region.bottom == third.region.y
        await pilot.press("alt+d")
        await pilot.pause()
        assert all(split.show_divider for split in splits)
        assert [split.position for split in splits] == positions
        assert first.region.right + 1 == second.region.x
        assert second.region.bottom + 1 == third.region.y
        assert first._task is task
        assert first.query_one(CommandInput).value == "keep this draft"
        assert third.query_one(CommandInput).has_focus


@pytest.mark.parametrize("shortcut", ["alt+v", "alt+h"])
async def test_hidden_divider_still_drags(shortcut):
    app = ShellApp()
    async with app.run_test(size=(100, 30)) as pilot:
        workspace = app.query_one(ShellWorkspace)
        await pilot.press(shortcut, "alt+d")
        await pilot.pause()
        split = workspace.query_one(SplitPane)
        initial = split.first_size
        x = split.divider.region.x - workspace.region.x
        y = split.divider.region.y - workspace.region.y
        await pilot.mouse_down(split.divider)
        if split.orientation == "horizontal":
            x += 4
        else:
            y += 4
        await pilot.hover(workspace, offset=(x, y))
        await pilot.mouse_up(workspace, offset=(x, y))
        await pilot.pause()
        assert not split.show_divider
        assert split.first_size == initial + 4
        first, second = workspace.panes
        if split.orientation == "horizontal":
            assert first.region.right == second.region.x
        else:
            assert first.region.bottom == second.region.y


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_alt_left_drag_swaps_sessions_and_closes_the_moved_pane(prompt_position):
    app = ShellApp(prompt_position=prompt_position)
    async with app.run_test(size=(100, 30)) as pilot:
        workspace = app.query_one(ShellWorkspace)
        first = workspace.active
        first.query_one(CommandInput).value = "sleep 30 &"
        await pilot.press("enter")
        await wait_until(lambda: bool(first.session.jobs) and not first._busy)
        job = first.session.jobs[1]
        first.query_one(CommandInput).value = "left draft"
        await pilot.press("alt+v")
        second = workspace.active
        second.query_one(CommandInput).value = "right draft"
        tasks = first._task, second._task
        source = first.query_one(CommandInput).region
        target = second.query_one(CommandInput).region
        await pilot.mouse_down(offset=(source.x + 2, source.y), meta=True)
        await pilot.hover(offset=(target.x + 2, target.y))
        await pilot.mouse_up(offset=(target.x + 2, target.y), meta=True)
        await pilot.pause()
        assert workspace.panes == [second, first]
        assert workspace.active is first
        assert first.query_one(CommandInput).value == "left draft"
        assert second.query_one(CommandInput).value == "right draft"
        assert (first._task, second._task) == tasks
        assert first.region.x > second.region.x
        assert job.result is None
        await pilot.press("alt+w")
        await wait_until(job.done.is_set)
        assert workspace.panes == [second]
        assert second.query_one(CommandInput).value == "right draft"
        assert job.result.cancelled


async def test_alt_left_drag_swaps_across_nested_splits():
    app = ShellApp()
    async with app.run_test(size=(120, 40)) as pilot:
        workspace = app.query_one(ShellWorkspace)
        left = workspace.active
        await pilot.press("alt+v")
        top = workspace.active
        await pilot.press("alt+h")
        bottom = workspace.active
        source = left.query_one(CommandInput).region
        target = bottom.query_one(CommandInput).region
        await pilot.mouse_down(offset=(source.x + 2, source.y), meta=True)
        await pilot.mouse_up(offset=(target.x + 2, target.y), meta=True)
        await pilot.pause()
        assert workspace.panes == [bottom, top, left]
        assert workspace.active is left
        assert bottom.region.x < left.region.x
        await pilot.press("alt+up")
        assert workspace.active is top
        await pilot.press("alt+left")
        assert workspace.active is bottom


@pytest.mark.parametrize("show_divider", [True, False])
async def test_alt_right_drag_resizes_over_shell_content(show_divider):
    app = ShellApp()
    async with app.run_test(size=(100, 30)) as pilot:
        workspace = app.query_one(ShellWorkspace)
        await pilot.press("alt+v", "alt+h")
        if not show_divider:
            await pilot.press("alt+d")
        await pilot.pause()
        outer, inner = workspace.query(SplitPane)
        before_outer, before_inner = outer.first_size, inner.first_size
        pane = workspace.active
        pane.query_one(CommandInput).value = "keep draft"
        region = pane.query_one(CommandInput).region
        # Resize from the transcript area, away from the divider itself.
        x, y = region.x + 3, pane.region.y + 3
        await pilot.mouse_down(offset=(x, y), meta=True, button=3)
        assert app.mouse_captured is inner.divider
        await pilot.hover(offset=(x, y + 3))
        await pilot.mouse_up(offset=(x, y + 3), meta=True, button=3)
        await pilot.pause()
        assert inner.first_size == before_inner + 3
        assert outer.first_size == before_outer
        assert pane.query_one(CommandInput).value == "keep draft"


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
@pytest.mark.parametrize("show_divider", [True, False])
async def test_drag_wrapped_shell_border_corner(prompt_position, show_divider):
    """The Shell border inside an unbordered host resizes both shared edges."""
    app = ShellApp(prompt_position=prompt_position)
    async with app.run_test(size=(100, 30)) as pilot:
        workspace = app.query_one(ShellWorkspace)
        await pilot.press("alt+o", "alt+v", "alt+h")
        if not show_divider:
            await pilot.press("alt+d")
        for _ in range(3):
            await pilot.pause()
        outer, inner = workspace.query(SplitPane)
        pane = workspace.active
        pane.query_one(CommandInput).value = "keep draft"
        task = pane._task
        before_outer, before_inner = outer.first_size, inner.first_size
        start = pane.region.offset
        await pilot.mouse_down(offset=start)
        assert app.mouse_captured is inner.divider
        await pilot.hover(offset=(start.x + 3, start.y + 2))
        await pilot.mouse_up(offset=(start.x + 3, start.y + 2))
        await pilot.pause()
        assert outer.first_size == before_outer + 3
        assert inner.first_size == before_inner + 2
        assert pane.query_one(CommandInput).value == "keep draft"
        assert pane._task is task
        assert app.mouse_captured is None


@pytest.mark.parametrize("key", ["alt+v", "alt+h"])
@pytest.mark.parametrize("nested", [False, True])
async def test_split_never_paints_an_incomplete_layout(monkeypatch, key, nested):
    """A refresh during mounting shows the old layout or the completed split."""
    app = ShellApp()
    async with app.run_test(size=(100, 30)) as pilot:
        workspace = app.query_one(ShellWorkspace)
        if nested:
            await pilot.press("alt+v")
        original = workspace.active
        parent = original.parent
        frames = []
        display = app._display
        mount = parent.mount

        def record_frame(screen, renderable):
            # App._display suppresses terminal output while a batch is active.
            if renderable is not None and not app._batch_count:
                frames.append((tuple(parent.children), len(parent.query(Shell))))
            return display(screen, renderable)

        async def mount_with_refresh(*args, **kwargs):
            result = mount(*args, **kwargs)
            await result
            # Force the frame that would otherwise depend on runner timing.
            app.screen._on_timer_update()
            return result

        monkeypatch.setattr(app, "_display", record_frame)
        monkeypatch.setattr(parent, "mount", mount_with_refresh)
        await pilot.press(key)
        await pilot.pause()
        assert frames
        for children, shells in frames:
            assert len(children) == 1
            if children[0] is original:
                assert shells == 1
            else:
                assert isinstance(children[0], SplitPane)
                assert shells == 2
        split = parent.children[0]
        assert isinstance(split, SplitPane)
        assert split.orientation == ("horizontal" if key == "alt+v" else "vertical")
        assert workspace.active is not original
        assert workspace.active.query_one(CommandInput).has_focus


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
async def test_border_shortcut_applies_to_existing_and_future_panes(prompt_position):
    """Border visibility survives focus, splitting, and independent divider toggles."""
    app = ShellApp(prompt_position=prompt_position)
    async with app.run_test(size=(100, 30)) as pilot:
        workspace = app.query_one(ShellWorkspace)
        first = workspace.active
        first.query_one(CommandInput).value = "keep draft"
        assert not workspace.show_borders
        assert not any(first.styles.border.spacing)
        await pilot.press("alt+o")
        await pilot.pause()
        assert workspace.show_borders and any(first.styles.border.spacing)
        await pilot.press("alt+o")
        await pilot.pause()
        assert not workspace.show_borders
        assert not any(first.styles.border.spacing)
        assert first.query_one(CommandInput).value == "keep draft"
        assert first.query_one(CommandInput).has_focus
        await pilot.press("alt+v", "alt+h")
        await pilot.pause()
        assert len(workspace.panes) == 3
        assert all(not any(pane.styles.border.spacing) for pane in workspace.panes)
        assert all(split.show_divider for split in workspace.query(SplitPane))
        await pilot.press("alt+left", "alt+d")
        await pilot.pause()
        assert workspace.active is first
        assert all(not any(pane.styles.border.spacing) for pane in workspace.panes)
        assert all(not split.show_divider for split in workspace.query(SplitPane))
        await pilot.press("alt+o")
        await pilot.pause()
        assert workspace.show_borders
        assert all(any(pane.styles.border.spacing) for pane in workspace.panes)
        assert all(not split.show_divider for split in workspace.query(SplitPane))
        assert first.query_one(CommandInput).value == "keep draft"
        assert first.query_one(CommandInput).has_focus


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
@pytest.mark.parametrize("theme", ["anytext-dark", "anytext-light"])
async def test_active_pane_background_follows_focus_without_separators(
    prompt_position, theme
):
    """Theme-aware backgrounds identify focus without consuming pane space."""
    from anyshell.frontend import Transcript

    app = ShellApp(prompt_position=prompt_position)
    app.theme = theme
    async with app.run_test(size=(100, 30)) as pilot:
        workspace = app.query_one(ShellWorkspace)
        first = workspace.active
        await pilot.press("alt+d", "alt+v")
        for _ in range(3):
            await pilot.pause()
        second = workspace.active
        assert second.has_class("pane-active")
        assert first.has_class("pane-inactive")
        assert not any(first.styles.border.spacing)
        assert not any(second.styles.border.spacing)
        assert first.region.right == second.region.x
        assert first.background_colors[1] != second.background_colors[1]
        for pane in (first, second):
            for widget in (pane.query_one(Transcript), pane.query_one(CommandInput)):
                assert widget.background_colors[1] == pane.background_colors[1]
        await pilot.press("alt+left")
        await pilot.pause()
        assert first.has_class("pane-active")
        assert not second.has_class("pane-active")
        assert (
            first.query_one(CommandInput).background_colors[1]
            == first.background_colors[1]
        )
        await pilot.click(second, offset=(3, 3))
        await pilot.pause()
        assert workspace.active is second and second.has_class("pane-active")
        assert second.query_one(CommandInput).has_focus
        await pilot.press("alt+w")
        await pilot.pause()
        assert workspace.panes == [first]
        assert not first.has_class("pane-active")
        assert not first.has_class("pane-inactive")


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
@pytest.mark.parametrize("command", ["less", "edit"])
async def test_active_pane_tints_pager_and_editor(prompt_position, command):
    """Interactive views retain the active pane's visual identity."""
    from anyshell.editor import FileEditor, FileTextArea
    from anyshell.pager import Pager, PagerLog

    app = ShellApp(
        Session(MemoryFileSystem({"file": "hello\n"})), prompt_position=prompt_position
    )
    async with app.run_test(size=(100, 30)) as pilot:
        workspace = app.query_one(ShellWorkspace)
        await pilot.press("alt+d", "alt+v")
        pane = workspace.active
        pane.query_one(CommandInput).value = f"{command} file"
        await pilot.press("enter")
        view_type = Pager if command == "less" else FileEditor
        await wait_until(lambda: bool(pane.query(view_type)))
        await pilot.pause()
        view = pane.query_one(view_type)
        content = view.query_one(PagerLog if command == "less" else FileTextArea)
        assert view.background_colors[1] == pane.background_colors[1]
        assert content.background_colors[1] == pane.background_colors[1]
        await pilot.press("ctrl+q")
        await wait_until(
            lambda: not pane._busy and pane.query_one(CommandInput).has_focus
        )


@pytest.mark.parametrize("prompt_position", ["inline", "bottom"])
@pytest.mark.parametrize("key", ["alt+h", "alt+v"])
@pytest.mark.parametrize("nested", [False, True])
async def test_close_never_paints_promoted_pane_beside_old_split(
    monkeypatch, prompt_position, key, nested
):
    """A surviving pane expands without an intermediate sibling layout."""
    app = ShellApp(prompt_position=prompt_position)
    async with app.run_test(size=(100, 30)) as pilot:
        workspace = app.query_one(ShellWorkspace)
        if nested:
            await pilot.press("alt+v")
        survivor = workspace.active
        await pilot.press(key)
        closing = workspace.active
        split = closing.query_ancestor(SplitPane)
        parent = split.parent
        frames = []
        display = app._display
        remove = split.remove

        def record_frame(screen, renderable):
            if renderable is not None and not app._batch_count:
                frames.append((tuple(parent.children), survivor.region))
            return display(screen, renderable)

        def remove_with_refresh():
            # Expose the promotion frame before starting old-split teardown.
            app.screen._on_timer_update()
            return remove()

        monkeypatch.setattr(app, "_display", record_frame)
        monkeypatch.setattr(split, "remove", remove_with_refresh)
        await pilot.press("alt+w")
        await pilot.pause()
        assert frames
        for children, region in frames:
            assert len(children) == 1
            if children[0] is not split:
                assert children[0] is survivor
                assert region == parent.content_region
        assert survivor.parent is parent
        assert workspace.active is survivor
        assert survivor.query_one(CommandInput).has_focus
        assert not closing.is_attached
