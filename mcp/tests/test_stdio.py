"""Exercise the actual MCP stdio framing in a separate server process."""
import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from aegisub_mcp.bridge import atomic_json


async def test_stdio_initialize_list_and_unicode_tool_call(tmp_path):
    code = (
        "import sys; from pathlib import Path; "
        "from aegisub_mcp.bridge import Bridge; "
        "from aegisub_mcp.server import create_server; "
        "create_server(Bridge(Path(sys.argv[1]), 'stdio-test')).run(transport='stdio')"
    )
    params = StdioServerParameters(command=sys.executable, args=["-c", code, str(tmp_path)])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            initialized = await client.initialize()
            assert initialized.serverInfo.name == "Aegisub"
            assert len((await client.list_tools()).tools) == 3
            call = asyncio.create_task(client.call_tool("get_document", {}))
            for _ in range(400):
                if (tmp_path / "request.json").exists():
                    break
                await asyncio.sleep(0.01)
            request = json.loads((tmp_path / "request.json").read_text(encoding="utf-8"))
            assert request["token"] == "stdio-test"
            atomic_json(tmp_path / f'{request["id"]}.json', {
                "id": request["id"], "result": {"filename": "日本語.ass", "revision": 3},
            })
            result = await call
            assert not result.isError
            assert "日本語.ass" in result.content[0].text
