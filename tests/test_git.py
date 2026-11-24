from threading import get_ident

from dulwich import porcelain
from rich.text import Text

from anyshell import LocalFileSystem, Session


async def test_git_requires_local_filesystem():
    status, events = await run(Session(), "git status")
    assert status == 1
    assert any(
        event.kind == "error" and "requires a local filesystem" in event.value
        for event in events
    )


async def run(session, source, *, is_terminal=False):
    events = []

    async def emit(event):
        events.append(event)

    result = await session.execute(source, emit, is_terminal=is_terminal)
    return result.status, events


async def test_git_uses_session_cwd_and_passes_paths_to_porcelain(tmp_path):
    repo = tmp_path / "repo"
    (repo / "subdir").mkdir(parents=True)
    session = Session(LocalFileSystem(tmp_path))
    status, events = await run(
        session,
        'cd repo; git init; cd subdir; echo sample > "-my file"; '
        'git add -- "-my file"; git ls-files > /tracked.txt',
    )
    assert status == 0, events
    assert (repo / ".git").is_dir()
    assert (tmp_path / "tracked.txt").read_text().splitlines() == ["subdir/-my file"]

    status, events = await run(session, "git --help | count")
    assert status == 0
    assert any(event.kind == "output" and event.value > 0 for event in events)

    status, events = await run(session, "git unknown-subcommand")
    assert status == 1
    assert any(event.kind == "error" for event in events)


async def test_git_calls_porcelain_on_the_shell_thread(tmp_path, monkeypatch):
    for variable in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{variable}_NAME", "AnyShell Test")
        monkeypatch.setenv(f"{variable}_EMAIL", "test@example.org")
    original_commit = porcelain.commit
    shell_thread = get_ident()
    commits = []

    def commit(repo, **kwargs):
        assert get_ident() == shell_thread
        commits.append(kwargs["message"])
        return original_commit(repo, **kwargs)

    monkeypatch.setattr(porcelain, "commit", commit)
    session = Session(LocalFileSystem(tmp_path))
    status, events = await run(
        session,
        'git init; echo sample > file; git add .; git commit -m "First commit"; '
        "git log -n 1 --oneline; git branch work; git switch work",
    )
    assert status == 0
    assert not [event for event in events if event.kind == "error"]
    assert commits == ["First commit"]
    assert any(
        event.kind == "output" and "First commit" in event.value for event in events
    )
    assert (tmp_path / ".git" / "HEAD").read_text().strip() == "ref: refs/heads/work"

    status, events = await run(session, "git branch other; git branch")
    assert status == 0
    branches = [event.value for event in events if event.kind == "output"]
    assert "* work" in branches
    assert "  other" in branches
    assert sum(line.startswith("* ") for line in branches) == 1

    status, events = await run(session, "git branch", is_terminal=True)
    assert status == 0
    colored = [event.value for event in events if isinstance(event.value, Text)]
    assert len(colored) == 1
    assert colored[0].plain == "* work"
    assert [(span.start, span.end, span.style) for span in colored[0].spans] == [
        (2, 6, "green")
    ]

    with porcelain.open_repo_closing(tmp_path) as repo:
        config = repo.get_config()
        config.set((b"branch", b"work"), b"remote", b"origin")
        config.set((b"branch", b"work"), b"merge", b"refs/heads/work")
        config.write_to_path()
        repo.refs[b"refs/remotes/origin/work"] = repo.head()

    status, events = await run(session, "git status")
    assert status == 0
    assert [event.value for event in events if event.kind == "output"] == [
        "On branch work",
        "Your branch is up to date with 'origin/work'.",
        "nothing to commit, working tree clean",
    ]

    status, events = await run(
        session, "echo changed > file; echo new > untracked; git status"
    )
    assert status == 0
    lines = [event.value for event in events if event.kind == "output"]
    assert "Changes not staged for commit:" in lines
    assert "\tmodified:   file" in lines
    assert "Untracked files:" in lines
    assert "\tuntracked" in lines
    assert lines[-1].startswith("no changes added to commit")

    status, events = await run(session, 'git commit -a -m "Second commit"; git status')
    assert status == 0
    assert any(
        event.kind == "output"
        and event.value == "Your branch is ahead of 'origin/work' by 1 commit."
        for event in events
    )

    pages = []

    async def emit(event):
        if event.kind == "page":
            request = event.value
            assert request.searchable
            pages.append([line async for line in request.lines])
            request.handled = True

    result = await session.execute("git log -n 1 --oneline", emit, is_terminal=True)
    assert result.status == 0
    assert len(pages) == 1 and pages[0][0].endswith("Second commit")

    result = await session.execute(
        "git log -n 1 --oneline > history.txt", emit, is_terminal=True
    )
    assert result.status == 0
    assert len(pages) == 1
    assert (tmp_path / "history.txt").read_text().strip().endswith("Second commit")

    (tmp_path / "file").write_text("+++ replacement\n")
    result = await session.execute("git diff", emit, is_terminal=True)
    assert result.status == 0
    assert len(pages) == 2
    assert {line.plain: line.style for line in pages[-1] if isinstance(line, Text)} == {
        "-changed": "red",
        "++++ replacement": "green",
    }

    result = await session.execute("git diff > patch.txt", emit, is_terminal=True)
    assert result.status == 0
    assert len(pages) == 2
    assert (tmp_path / "patch.txt").read_text().splitlines() == [
        line.plain if isinstance(line, Text) else line for line in pages[-1]
    ]
