"""Discover opt-in native windows and hold an OS-released exclusive client lease."""
from __future__ import annotations

import json
import asyncio
import os
from pathlib import Path
import re
import sys
import subprocess
import tempfile

from .bridge import Bridge, BridgeError, atomic_json

SESSION_ID = re.compile(r"[0-9a-f]{32}")


def launch_window_helper(command: list[str], result_path: Path):
    if os.name == 'nt':
        # BREAKAWAY can escape an inner job while retaining an outer client job.
        # Always use the independent local process provider on Windows.
        from .windows_launch import launch_through_desktop
        launch_through_desktop(command)

        class DesktopLaunch:
            def poll(self):
                return 0 if result_path.is_file() else None

        return DesktopLaunch()
    return subprocess.Popen(command, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def registry_path() -> Path:
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA")
        if not root:
            raise BridgeError("LOCALAPPDATA is unavailable")
        return Path(root) / "Aegisub" / "mcp-sessions"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Aegisub" / "mcp-sessions"
    return Path.home() / ".local" / "share" / "aegisub" / "mcp-sessions"


def process_alive(pid: int) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) is unsafe on Windows. Query a handle instead.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def read_session(directory: Path) -> dict:
    # Do not follow registry entries outside the per-user registry.
    if directory.is_symlink() or directory.resolve().parent != directory.parent.resolve():
        raise BridgeError("Invalid session directory")
    path = directory / "session.json"
    if path.is_symlink() or path.stat().st_size > 16384:
        raise BridgeError("Invalid session descriptor")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("protocol_version") != 3 or data.get("session_id") != directory.name
            or not SESSION_ID.fullmatch(directory.name)
            or not isinstance(data.get("token"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", data["token"])
            or type(data.get("write_enabled")) is not bool
            or not process_alive(data.get("pid"))):
        raise BridgeError("Session is stale or uses an incompatible native build")
    return data


class ClientLease:
    """One client per window, including across separate MCP server processes."""
    def __init__(self, directory: Path):
        self.directory = directory
        self.identity = None
        self.file = (directory / "client.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                if not self.file.read(1):
                    self.file.write(b"\0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise BridgeError("This Aegisub window is already connected to another MCP client") from exc
        try:
            self.set_client_info("MCP client", "")
        except BaseException:
            self.file.close()
            raise

    def set_client_info(self, name: str, version: str):
        # Metadata is descriptive, never a credential or proof of ownership.
        clean = lambda value: ''.join(c for c in str(value) if c.isprintable())[:128]
        identity = (clean(name), clean(version))
        if identity != self.identity:
            atomic_json(self.directory / "client.json", dict(name=identity[0], version=identity[1], pid=os.getpid()))
            self.identity = identity

    def close(self):
        # Closing the descriptor releases the lock even after a client crashes.
        if self.file.closed:
            return
        try:
            (self.directory / "client.json").unlink(missing_ok=True)
        except OSError:
            pass  # Native status verifies the OS lock, never this file alone.
        self.file.close()


def list_sessions(registry: Path | None = None) -> list[dict]:
    registry = registry or registry_path()
    result = []
    if not registry.is_dir():
        return result
    for directory in sorted(registry.iterdir()):
        if not SESSION_ID.fullmatch(directory.name) or not directory.is_dir():
            continue
        try:
            data = read_session(directory)
            context = Bridge(directory, data["token"]).read_context()
            result.append({"session_id": data["session_id"], "pid": data["pid"],
                           "write_enabled": data["write_enabled"],
                           "filename": context.get("filename", ""),
                           "modified": context.get("modified", False),
                           "selected_ids": context.get("selected_ids", [])})
        except (OSError, ValueError, TypeError, AttributeError, BridgeError):
            continue
    return result


class SessionManager:
    """Attach mode: discover windows without spawning or taking over another client."""
    def __init__(self, allow_write: bool, registry: Path | None = None, executable: Path | None = None):
        self.allow_write = allow_write
        self.registry = registry or registry_path()
        self.bridge: Bridge | None = None
        self.lease: ClientLease | None = None
        self.session_id: str | None = None
        self._pending = 0
        self._requires_connect = False
        self.executable = executable.resolve(strict=True) if executable else None
        self.client_info = ("MCP client", "")
        if self.executable and not self.executable.is_file():
            raise ValueError('The Aegisub executable must be a file')

    async def open_window(self, project_file: str | None = None) -> dict:
        if self.executable is None:
            raise BridgeError('Configure --attach --aegisub PATH to enable explicit window opening')
        if self._pending:
            raise BridgeError('Wait for the current tool call before opening another window')
        project = None
        if project_file is not None:
            project = Path(project_file)
            if not project.is_absolute():
                raise BridgeError('project_file must be an absolute local subtitle path')
            project = project.resolve(strict=True)
            if not project.is_file():
                raise BridgeError('project_file must name an existing subtitle file')
        self._pending += 1
        try:
            with tempfile.TemporaryDirectory(prefix='aegisub-window-') as ticket:
                result_path = Path(ticket) / 'result.json'
                if os.name == 'nt':
                    # Windows venv redirectors can create their own kill-on-exit
                    # jobs. Use the real interpreter for this transient worker,
                    # importing our installed package from its exact location.
                    bootstrap = ('import sys; sys.path.insert(0, sys.argv.pop(1)); '
                                 'from aegisub_mcp.window_launcher import main; main()')
                    command = [getattr(sys, '_base_executable', sys.executable), '-c', bootstrap,
                               str(Path(__file__).resolve().parent.parent)]
                else:
                    command = [sys.executable, '-m', 'aegisub_mcp.window_launcher']
                command += ['--aegisub', str(self.executable), '--result', str(result_path)]
                if project:
                    command += ['--project', str(project)]
                if self.allow_write:
                    command += ['--allow-write']
                # A transient intermediary exits before this call returns. Hosts
                # that kill the MCP server's children cannot then kill the GUI.
                process = launch_window_helper(command, result_path)
                deadline = asyncio.get_running_loop().time() + 25
                while process.poll() is None:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise BridgeError('Window launch status is uncertain; inspect Aegisub and list_sessions before retrying')
                    await asyncio.sleep(0.05)
                if not result_path.is_file() or result_path.stat().st_size > 16384:
                    raise BridgeError('Window launcher returned no valid result; inspect Aegisub before retrying')
                result = json.loads(result_path.read_text(encoding='utf-8'))
                if result.get('error') or not result.get('ready'):
                    raise BridgeError(result.get('error', 'Window is not ready; inspect Aegisub before retrying'))
        finally:
            self._pending -= 1
        context = self.connect(result['session_id'])
        return {**context, 'launched': True, 'pid': result['pid']}

    def list_sessions(self) -> list[dict]:
        return list_sessions(self.registry)

    def connect(self, session_id: str) -> dict:
        if self._pending:
            raise BridgeError("Wait for the current tool call to finish before switching or reconnecting windows")
        if not SESSION_ID.fullmatch(session_id):
            raise BridgeError("Invalid session ID; use list_sessions")
        directory = self.registry / session_id
        data = read_session(directory)
        if self.allow_write and not data["write_enabled"]:
            raise BridgeError("Enable 'Agent connection: allow editing' in the selected window first")
        if session_id == self.session_id and self.bridge and not self.bridge._failed:
            return self.bridge.read_context()
        if session_id == self.session_id:
            # Explicit reconnect after a transport failure releases our own lease.
            self.close()
        lease = ClientLease(directory)
        try:
            lease.set_client_info(*self.client_info)
            bridge = Bridge(directory, data["token"], allow_write=self.allow_write)
            # Validate before releasing a previously selected window.
            context = bridge.read_context()
        except BaseException:
            lease.close()
            raise
        self.close()
        self.lease, self.bridge, self.session_id = lease, bridge, session_id
        self._requires_connect = False
        bridge.session_guard = lambda: self._check_session(bridge)
        return context

    def set_client_info(self, name: str, version: str):
        self.client_info = (name, version)
        if self.lease:
            self.lease.set_client_info(name, version)

    def disconnect(self) -> dict:
        if self._pending:
            raise BridgeError("Wait for the current tool call to finish before disconnecting")
        previous = self.session_id
        self._requires_connect = True
        self.close()
        return {"disconnected": True, "previous_session_id": previous}

    def _check_session(self, bridge: Bridge):
        try:
            data = read_session(bridge.directory)
            if data["token"] != bridge.token:
                raise BridgeError("Session credentials changed")
        except (OSError, ValueError, BridgeError) as exc:
            # Never let resource polling or a later tool silently reclaim a
            # window after the user hands it to another agent. In-flight calls
            # keep their lease until their own cleanup completes.
            self._requires_connect = True
            if not self._pending:
                self.close()
            raise BridgeError("Session credentials changed or agent disconnected. "
                "Use list_sessions and explicitly connect_session to reconnect. "
                "An in-flight request may have completed; inspect the document before retrying.") from exc

    def _connected(self) -> Bridge:
        if not self.bridge:
            if self._requires_connect:
                raise BridgeError("Agent disconnected; use list_sessions and explicitly connect_session to reconnect")
            sessions = self.list_sessions()
            if len(sessions) != 1:
                raise BridgeError("Use list_sessions and connect_session to choose an enabled Aegisub window")
            self.connect(sessions[0]["session_id"])
        # Detect closed windows and credential rotation before publishing anything.
        self._check_session(self.bridge)
        return self.bridge

    async def call(self, method: str, args: dict | None = None) -> dict:
        bridge = self._connected()
        self._pending += 1
        try:
            return await bridge.call(method, args)
        finally:
            self._pending -= 1
            if self._requires_connect and not self._pending:
                self.close()

    def read_context(self) -> dict:
        return self._connected().read_context()

    def close(self):
        if self.lease:
            self.lease.close()
        self.lease = self.bridge = self.session_id = None
