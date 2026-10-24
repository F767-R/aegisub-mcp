import pytest
from pydantic import ValidationError
from mcp.shared.memory import create_connected_server_and_client_session

from aegisub_mcp.server import LineUpdate, create_server


class RecordingBridge:
    def __init__(self, allow_write):
        self.allow_write = allow_write
        self.calls = []

    async def call(self, method, args=None):
        self.calls.append((method, args))
        return {"revision": 7, "method": method}


async def test_protocol_initialize_discovery_and_call():
    bridge = RecordingBridge(True)
    async with create_connected_server_and_client_session(create_server(bridge)) as client:
        tools = (await client.list_tools()).tools
        assert {t.name for t in tools} == {
            "get_document", "list_lines", "list_styles", "update_lines",
            "shift_times", "select_lines", "undo", "redo",
            "create_lines", "delete_lines", "create_style", "update_style",
            "get_context", "wait_for_context", "render_preview", "seek_video", "play_audio", "stop_playback",
            "get_agent_status", "get_operation", "propose_batch", "apply_batch", "cancel_batch",
        }
        assert next(t for t in tools if t.name == "get_document").annotations.readOnlyHint
        assert next(t for t in tools if t.name == "delete_lines").annotations.destructiveHint
        result = await client.call_tool("update_lines", {
            "expected_revision": 7, "updates": [{"id": 2, "text": "翻訳 \\N hello"}],
        })
        assert not result.isError
        assert bridge.calls[-1] == ("update_lines", {
            "expected_revision": 7, "updates": [{"id": 2, "text": "翻訳 \\N hello"}],
        })


async def test_read_only_discovery_and_unknown_tool():
    bridge = RecordingBridge(False)
    async with create_connected_server_and_client_session(create_server(bridge)) as client:
        assert {t.name for t in (await client.list_tools()).tools} == {
            "get_document", "list_lines", "list_styles", "get_context", "wait_for_context", "render_preview",
            "get_agent_status", "get_operation"}
        result = await client.call_tool("undo", {"expected_revision": 1})
        assert result.isError
        assert not bridge.calls


@pytest.mark.parametrize("name,args", [
    ("list_lines", {"limit": 501}),
    ("list_lines", {"offset": -1}),
    ("update_lines", {"expected_revision": 1, "updates": []}),
    ("update_lines", {"expected_revision": True, "updates": [{"id": 1, "text": "x"}]}),
    ("shift_times", {"expected_revision": 1, "ids": [1], "delta_ms": 7}),
    ("select_lines", {"expected_revision": 1, "ids": [], "active_id": 1}),
    ("create_lines", {"expected_revision": 1, "lines": []}),
    ("create_lines", {"expected_revision": 1, "lines": [{"text": "x", "start_ms": 10}]}),
    ("create_lines", {"expected_revision": 1, "lines": [{"text": "x", "start_ms": 100, "end_ms": 50}]}),
    ("create_lines", {"expected_revision": 1, "after_id": True, "lines": [{"text": "x", "start_ms": 0, "end_ms": 10}]}),
    ("delete_lines", {"expected_revision": 1, "ids": []}),
    ("create_style", {"expected_revision": 1, "name": "bad,name", "properties": {}}),
    ("create_style", {"expected_revision": 1, "name": " ", "properties": {}}),
    ("create_style", {"expected_revision": 1, "name": "Glow", "properties": {"primary_color": "red"}}),
    ("update_style", {"expected_revision": 1, "name": "Default", "properties": {}}),
    ("update_style", {"expected_revision": 1, "name": "Default", "properties": {"font_size": None}}),
    ("update_style", {"expected_revision": 1, "name": "Default", "properties": {"font_size": True}}),
    ("update_style", {"expected_revision": 1, "name": "Default", "properties": {"font_size": float("inf")}}),
    ("update_style", {"expected_revision": 1, "name": "Default", "properties": {"border_style": 2}}),
    ("update_style", {"expected_revision": 1, "name": "Default", "properties": {"alignment": 10}}),
    ("update_style", {"expected_revision": 1, "name": "Default", "properties": {"font": "bad\nfont"}}),
    ("update_style", {"expected_revision": 1, "name": "Default", "properties": {"unknown": 1}}),
])
async def test_invalid_tools_do_not_reach_bridge(name, args):
    bridge = RecordingBridge(True)
    async with create_connected_server_and_client_session(create_server(bridge)) as client:
        assert (await client.call_tool(name, args)).isError
    assert not bridge.calls


@pytest.mark.parametrize("update", [
    {"id": 1}, {"id": 1, "text": None}, {"id": 1, "text": "a\nb"},
    {"id": 1, "text": "a\0b"}, {"id": 1, "actor": "a,b"},
    {"id": 1, "start_ms": 12}, {"id": 1, "start_ms": 1000, "end_ms": 500},
    {"id": True, "text": "x"}, {"id": 1, "unknown": 2},
    {"id": 1, "text": "日" * 30000},
])
def test_invalid_updates(update):
    with pytest.raises(ValidationError):
        LineUpdate(**update)


async def test_effect_tools_preserve_tags_and_forward_only_supplied_style_properties():
    bridge = RecordingBridge(True)
    async with create_connected_server_and_client_session(create_server(bridge)) as client:
        for method, args in [
            ("create_style", dict(expected_revision=7, name="Glow", base_style="Default",
                                  properties={"primary_color": "&H0000FFFF", "font_size": 60})),
            ("update_style", dict(expected_revision=7, name="Glow", properties={"outline_width": 8.5})),
            ("create_lines", dict(expected_revision=7, after_id=2,
                                  lines=[dict(text=r"{\blur6\fad(300,500)}日本語", start_ms=1000,
                                              end_ms=3000, style="Glow", layer=1)])),
            ("delete_lines", dict(expected_revision=7, ids=[4, 5])),
        ]:
            result = await client.call_tool(method, args)
            assert not result.isError
            assert bridge.calls[-1] == (method, args)


@pytest.mark.parametrize("method", ["create_lines", "delete_lines", "create_style", "update_style"])
async def test_effect_tools_are_not_advertised_in_read_only_sessions(method):
    bridge = RecordingBridge(False)
    async with create_connected_server_and_client_session(create_server(bridge)) as client:
        assert (await client.call_tool(method, {})).isError
    assert not bridge.calls
