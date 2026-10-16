"""Client setup must preserve existing settings and use literal absolute paths."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


spec = importlib.util.spec_from_file_location(
    "mcp_setup", Path(__file__).resolve().parents[2] / "tools/mcp-setup-windows.py"
)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


@pytest.mark.parametrize("packaged", [False, True])
def test_desktop_config_path_handles_store_installation(tmp_path, monkeypatch, packaged):
    roaming = tmp_path / "Roaming"
    local = tmp_path / "Local"
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    store = local / "Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude"
    if packaged:
        store.mkdir(parents=True)
    expected = store if packaged else roaming / "Claude"
    assert setup.desktop_config_path() == expected / "claude_desktop_config.json"


def test_desktop_merge_backup_and_idempotency(tmp_path):
    destination = tmp_path / "Claude/claude_desktop_config.json"
    destination.parent.mkdir()
    original = b'\xef\xbb\xbf{"preferences":{"theme":"dark"},"mcpServers":{"other":{"command":"keep"},"aegisub":{"command":"old"}}}'
    destination.write_bytes(original)
    server = setup.server_config(tmp_path / "日本語 folder")
    backup = setup.configure_desktop(destination, server)
    assert backup.read_bytes() == original
    merged = json.loads(destination.read_text(encoding="utf-8"))
    assert merged == {"preferences": {"theme": "dark"}, "mcpServers": {
        "other": {"command": "keep"}, "aegisub": server,
    }}
    written = destination.read_bytes()
    assert setup.configure_desktop(destination, server) is None
    assert destination.read_bytes() == written
    assert len(list(destination.parent.glob("*.bak"))) == 1


@pytest.mark.parametrize("original", [b"invalid", b"[]", b'{"mcpServers":[]}', b'{"mcpServers":null}'])
def test_desktop_invalid_settings_left_untouched(tmp_path, original):
    destination = tmp_path / "config.json"
    destination.write_bytes(original)
    with pytest.raises(ValueError):
        setup.configure_desktop(destination, {"command": "python"})
    assert destination.read_bytes() == original
    assert list(tmp_path.glob("*.bak")) == []


def test_desktop_remove_only_aegisub(tmp_path):
    destination = tmp_path / "config.json"
    destination.write_text(json.dumps({"mcpServers": {
        "aegisub": {"command": "old"}, "other": {"command": "keep"},
    }, "preferences": {"language": "vi"}}))
    backup = setup.configure_desktop(destination)
    assert backup.is_file()
    assert json.loads(destination.read_text()) == {
        "mcpServers": {"other": {"command": "keep"}}, "preferences": {"language": "vi"},
    }
    assert setup.configure_desktop(destination) is None


def test_desktop_remove_missing_does_not_create_file(tmp_path):
    destination = tmp_path / "missing/config.json"
    assert setup.configure_desktop(destination) is None
    assert not destination.parent.exists()


def test_server_config_paths_and_read_only(tmp_path):
    root = tmp_path / "日本語 and spaces"
    writable = setup.server_config(root)
    assert writable["command"] == str(root / ".venv/Scripts/python.exe")
    assert writable["args"] == ["-m", "aegisub_mcp.server", "--aegisub",
                                 str(root / "aegisub.exe"), "--allow-write"]
    assert setup.server_config(root, read_only=True)["args"] == writable["args"][:-1]


def test_failed_atomic_replace_preserves_settings(tmp_path, monkeypatch):
    destination = tmp_path / "config.json"
    original = b'{"preferences":{"theme":"dark"}}'
    destination.write_bytes(original)

    def fail(*args):
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(PermissionError):
        setup.configure_desktop(destination, {"command": "python"})
    assert destination.read_bytes() == original
    assert len(list(tmp_path.glob("*.bak"))) == 1
    assert len(list(tmp_path.iterdir())) == 2


def test_desktop_cli_reuses_environment_without_install(tmp_path, monkeypatch):
    root = tmp_path / "portable with spaces"
    python = root / ".venv/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    (root / "aegisub.exe").touch()
    appdata = tmp_path / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(setup, "__file__", str(root / "setup-mcp.py"))
    monkeypatch.setattr(sys, "argv", ["setup-mcp.py", "--claude-desktop", "--skip-install", "--read-only"])
    commands = []
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: commands.append(command))
    setup.main()
    assert commands == [[str(python), "-c", "import aegisub_mcp.server"]]
    config = json.loads((root / "mcp-config.json").read_text())
    assert "--allow-write" not in config["mcpServers"]["aegisub"]["args"]
    assert json.loads((appdata / "Claude/claude_desktop_config.json").read_text()) == config
    monkeypatch.setattr(sys, "argv", ["setup-mcp.py", "--remove-claude-desktop"])
    setup.main()
    assert json.loads((appdata / "Claude/claude_desktop_config.json").read_text()) == {"mcpServers": {}}
    assert len(commands) == 1  # Disconnect never installs or launches the server.


@pytest.mark.skipif(sys.platform != "win32", reason="Windows batch launcher")
def test_code_launcher_preserves_project_and_config_argument(tmp_path):
    portable = tmp_path / "portable with spaces"
    portable.mkdir()
    launcher = portable / "start-claude-code.cmd"
    shutil.copy2(Path(setup.__file__).with_name("mcp-start-claude-code.cmd"), launcher)
    (portable / "mcp-config.json").write_text('{}')
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "claude.cmd").write_text(
        '@echo off\necho %CD%>"%AEGISUB_TEST_CAPTURE%"\n'
        'echo %1>>"%AEGISUB_TEST_CAPTURE%"\n'
        'echo %~2>>"%AEGISUB_TEST_CAPTURE%"\n'
        'echo %3>>"%AEGISUB_TEST_CAPTURE%"\nexit /b 0\n'
    )
    project = tmp_path / "subtitle project"
    project.mkdir()
    capture = tmp_path / "capture.txt"
    env = {**{key.upper(): value for key, value in os.environ.items()},
           "PATH": str(stub) + os.pathsep + os.environ["PATH"],
           "AEGISUB_TEST_CAPTURE": str(capture)}
    found = subprocess.run(["where.exe", "claude"], env=env, capture_output=True)
    assert found.returncode == 0, (found.stdout, found.stderr)
    result = subprocess.run(f'"{os.environ["COMSPEC"]}" /d /s /c ""{launcher}" hello"',
                            cwd=project, env=env, capture_output=True)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert capture.read_text().splitlines() == [
        str(project), "--mcp-config", str(portable / "mcp-config.json"), "hello",
    ]
