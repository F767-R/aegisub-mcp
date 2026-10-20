import json
import asyncio
import os
import subprocess
import sys

import pytest

from aegisub_mcp.bridge import BridgeError, atomic_json
from aegisub_mcp.sessions import ClientLease, SessionManager, list_sessions, process_alive


def make_session(registry, session_id="a" * 32, writable=True):
    directory = registry / session_id
    directory.mkdir()
    atomic_json(directory / "session.json", dict(protocol_version=3, session_id=session_id,
                pid=os.getpid(), token="b" * 64, write_enabled=writable))
    atomic_json(directory / "context.json", dict(session_id=session_id, revision=4,
                context_version=8, filename="日本語.ass", modified=True, selected_ids=[5]))
    return directory


def test_discovery_omits_credentials_stale_and_malformed_sessions(tmp_path):
    directory = make_session(tmp_path)
    invalid = tmp_path / ("c" * 32)
    invalid.mkdir()
    (invalid / "session.json").write_text("[]")
    assert list_sessions(tmp_path) == [dict(session_id="a" * 32, pid=os.getpid(),
        write_enabled=True, filename="日本語.ass", modified=True, selected_ids=[5])]
    data = json.loads((directory / "session.json").read_text())
    data["pid"] = -1
    atomic_json(directory / "session.json", data)
    assert list_sessions(tmp_path) == []
    assert process_alive(os.getpid())
    assert not process_alive(-1)


def test_exclusive_lock_is_released_on_disconnect_and_process_exit(tmp_path):
    directory = make_session(tmp_path)
    lease = ClientLease(directory)
    with pytest.raises(BridgeError, match="already connected"):
        ClientLease(directory)
    code = "from pathlib import Path; from aegisub_mcp.sessions import ClientLease; import sys; ClientLease(Path(sys.argv[1]))"
    assert subprocess.run([sys.executable, "-c", code, str(directory)], capture_output=True).returncode != 0
    lease.close()
    assert subprocess.run([sys.executable, "-c", code, str(directory)], capture_output=True).returncode == 0
    ClientLease(directory).close()


def test_switching_clients_preserves_window_and_refuses_native_read_only(tmp_path):
    directory = make_session(tmp_path)
    readonly = make_session(tmp_path, "c" * 32, False)
    manager = SessionManager(True, tmp_path)
    assert manager.connect(directory.name)["revision"] == 4
    with pytest.raises(BridgeError, match="allow editing"):
        manager.connect(readonly.name)
    assert manager.session_id == directory.name
    other = SessionManager(False, tmp_path)
    with pytest.raises(BridgeError, match="another MCP client"):
        other.connect(directory.name)
    manager.close()
    assert other.connect(directory.name)["filename"] == "日本語.ass"
    other.close()
    assert (directory / "session.json").is_file()


def test_auto_selection_requires_one_window_and_rejects_path_traversal(tmp_path):
    manager = SessionManager(False, tmp_path)
    with pytest.raises(BridgeError, match="choose"):
        manager.read_context()
    first = make_session(tmp_path)
    assert manager.read_context()["session_id"] == first.name
    manager.close()
    make_session(tmp_path, "c" * 32)
    with pytest.raises(BridgeError, match="choose"):
        manager.read_context()
    with pytest.raises(BridgeError, match="Invalid session ID"):
        manager.connect("../" + first.name)


def test_rotation_revokes_existing_client(tmp_path):
    directory = make_session(tmp_path)
    manager = SessionManager(False, tmp_path)
    manager.connect(directory.name)
    data = json.loads((directory / "session.json").read_text())
    data["token"] = "d" * 64
    atomic_json(directory / "session.json", data)
    with pytest.raises(BridgeError, match="credentials changed"):
        manager.read_context()
    manager.close()


async def test_switching_does_not_release_lease_during_inflight_request(tmp_path):
    directory = make_session(tmp_path)
    second = make_session(tmp_path, "c" * 32)
    manager = SessionManager(False, tmp_path)
    manager.connect(directory.name)
    started, finish = asyncio.Event(), asyncio.Event()

    async def delayed(method, args):
        started.set()
        await finish.wait()
        return {"revision": 4}

    manager.bridge.call = delayed
    task = asyncio.create_task(manager.call("get_document"))
    try:
        await started.wait()
        with pytest.raises(BridgeError, match="current tool call"):
            manager.connect(second.name)
        with pytest.raises(BridgeError, match="already connected"):
            ClientLease(directory)
        finish.set()
        await task
        manager.connect(second.name)
    finally:
        finish.set()
        await task
        manager.close()
