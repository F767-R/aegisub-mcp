import asyncio
import base64
import json

from mcp.server.fastmcp import Context
from mcp.shared.memory import create_connected_server_and_client_session
import pytest

from aegisub_mcp.server import create_server


class LiveBridge:
    allow_write = True

    def __init__(self):
        self.context = dict(session_id="a" * 32, revision=7, context_version=10, selected_ids=[2])
        self.calls = []

    def read_context(self):
        return dict(self.context)

    async def call(self, method, args=None):
        self.calls.append((method, args))
        if method == "render_preview":
            return {"revision": 7, "width": 1, "height": 1,
                    "png_base64": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()}
        return self.read_context()


async def test_context_resource_wait_and_preview_image_content():
    bridge = LiveBridge()
    async with create_connected_server_and_client_session(create_server(bridge)) as client:
        resources = (await client.list_resources()).resources
        assert str(resources[0].uri) == "aegisub://context"
        resource = await client.read_resource("aegisub://context")
        assert json.loads(resource.contents[0].text)["context_version"] == 10
        result = await client.call_tool("wait_for_context", dict(after_version=10, timeout_ms=0))
        assert json.loads(result.content[0].text)["changed"] is False
        bridge.context["context_version"] = 11
        result = await client.call_tool("wait_for_context", dict(after_version=10, timeout_ms=100))
        assert json.loads(result.content[0].text)["changed"] is True
        assert not bridge.calls  # Watching never occupies the native edit mailbox.
        result = await client.call_tool("render_preview", {"time_ms": 1500})
        assert not result.isError
        assert result.content[1].type == "image"
        assert result.content[1].mimeType == "image/png"
        assert base64.b64decode(result.content[1].data).startswith(b"\x89PNG")


async def test_subscription_notifies_after_native_context_change_and_unsubscribes():
    bridge = LiveBridge()
    server = create_server(bridge)
    # Capture the actual ServerSession notification while exercising MCP handlers.
    async with create_connected_server_and_client_session(server) as client:
        notices = []

        @server.tool()
        async def capture(ctx: Context) -> bool:
            original = ctx.session.send_resource_updated

            async def observed(uri):
                notices.append(str(uri))
                await original(uri)

            ctx.session.send_resource_updated = observed
            return True

        await client.call_tool("capture", {})
        await client.subscribe_resource("aegisub://context")
        bridge.context["context_version"] += 1
        async with asyncio.timeout(3):
            while not notices:
                await asyncio.sleep(0.05)
        assert notices == ["aegisub://context"]
        await client.unsubscribe_resource("aegisub://context")
        bridge.context["context_version"] += 1
        await asyncio.sleep(0.35)
        assert len(notices) == 1


@pytest.mark.parametrize("name,args", [
    ("render_preview", {"max_width": 4000}),
    ("render_preview", {"time_ms": -1}),
    ("seek_video", {"expected_revision": True, "time_ms": 0}),
    ("play_audio", {"expected_revision": 7, "start_ms": 0, "end_ms": 30010}),
    ("play_audio", {"expected_revision": 7, "start_ms": 1000, "end_ms": 0}),
    ("wait_for_context", {"after_version": 0, "timeout_ms": 10001}),
])
async def test_invalid_media_and_wait_arguments_never_reach_native(name, args):
    bridge = LiveBridge()
    async with create_connected_server_and_client_session(create_server(bridge)) as client:
        assert (await client.call_tool(name, args)).isError
    assert not bridge.calls
