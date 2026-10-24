import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from aegisub_mcp.server import create_server
from aegisub_mcp.sessions import SessionManager
from test_server import RecordingBridge
from test_sessions import make_session


async def test_batch_schema_validates_and_preserves_operation_order():
    bridge = RecordingBridge(True)
    async with create_connected_server_and_client_session(create_server(bridge)) as client:
        operations = [
            dict(method="create_style", name="Glow", properties={"font_size": 72}),
            dict(method="update_lines", updates=[dict(id=4, text=r"{\k20}花", style="Glow")]),
            dict(method="shift_times", ids=[4], delta_ms=100),
        ]
        args = dict(operation_id="a" * 32, expected_revision=7, label="Karaoke draft", operations=operations)
        result = await client.call_tool("propose_batch", args)
        assert not result.isError, result.content
        assert bridge.calls[-1] == ("propose_batch", args)
        for invalid in [
            dict(args, operation_id="../escape"),
            dict(args, operations=[dict(method="undo")]),
            dict(args, operations=[dict(method="update_style", name="Default", properties={})]),
            dict(args, operations=[dict(method="shift_times", ids=[4], delta_ms=1)]),
            dict(args, operations=[dict(method="delete_lines", ids=list(range(500)))] * 2),
            dict(args, operations=[dict(method="update_lines", updates=[dict(id=4, text=None)])]),
        ]:
            assert (await client.call_tool("propose_batch", invalid)).isError
        assert len(bridge.calls) == 1


async def test_client_identity_from_real_mcp_initialize_and_handoff(tmp_path):
    directory = make_session(tmp_path)
    manager = SessionManager(True, tmp_path)
    async with create_connected_server_and_client_session(create_server(manager)) as client:
        result = await client.call_tool("connect_session", {"session_id": directory.name})
        assert not result.isError, result.content
        identity = json.loads((directory / "client.json").read_text())
        assert identity["name"] != "MCP client"
        assert identity["pid"] > 0
        assert not (await client.call_tool("disconnect_session", {})).isError
        assert not (directory / "client.json").exists()
        # Metadata updates must never reconnect a handed-off client.
        assert (await client.call_tool("get_agent_status", {})).isError
        assert manager.lease is None


def test_identity_is_removed_before_releasing_lease(tmp_path):
    from aegisub_mcp.sessions import ClientLease
    directory = make_session(tmp_path)
    first = ClientLease(directory)
    first.set_client_info("Claude\nDesktop", "1.2")
    assert json.loads((directory / "client.json").read_text())["name"] == "ClaudeDesktop"
    first.close()
    second = ClientLease(directory)
    second.set_client_info("Codex", "3")
    first.close()  # A stale close must not erase the new owner's identity.
    assert json.loads((directory / "client.json").read_text())["name"] == "Codex"
    second.close()
