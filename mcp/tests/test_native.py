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
        for method in ("undo", "create_lines", "delete_lines", "create_style", "update_style"):
            with pytest.raises(BridgeError, match="Read-only"):
                await session.bridge.call(method, dict(expected_revision=document["revision"]))
        session.bridge.token = "wrong-token"
        with pytest.raises(BridgeError, match="session token"):
            await session.bridge.call("get_document")
    finally:
        session.process.terminate()
        session.process.wait(timeout=10)
        session.close()


async def test_native_layered_effects_styles_and_history():
    session = LaunchedSession(Path(EXE), FIXTURE, True)
    session.bridge.timeout = 60
    bridge = session.bridge

    async def lines():
        return await bridge.call("list_lines", dict(offset=0, limit=100, selected_only=False))

    try:
        original = await lines()
        first, second = [line["id"] for line in original["lines"]]
        default = (await bridge.call("list_styles"))["styles"][0]
        created = await bridge.call("create_style", dict(expected_revision=original["revision"],
            name="Glow", base_style="Default", properties={"primary_color": "&H0000FFFF",
                "font_size": 60, "outline_width": 6.5, "bold": True}))
        assert created["style"]["font_size"] == 60
        assert created["style"]["font"] == default["font"]
        assert created["style"]["primary_color"] == "&H0000FFFF"
        await bridge.call("undo", dict(expected_revision=created["revision"]))
        undone_style = await bridge.call("list_styles")
        assert [style["name"] for style in undone_style["styles"]] == ["Default"]
        created = await bridge.call("redo", dict(expected_revision=undone_style["revision"]))
        with pytest.raises(BridgeError, match="already exists"):
            await bridge.call("create_style", dict(expected_revision=created["revision"], name="glow", properties={}))
        with pytest.raises(BridgeError, match="Unknown base"):
            await bridge.call("create_style", dict(expected_revision=created["revision"], name="Missing", base_style="NoSuchStyle", properties={}))
        for properties in ({"primary_color": "red"}, {"font_size": True}, {"border_style": 2}, {"unknown": 1}):
            with pytest.raises(BridgeError):
                await bridge.call("update_style", dict(expected_revision=created["revision"], name="Glow", properties=properties))
        assert (await bridge.call("get_document"))["revision"] == created["revision"]
        updated = await bridge.call("update_style", dict(expected_revision=created["revision"],
            name="Glow", properties={"outline_color": "&H80FF0000", "alignment": 8}))
        assert updated["style"]["font_size"] == 60
        assert updated["style"]["outline_color"] == "&H80FF0000"
        await bridge.call("undo", dict(expected_revision=updated["revision"]))
        styles = await bridge.call("list_styles")
        assert styles["styles"][-1]["outline_color"] == default["outline_color"]
        await bridge.call("redo", dict(expected_revision=styles["revision"]))
        current = await lines()
        effect = dict(text=r"{\blur6\fad(300,500)}Hello", start_ms=1000, end_ms=2000, style="Glow", layer=1)
        with pytest.raises(BridgeError, match="Unknown style"):
            await bridge.call("create_lines", dict(expected_revision=current["revision"],
                lines=[effect, dict(effect, style="Missing")]))
        assert (await lines())["total"] == 2
        assert (await lines())["revision"] == current["revision"]
        added = await bridge.call("create_lines", dict(expected_revision=current["revision"], after_id=first,
            lines=[effect, dict(effect, text=r"{\fad(300,500)}Hello", layer=2)]))
        ids = added["created_ids"]
        assert len(set(ids)) == 2 and first not in ids
        assert [line["id"] for line in (await lines())["lines"]] == [first, *ids, second]
        assert (await bridge.call("get_document"))["selected_ids"] == ids
        await bridge.call("undo", dict(expected_revision=added["revision"]))
        undone_lines = await lines()
        assert [line["id"] for line in undone_lines["lines"]] == [first, second]
        added = await bridge.call("redo", dict(expected_revision=undone_lines["revision"]))
        assert [line["id"] for line in (await lines())["lines"]] == [first, *ids, second]
        with pytest.raises(BridgeError, match="revision conflict"):
            await bridge.call("delete_lines", dict(expected_revision=current["revision"], ids=ids))
        for invalid_ids in ([ids[0], ids[0]], [ids[0], 2147483647]):
            with pytest.raises(BridgeError):
                await bridge.call("delete_lines", dict(expected_revision=added["revision"], ids=invalid_ids))
        assert (await lines())["total"] == 4
        deleted = await bridge.call("delete_lines", dict(expected_revision=added["revision"], ids=ids))
        assert deleted["deleted"] == 2 and deleted["replacement_id"] is None
        assert [line["text"] for line in (await lines())["lines"]] == ["Hello", "World"]
        await bridge.call("undo", dict(expected_revision=deleted["revision"]))
        restored = await lines()
        assert [line["id"] for line in restored["lines"]] == [first, *ids, second]
        await bridge.call("redo", dict(expected_revision=restored["revision"]))
        remaining = await lines()
        cleared = await bridge.call("delete_lines", dict(expected_revision=remaining["revision"], ids=[first, second]))
        blank = await lines()
        assert blank["total"] == 1
        assert blank["lines"][0]["id"] == cleared["replacement_id"]
        assert blank["lines"][0]["text"] == ""
        assert (await bridge.call("get_document"))["active_id"] == cleared["replacement_id"]
        await bridge.call("undo", dict(expected_revision=cleared["revision"]))
        assert [line["text"] for line in (await lines())["lines"]] == ["Hello", "World"]
    finally:
        session.process.terminate()
        session.process.wait(timeout=10)
        session.close()
