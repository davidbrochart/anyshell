import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from anyshell import Command, Session, ShellError, plugins


def install(monkeypatch, *entries):
    discover = Mock(return_value=entries)
    monkeypatch.setattr(plugins, "entry_points", discover)
    return discover


def test_only_selected_plugins_load_once(monkeypatch):
    def register(session):
        session.register(Command("greet", "Say hello", lambda: "hello"))

    selected = SimpleNamespace(name="tools", load=Mock(return_value=register))
    other = SimpleNamespace(name="other", load=Mock())
    discover = install(monkeypatch, selected, other)
    session = Session()
    plugins.load_plugins(session, ["tools", "tools"])
    assert "greet" in session.commands
    selected.load.assert_called_once_with()
    other.load.assert_not_called()
    discover.assert_called_once_with(group="anyshell.commands")


def test_empty_selection_loads_nothing(monkeypatch):
    entry = SimpleNamespace(name="tools", load=Mock())
    install(monkeypatch, entry)
    plugins.load_plugins(Session(), [])
    entry.load.assert_not_called()


def test_unknown_plugin_is_reported_before_loading(monkeypatch):
    entry = SimpleNamespace(name="tools", load=Mock())
    install(monkeypatch, entry)
    with pytest.raises(ShellError, match="plugin not installed: missing"):
        plugins.load_plugins(Session(), ["tools", "missing"])
    entry.load.assert_not_called()


def test_duplicate_plugin_names_are_rejected(monkeypatch):
    entry = SimpleNamespace(name="tools", load=Mock())
    install(monkeypatch, entry, entry)
    with pytest.raises(ShellError, match="multiple command plugins named tools"):
        plugins.load_plugins(Session(), ["tools"])
    entry.load.assert_not_called()


def test_broken_plugin_has_context(monkeypatch):
    entry = SimpleNamespace(
        name="broken", load=Mock(side_effect=ImportError("dependency"))
    )
    install(monkeypatch, entry)
    with pytest.raises(
        ShellError, match="could not load command plugin broken: dependency"
    ):
        plugins.load_plugins(Session(), ["broken"])


def test_discovery_loads_all_plugins_in_name_order(monkeypatch):
    loaded = []
    entries = [
        SimpleNamespace(
            name=name, load=lambda name=name: lambda session: loaded.append(name)
        )
        for name in ["zebra", "apple"]
    ]
    install(monkeypatch, *entries)
    plugins.load_plugins(Session())
    assert loaded == ["apple", "zebra"]


def test_discovery_rejects_duplicate_names_before_loading(monkeypatch):
    entry = SimpleNamespace(name="tools", load=Mock())
    install(monkeypatch, entry, entry)
    with pytest.raises(ShellError, match="multiple command plugins named tools"):
        plugins.load_plugins(Session())
    entry.load.assert_not_called()


@pytest.mark.parametrize(
    "flags, enabled",
    [
        ([], True),
        (["--disable", "sample"], False),
        (["--plugin", "sample"], True),
        (["--plugin", "sample", "--disable", "sample"], False),
    ],
)
def test_cli_plugin_discovery(tmp_path, flags, enabled):
    metadata = tmp_path / "sample-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: sample\nVersion: 1.0\n")
    (metadata / "entry_points.txt").write_text(
        "[anyshell.commands]\nsample = sample_plugin:register\n"
    )
    (tmp_path / "sample_plugin.py").write_text(
        "from anyshell import Command\n"
        "def register(session):\n"
        "    session.register(Command('sample_command', 'External test command', lambda: 'hello'))\n"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(tmp_path), environment.get("PYTHONPATH", "")]
    )
    result = subprocess.run(
        [sys.executable, "-m", "anyshell", *flags, "-c", "help"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert ("sample_command" in result.stdout) == enabled


def test_disabled_plugins_are_never_imported(monkeypatch):
    disabled = SimpleNamespace(
        name="broken", load=Mock(side_effect=ImportError("broken"))
    )
    enabled = SimpleNamespace(name="working", load=Mock(return_value=Mock()))
    install(monkeypatch, disabled, enabled)
    plugins.load_plugins(Session(), disabled=["broken"])
    disabled.load.assert_not_called()
    enabled.load.assert_called_once_with()
