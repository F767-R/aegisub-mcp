import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
import pytest

from aegisub_mcp.bridge import BridgeError, atomic_json
from aegisub_mcp.server import create_server
from aegisub_mcp.sessions import SessionManager
from aegisub_mcp.sessions import launch_window_helper


def fixture_window(root, sid):
    directory = root / sid
    directory.mkdir()
    atomic_json(directory / 'session.json', dict(protocol_version=3, session_id=sid,
        pid=os.getpid(), token='b' * 64, write_enabled=True))
    atomic_json(directory / 'context.json', dict(session_id=sid, revision=1, context_version=1))
    return directory


async def test_open_window_is_explicit_and_selects_new_window_without_closing_old(tmp_path, monkeypatch):
    exe = tmp_path / 'aegisub.exe'
    exe.touch()
    first = fixture_window(tmp_path, 'a' * 32)
    second = fixture_window(tmp_path, 'c' * 32)
    manager = SessionManager(True, tmp_path, exe)
    manager.connect(first.name)
    calls = []

    class Launcher:
        def __init__(self, command, **kwargs):
            calls.append((command, kwargs))
            result_path = command[command.index('--result') + 1]
            from pathlib import Path
            atomic_json(Path(result_path), dict(ready=True, launched=True,
                pid=os.getpid(), session_id=second.name))

        def poll(self):
            return 0

    monkeypatch.setattr('aegisub_mcp.sessions.launch_window_helper', lambda command, result: Launcher(command))
    try:
        async with create_connected_server_and_client_session(create_server(manager)) as client:
            tools = (await client.list_tools()).tools
            assert 'open_window' in {tool.name for tool in tools}
            assert not calls  # Discovery never starts a window.
            result = await client.call_tool('open_window', {})
            assert not result.isError, result.content
            assert json.loads(result.content[0].text)['session_id'] == second.name
            assert len(calls) == 1
            if os.name == 'nt':
                assert calls[0][0][0] == sys._base_executable
                assert calls[0][0][1] == '-c'
                assert 'aegisub_mcp.window_launcher' in calls[0][0][2]
            else:
                assert calls[0][0][:3] == [sys.executable, '-m', 'aegisub_mcp.window_launcher']
            assert '--allow-write' in calls[0][0]
            assert (first / 'session.json').is_file()
    finally:
        manager.close()


async def test_attach_with_executable_initializes_without_launching_it(tmp_path):
    exe = tmp_path / 'must-not-start.exe'
    exe.touch()
    params = StdioServerParameters(command=sys.executable, args=['-m', 'aegisub_mcp.server',
        '--attach', '--aegisub', str(exe), '--allow-write'])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert {'open_window', 'list_sessions', 'connect_session'} <= tools


@pytest.mark.parametrize('project', ['relative.ass', ''])
async def test_invalid_project_does_not_launch(tmp_path, monkeypatch, project):
    exe = tmp_path / 'aegisub.exe'
    exe.touch()
    manager = SessionManager(True, tmp_path, exe)
    monkeypatch.setattr('aegisub_mcp.sessions.subprocess.Popen',
        lambda *a, **k: pytest.fail('Invalid input must never launch a process'))
    with pytest.raises(BridgeError, match='absolute'):
        await manager.open_window(project)


async def test_cancelled_launch_is_not_retried_or_terminated(tmp_path, monkeypatch):
    exe = tmp_path / 'aegisub.exe'
    exe.touch()
    manager = SessionManager(True, tmp_path, exe)
    calls = []

    class Launcher:
        def __init__(self, *args, **kwargs):
            calls.append(args)

        def poll(self):
            return None

    monkeypatch.setattr('aegisub_mcp.sessions.launch_window_helper', lambda command, result: Launcher(command))
    task = asyncio.create_task(manager.open_window())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 1
    assert manager._pending == 0


async def test_failed_launcher_leaves_current_window_connected(tmp_path, monkeypatch):
    exe = tmp_path / 'aegisub.exe'
    exe.touch()
    first = fixture_window(tmp_path, 'a' * 32)
    manager = SessionManager(True, tmp_path, exe)
    manager.connect(first.name)

    class Launcher:
        def __init__(self, command, **kwargs):
            from pathlib import Path
            atomic_json(Path(command[command.index('--result') + 1]), {'error': 'not ready; inspect GUI'})

        def poll(self):
            return 0

    monkeypatch.setattr('aegisub_mcp.sessions.launch_window_helper', lambda command, result: Launcher(command))
    try:
        with pytest.raises(BridgeError, match='inspect GUI'):
            await manager.open_window()
        assert manager.session_id == first.name
        assert manager.read_context()['revision'] == 1
    finally:
        manager.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows job lifecycle')
def test_windows_always_uses_desktop_broker_without_launching_in_job(tmp_path, monkeypatch):
    result = tmp_path / 'result.json'
    calls = []

    def denied(*args, **kwargs):
        pytest.fail('Never create a child in the MCP client job on Windows')

    def desktop(command):
        calls.append(command)

    monkeypatch.setattr('aegisub_mcp.sessions.subprocess.Popen', denied)
    monkeypatch.setattr('aegisub_mcp.windows_launch.launch_through_desktop', desktop)
    process = launch_window_helper([sys.executable, '-m', 'example'], result)
    assert len(calls) == 1
    assert process.poll() is None
    result.write_text('{}')
    assert process.poll() == 0
