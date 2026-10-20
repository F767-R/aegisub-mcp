"""MCP resource subscriptions backed by native context events, never edit retries."""
from contextlib import asynccontextmanager, suppress
import asyncio
import json

from pydantic import AnyUrl

CONTEXT_URI = "aegisub://context"


class LiveContext:
    def __init__(self, bridge):
        self.bridge = bridge
        self.subscribers = {}

    async def watch(self):
        while True:
            await asyncio.sleep(0.25)
            if not self.subscribers:
                continue
            try:
                context = self.bridge.read_context()
                version = (context.get("session_id"), context["context_version"])
            except (OSError, ValueError, RuntimeError):
                # Modal dialogs and disconnected windows must not publish mailbox
                # requests or poison the edit transport. Reads resume on reconnect.
                continue
            for session, previous in list(self.subscribers.items()):
                if previous == version:
                    continue
                try:
                    await session.send_resource_updated(AnyUrl(CONTEXT_URI))
                    self.subscribers[session] = version
                except Exception:
                    self.subscribers.pop(session, None)

    @asynccontextmanager
    async def lifespan(self, server):
        task = asyncio.create_task(self.watch())
        try:
            yield {}
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self.subscribers.clear()

    def register(self, server):
        @server.resource(CONTEXT_URI, name="Live Aegisub editing context", mime_type="application/json")
        async def context_resource() -> str:
            return json.dumps(self.bridge.read_context(), ensure_ascii=False)

        # FastMCP 1.x registers resources but does not advertise subscription
        # support. Keep this small compatibility adapter covered by protocol tests.
        low = server._mcp_server
        original_capabilities = low.get_capabilities

        def capabilities(*args, **kwargs):
            result = original_capabilities(*args, **kwargs)
            result.resources.subscribe = True
            return result

        low.get_capabilities = capabilities

        @low.subscribe_resource()
        async def subscribe(uri):
            if str(uri).rstrip("/") != CONTEXT_URI:
                raise ValueError("Unknown resource")
            context = self.bridge.read_context()
            self.subscribers[low.request_context.session] = (
                context.get("session_id"), context["context_version"])

        @low.unsubscribe_resource()
        async def unsubscribe(uri):
            if str(uri).rstrip("/") != CONTEXT_URI:
                raise ValueError("Unknown resource")
            self.subscribers.pop(low.request_context.session, None)
