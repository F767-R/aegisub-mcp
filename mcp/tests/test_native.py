"""Real native GUI integration; set AEGISUB_MCP_TEST_EXE to a built fork.

Only disposable fixture instances are terminated by these tests.
"""
import os
import base64
import struct
import asyncio
import json
import signal
import sys
import time
import uuid
from pathlib import Path

import pytest

from aegisub_mcp.bridge import BridgeError, LaunchedSession, atomic_json
from aegisub_mcp.sessions import SessionManager
from aegisub_mcp.sessions import process_alive

EXE = os.environ.get("AEGISUB_MCP_TEST_EXE")
pytestmark = pytest.mark.skipif(not EXE, reason="Native fork executable not supplied")
FIXTURE = Path(__file__).parent / "fixtures" / "example.ass"


@pytest.mark.skipif(sys.platform != "win32", reason="Exercise the native Windows menu command")
async def test_native_menu_handoff_preserves_unsaved_document_and_history():
    import ctypes
    from ctypes import wintypes

    # Drive only this test's disposable GUI through its real menu command.
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetMenu.argtypes, user.GetMenu.restype = [wintypes.HWND], wintypes.HMENU
    user.GetSubMenu.argtypes, user.GetSubMenu.restype = [wintypes.HMENU, ctypes.c_int], wintypes.HMENU
    user.GetMenuItemCount.argtypes = [wintypes.HMENU]
    user.GetMenuStringW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.LPWSTR, ctypes.c_int, wintypes.UINT]
    user.GetMenuItemID.argtypes, user.GetMenuItemID.restype = [wintypes.HMENU, ctypes.c_int], wintypes.UINT
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]

    def find_command(menu):
        for index in range(user.GetMenuItemCount(menu)):
            label = ctypes.create_unicode_buffer(512)
            user.GetMenuStringW(menu, index, label, len(label), 0x400)
            if label.value.replace("&", "").startswith("Disconnect current agent (allow another)"):
                return user.GetMenuItemID(menu, index)
            child = user.GetSubMenu(menu, index)
            if child:
                result = find_command(child)
                if result is not None:
                    return result
        return None

    session = LaunchedSession(Path(EXE), FIXTURE, True)
    first, second = SessionManager(True), SessionManager(True)
    try:
        session.bridge.timeout = 60
        original = await session.bridge.call("list_lines", dict(offset=0, limit=100, selected_only=False))
        changed = await session.bridge.call("update_lines", dict(expected_revision=original["revision"],
            updates=[dict(id=original["lines"][0]["id"], text="Unsaved handoff")]))
        session.close()
        first.connect(session.directory.name)
        found = []

        @callback_type
        def visit(window, unused):
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(window, ctypes.byref(pid))
            if pid.value == session.process.pid:
                menu = user.GetMenu(window)
                command = find_command(menu) if menu else None
                if command is not None:
                    found.append((window, command))
            return True

        user.EnumWindows(visit, 0)
        assert len(found) == 1, "Handoff menu command missing from the native window"
        assert user.PostMessageW(found[0][0], 0x111, found[0][1], 0)  # WM_COMMAND
        async with asyncio.timeout(10):
            while True:
                replacements = [s for s in second.list_sessions()
                    if s["pid"] == session.process.pid and s["session_id"] != session.directory.name]
                if replacements:
                    break
                await asyncio.sleep(0.05)
        with pytest.raises(BridgeError, match="disconnected"):
            first.read_context()
        current = second.connect(replacements[0]["session_id"])
        assert current["modified"] and current["revision"] == changed["revision"]
        assert session.process.poll() is None
        lines = await second.call("list_lines", dict(offset=0, limit=100, selected_only=False))
        assert lines["lines"][0]["text"] == "Unsaved handoff"
        for _ in range(3):
            with pytest.raises(BridgeError, match="explicitly connect_session"):
                first.read_context()
        await second.call("undo", dict(expected_revision=current["revision"]))
        restored = await second.call("list_lines", dict(offset=0, limit=100, selected_only=False))
        assert restored["lines"][0]["text"] == original["lines"][0]["text"]
    finally:
        first.close()
        second.close()
        session.process.terminate()
        session.process.wait(timeout=10)
        session.close()


async def test_native_explicit_window_survives_stdio_client_cleanup():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    # The SDK's POSIX safe environment excludes DISPLAY/XAUTHORITY, which are
    # needed to reach the test's Xvfb desktop. Do not forward unrelated secrets.
    desktop_env = {name: os.environ[name] for name in
        ('DISPLAY', 'XAUTHORITY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR', 'LD_LIBRARY_PATH')
        if name in os.environ}
    params = StdioServerParameters(command=sys.executable, args=[
        '-m', 'aegisub_mcp.server', '--attach', '--aegisub', str(Path(EXE).resolve()), '--allow-write'],
        env=desktop_env)
    pid = None
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                assert 'open_window' in {tool.name for tool in (await client.list_tools()).tools}
                result = await client.call_tool('open_window', {'project_file': str(FIXTURE.resolve())})
                assert not result.isError, result.content
                opened = json.loads(result.content[0].text)
                pid = opened['pid']
                assert process_alive(pid)
        await asyncio.sleep(0.3)
        assert process_alive(pid), 'MCP client process-job cleanup closed the GUI'
    finally:
        # This PID belongs only to the disposable window created by this test.
        if pid and process_alive(pid):
            os.kill(pid, signal.SIGTERM)


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
        for method in ("undo", "create_lines", "delete_lines", "create_style", "update_style",
                       "seek_video", "play_audio", "stop_playback"):
            with pytest.raises(BridgeError, match="Read-only"):
                await session.bridge.call(method, dict(expected_revision=document["revision"]))
        session.bridge.token = "wrong-token"
        with pytest.raises(BridgeError, match="session token"):
            await session.bridge.call("get_document")
    finally:
        session.process.terminate()
        session.process.wait(timeout=10)
        session.close()


async def test_native_reconnect_context_and_rendered_preview(tmp_path):
    # CI enables loading fixture-linked media. The dummy provider needs no codecs
    # or external files and has matching script/video resolution to avoid prompts.
    fixture = tmp_path / "preview.ass"
    source = FIXTURE.read_text(encoding="utf-8")
    source = source.replace("PlayResY: 1080", "PlayResY: 1080\nLayoutResX: 1920\nLayoutResY: 1080\nYCbCr Matrix: None")
    source = source.replace("[V4+ Styles]", "[Aegisub Project Garbage]\nVideo File: ?dummy:25:250:1920:1080:0:0:0:\n\n[V4+ Styles]")
    fixture.write_text(source, encoding="utf-8")
    session = LaunchedSession(Path(EXE), fixture, True)
    session.bridge.timeout = 60
    manager = SessionManager(True, session.directory.parent)
    try:
        context = await session.bridge.call("get_context")
        assert context["video_loaded"]
        assert context["video_width"] == 1920
        with pytest.raises(BridgeError, match="another MCP client"):
            manager.connect(session.directory.name)
        first = (await session.bridge.call("list_lines", dict(offset=0, limit=100, selected_only=False)))["lines"][0]["id"]
        await session.bridge.call("update_lines", dict(expected_revision=context["revision"],
            updates=[dict(id=first, text=r"{\pos(960,540)\fs120}LIVE PREVIEW")]))
        current = await session.bridge.call("get_context")
        arguments = dict(time_ms=1500, max_width=320, max_height=180, raw=False)
        rendered = await session.bridge.call("render_preview", arguments)
        raw = await session.bridge.call("render_preview", dict(arguments, raw=True))
        png = base64.b64decode(rendered["png_base64"])
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", png[16:24]) == (320, 180)
        assert rendered["png_base64"] != raw["png_base64"]
        after = await session.bridge.call("get_context")
        assert after["frame"] == current["frame"]  # Preview never seeks or edits.
        assert after["revision"] == current["revision"]
        seek = await session.bridge.call("seek_video", dict(expected_revision=after["revision"], time_ms=3000))
        assert seek["revision"] == after["revision"]
        assert seek["context_version"] > after["context_version"]
        assert seek["time_ms"] == 3000
        session.close()
        attached = manager.connect(session.directory.name)
        assert attached["modified"]
        assert session.process.poll() is None
        lines = await manager.call("list_lines", dict(offset=0, limit=100, selected_only=False))
        assert "LIVE PREVIEW" in lines["lines"][0]["text"]
        manager.close()
        assert (session.directory / "session.json").is_file()
    finally:
        manager.close()
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


async def test_native_review_batches_lost_response_handoff_and_single_undo(tmp_path):
    fixture = tmp_path / "batch-preview.ass"
    source = FIXTURE.read_text(encoding="utf-8")
    source = source.replace("PlayResY: 1080", "PlayResY: 1080\nLayoutResX: 1920\nLayoutResY: 1080\nYCbCr Matrix: None")
    source = source.replace("[V4+ Styles]", "[Aegisub Project Garbage]\nVideo File: ?dummy:25:250:1920:1080:0:0:0:\n\n[V4+ Styles]")
    fixture.write_text(source, encoding="utf-8")
    session = LaunchedSession(Path(EXE), fixture, True)
    session.bridge.timeout = 60
    bridge = session.bridge
    manager = SessionManager(True, session.directory.parent)
    async def lines(target=bridge):
        return await target.call("list_lines", dict(offset=0, limit=100, selected_only=False))
    try:
        original = await lines()
        first, second = [line["id"] for line in original["lines"]]
        rev = original["revision"]
        document = await bridge.call("get_document")
        bridge.set_client_info("Codex test", "0.4")
        status = await bridge.call("get_agent_status")
        assert status["ownership"] == "connected"
        assert status["client_name"] == "Codex test" and status["native_ready"]
        operation = dict(operation_id=uuid.uuid4().hex, expected_revision=rev, label="Reviewed effect",
            operations=[
                dict(method="create_style", name="Draft", properties={"font_size": 140}),
                dict(method="update_lines", updates=[dict(id=first, text="PROPOSED", style="Draft")]),
                dict(method="shift_times", ids=[first], delta_ms=100),
                dict(method="delete_lines", ids=[second]),
                dict(method="create_lines", lines=[dict(text="New row", start_ms=4000, end_ms=5000, style="Draft")]),
            ])
        prepared = await bridge.call("propose_batch", operation)
        assert prepared["status"] == "prepared" and prepared["revision"] == rev
        assert (await lines()) == original
        assert (await bridge.call("get_document")) == document
        assert (await bridge.call("propose_batch", operation)) == prepared
        with pytest.raises(BridgeError, match="different proposal"):
            await bridge.call("propose_batch", dict(operation, label="Changed"))
        # Invalid final operation must not leak preceding staged edits.
        bad = dict(operation, operation_id=uuid.uuid4().hex,
            operations=operation["operations"] + [dict(method="update_lines", updates=[dict(id=2147483647, text="bad")])])
        with pytest.raises(BridgeError, match="Unknown subtitle"):
            await bridge.call("propose_batch", bad)
        assert (await lines()) == original
        preview_args = dict(time_ms=1500, max_width=320, max_height=180, raw=False)
        before = await bridge.call("render_preview", preview_args)
        after = await bridge.call("render_preview", dict(preview_args, operation_id=operation["operation_id"]))
        assert before["png_base64"] != after["png_base64"]
        assert (await bridge.call("render_preview", preview_args))["png_base64"] == before["png_base64"]
        assert (await lines()) == original
        cancelled = dict(operation, operation_id=uuid.uuid4().hex)
        await bridge.call("propose_batch", cancelled)
        assert (await bridge.call("cancel_batch", {"operation_id": cancelled["operation_id"]}))["status"] == "cancelled"
        with pytest.raises(BridgeError, match="cancelled or stale"):
            await bridge.call("apply_batch", dict(operation_id=cancelled["operation_id"], expected_revision=rev))
        assert (await lines()) == original
        # Publish an application, then deliberately discard its response before a
        # client consumes it. The next process must recover from native history.
        request_id = uuid.uuid4().hex
        atomic_json(session.directory / "request.json", dict(id=request_id, token=bridge.token,
            method="apply_batch", args=dict(operation_id=operation["operation_id"], expected_revision=rev),
            expires_ms=int((time.time() + 60) * 1000)))
        response = session.directory / f"{request_id}.json"
        async with asyncio.timeout(60):
            while not response.exists():
                await asyncio.sleep(0.05)
        response.unlink()
        session.close()
        manager.set_client_info("Claude test", "0.4")
        manager.connect(session.directory.name)
        receipt = await manager.call("get_operation", dict(operation_id=operation["operation_id"]))
        assert receipt["status"] == "applied"
        assert receipt["applied_revision"] == rev + 1
        assert receipt["window_id"] == document["window_id"]
        status = await manager.call("get_agent_status")
        assert status["client_name"] == "Claude test" and status["ownership"] == "connected"
        applied = await lines(manager)
        assert [line["text"] for line in applied["lines"]] == ["PROPOSED", "New row"]
        assert applied["lines"][0]["id"] == first
        assert applied["lines"][0]["start_ms"] == 1100
        repeated = await manager.call("apply_batch", dict(operation_id=operation["operation_id"], expected_revision=rev))
        assert repeated["applied_revision"] == receipt["applied_revision"]
        assert await lines(manager) == applied
        await manager.call("undo", dict(expected_revision=applied["revision"]))
        undone = await lines(manager)
        assert undone["lines"] == original["lines"]
        assert [style["name"] for style in (await manager.call("list_styles"))["styles"]] == ["Default"]
        # Retry after Undo must return history, not replay the application.
        await manager.call("apply_batch", dict(operation_id=operation["operation_id"], expected_revision=undone["revision"]))
        assert await lines(manager) == undone
        await manager.call("redo", dict(expected_revision=undone["revision"]))
        assert (await lines(manager))["lines"] == applied["lines"]
        current = await lines(manager)
        stale = dict(operation, operation_id=uuid.uuid4().hex, expected_revision=current["revision"],
                     operations=[dict(method="update_lines", updates=[dict(id=first, text="Stale")])])
        await manager.call("propose_batch", stale)
        await manager.call("update_lines", dict(expected_revision=current["revision"], updates=[dict(id=first, text="Manual edit")]))
        with pytest.raises(BridgeError, match="cancelled or stale"):
            await manager.call("apply_batch", dict(operation_id=stale["operation_id"], expected_revision=stale["expected_revision"]))
        assert (await lines(manager))["lines"][0]["text"] == "Manual edit"
        assert (await manager.call("get_operation", dict(operation_id=stale["operation_id"])))["status"] == "stale"
        assert (await manager.call("get_operation", dict(operation_id=uuid.uuid4().hex)))["status"] == "unknown"
    finally:
        manager.close()
        session.process.terminate()
        session.process.wait(timeout=10)
        session.close()
