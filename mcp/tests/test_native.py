"""Real native GUI integration; set AEGISUB_MCP_TEST_EXE to a built fork.

Only disposable fixture instances are terminated by these tests.
"""
import os
from pathlib import Path

import pytest

from aegisub_mcp.bridge import BridgeError, LaunchedSession

EXE = os.environ.get("AEGISUB_MCP_TEST_EXE")
pytestmark = pytest.mark.skipif(not EXE, reason="Native fork executable not supplied")
FIXTURE = Path(__file__).parent / "fixtures" / "example.ass"


async def test_native_edits_conflicts_atomicity_and_undo():
    session = LaunchedSession(Path(EXE), FIXTURE, True)
    session.bridge.timeout = 60
    bridge = session.bridge
    async def lines():
        return await bridge.call("list_lines", dict(offset=0, limit=100, selected_only=False))
    try:
        original = await lines()
        assert [line["text"] for line in original["lines"]] == ["Hello", "World"]
        first, second = [line["id"] for line in original["lines"]]
        rev = original["revision"]
        assert (await bridge.call("list_styles"))["styles"][0]["name"] == "Default"
        # Reject the entire batch if even one line is invalid.
        with pytest.raises(BridgeError, match="Unknown subtitle"):
            await bridge.call("update_lines", dict(expected_revision=rev,
                updates=[dict(id=first, text="must not apply"), dict(id=2147483647, text="bad")]))
        assert (await lines())["lines"][0]["text"] == "Hello"
        with pytest.raises(BridgeError, match="end_ms"):
            await bridge.call("update_lines", dict(expected_revision=rev,
                updates=[dict(id=first, start_ms=2500)]))
        updated = await bridge.call("update_lines", dict(expected_revision=rev,
            updates=[dict(id=first, text="日本語 \\N Tiếng Việt", actor="MCP")]))
        assert updated["revision"] > rev
        with pytest.raises(BridgeError, match="revision conflict"):
            await bridge.call("shift_times", dict(expected_revision=rev, ids=[first], delta_ms=100))
        await bridge.call("undo", dict(expected_revision=updated["revision"]))
        undone = await lines()
        assert undone["lines"][0]["text"] == "Hello"
        await bridge.call("redo", dict(expected_revision=undone["revision"]))
        redone = await lines()
        assert redone["lines"][0]["text"] == "日本語 \\N Tiếng Việt"
        shifted = await bridge.call("shift_times", dict(expected_revision=redone["revision"],
            ids=[first, second], delta_ms=500))
        assert [line["start_ms"] for line in (await lines())["lines"]] == [1500, 3500]
        await bridge.call("select_lines", dict(expected_revision=shifted["revision"],
            ids=[second], active_id=second))
        document = await bridge.call("get_document")
        assert document["selected_ids"] == [second]
        assert document["modified"]
        session.close()
        assert session.process.poll() is None  # Disconnect must not discard the GUI.
    finally:
        session.process.terminate()
        session.process.wait(timeout=10)
        session.close()


async def test_native_token_and_read_only_enforced():
    session = LaunchedSession(Path(EXE), FIXTURE, False)
    session.bridge.timeout = 60
    try:
        document = await session.bridge.call("get_document")
        # Bypass the Python policy to exercise the native guard independently.
        session.bridge.allow_write = True
        with pytest.raises(BridgeError, match="Read-only"):
            await session.bridge.call("undo", dict(expected_revision=document["revision"]))
        session.bridge.token = "wrong-token"
        with pytest.raises(BridgeError, match="session token"):
            await session.bridge.call("get_document")
    finally:
        session.process.terminate()
        session.process.wait(timeout=10)
        session.close()
