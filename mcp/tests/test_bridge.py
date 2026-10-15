import asyncio
import json
import time

import pytest

from aegisub_mcp.bridge import Bridge, BridgeError, atomic_json


async def respond(directory, result=None, error=None, wrong_id=False):
    request_path = directory / "request.json"
    for _ in range(400):
        if request_path.exists():
            break
        await asyncio.sleep(0.005)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["token"] == "test-secret"
    assert request["expires_ms"] > time.time() * 1000
    request_path.unlink()
    response = {"id": "wrong" if wrong_id else request["id"]}
    response["error" if error else "result"] = error or result or {"revision": 1}
    atomic_json(directory / (request["id"] + ".json"), response)
    return request


async def test_unicode_and_serialized_requests(tmp_path):
    bridge = Bridge(tmp_path, "test-secret", allow_write=True)
    async def worker():
        first = await respond(tmp_path, {"text": "日本語 \\N Tiếng Việt"})
        second = await respond(tmp_path)
        return first, second
    task = asyncio.create_task(worker())
    results = await asyncio.gather(bridge.call("list_lines"), bridge.call("get_document"))
    first, second = await task
    assert results[0]["text"] == "日本語 \\N Tiếng Việt"
    assert first["id"] != second["id"]
    assert not list(tmp_path.iterdir())


async def test_application_error_does_not_poison_session(tmp_path):
    bridge = Bridge(tmp_path, "test-secret", allow_write=True)
    task = asyncio.create_task(respond(tmp_path, error="Document revision conflict"))
    with pytest.raises(BridgeError, match="revision conflict"):
        await bridge.call("update_lines")
    await task
    task = asyncio.create_task(respond(tmp_path))
    assert (await bridge.call("get_document"))["revision"] == 1
    await task


async def test_timeout_fails_closed_and_cleans_request(tmp_path):
    bridge = Bridge(tmp_path, "test-secret", timeout=0.05)
    with pytest.raises(BridgeError, match="did not respond"):
        await bridge.call("get_document")
    assert not list(tmp_path.iterdir())
    with pytest.raises(BridgeError, match="disconnected"):
        await bridge.call("get_document")


async def test_cancel_fails_closed(tmp_path):
    bridge = Bridge(tmp_path, "test-secret")
    task = asyncio.create_task(bridge.call("get_document"))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(BridgeError, match="disconnected"):
        await bridge.call("get_document")
    assert not list(tmp_path.iterdir())


async def test_wrong_id_fails_closed(tmp_path):
    bridge = Bridge(tmp_path, "test-secret")
    task = asyncio.create_task(respond(tmp_path, wrong_id=True))
    with pytest.raises(BridgeError, match="response ID"):
        await bridge.call("get_document")
    await task
    with pytest.raises(BridgeError, match="disconnected"):
        await bridge.call("get_document")


async def test_read_only_and_unknown_method_never_publish(tmp_path):
    bridge = Bridge(tmp_path, "test-secret")
    with pytest.raises(BridgeError, match="Read-only"):
        await bridge.call("update_lines")
    with pytest.raises(BridgeError, match="Unknown"):
        await bridge.call("execute_lua")
    assert not list(tmp_path.iterdir())


async def test_oversized_request_does_not_poison_session(tmp_path):
    bridge = Bridge(tmp_path, "test-secret", allow_write=True)
    with pytest.raises(BridgeError, match="4 MiB"):
        await bridge.call("update_lines", {"text": "x" * (4 * 1024 * 1024)})
    task = asyncio.create_task(respond(tmp_path))
    assert (await bridge.call("get_document"))["revision"] == 1
    await task
