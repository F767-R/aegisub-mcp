# SPDX-License-Identifier: BSD-3-Clause
"""One serialized, authenticated mailbox per launched Aegisub instance."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import uuid
from collections.abc import Callable

MAX_REQUEST = 4 * 1024 * 1024
MAX_RESPONSE = 64 * 1024 * 1024
READ_METHODS = frozenset({"get_document", "list_lines", "list_styles", "get_context", "render_preview", "get_agent_status", "get_operation"})
WRITE_METHODS = frozenset({"update_lines", "create_lines", "delete_lines", "create_style", "update_style",
                           "shift_times", "select_lines", "undo", "redo", "seek_video", "play_audio", "stop_playback",
                           "propose_batch", "apply_batch", "cancel_batch"})


class BridgeError(RuntimeError):
    pass


def atomic_json(path: Path, data: dict) -> None:
    payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(payload) > MAX_REQUEST:
        raise BridgeError("Request exceeds 4 MiB; use a smaller batch")
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
    os.replace(temporary, path)


class Bridge:
    def __init__(self, directory: Path, token: str, *, allow_write: bool = False,
                 timeout: float = 15.0, process: subprocess.Popen | None = None,
                 session_guard: Callable[[], None] | None = None):
        self.directory = directory
        self.token = token
        self.allow_write = allow_write
        self.timeout = timeout
        self.process = process
        self._lock = asyncio.Lock()
        self._failed = False
        self.session_guard = session_guard
        self.client_lease = None

    def set_client_info(self, name: str, version: str):
        if self.client_lease:
            self.client_lease.set_client_info(name, version)

    def read_context(self) -> dict:
        """Native event snapshot: no mailbox request, including while a modal dialog is open."""
        if self.session_guard:
            self.session_guard()
        if self.process is not None and self.process.poll() is not None:
            raise BridgeError("Aegisub has exited")
        path = self.directory / "context.json"
        if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
            raise BridgeError("Invalid live context snapshot")
        result = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(result, dict) or type(result.get("context_version")) is not int
                or type(result.get("revision")) is not int):
            raise BridgeError("Invalid live context snapshot")
        return result

    async def call(self, method: str, args: dict | None = None) -> dict:
        if method not in READ_METHODS | WRITE_METHODS:
            raise BridgeError("Unknown bridge method")
        if method in WRITE_METHODS and not self.allow_write:
            raise BridgeError("Read-only session; restart with --allow-write to edit")
        async with self._lock:
            if self.session_guard:
                self.session_guard()
            if self._failed:
                raise BridgeError("Session disconnected after transport failure; restart the MCP server")
            if self.process is not None and self.process.poll() is not None:
                raise BridgeError("Aegisub has exited")
            request_id = uuid.uuid4().hex
            request_path = self.directory / "request.json"
            response_path = self.directory / f"{request_id}.json"
            deadline = time.monotonic() + self.timeout
            published = False
            try:
                atomic_json(request_path, {
                    "id": request_id, "token": self.token, "method": method,
                    "args": args or {},
                    "expires_ms": int((time.time() + self.timeout) * 1000),
                })
                published = True
                while time.monotonic() < deadline:
                    if self.session_guard:
                        self.session_guard()
                    if response_path.exists():
                        if response_path.stat().st_size > MAX_RESPONSE:
                            raise BridgeError("Response exceeds 64 MiB; request fewer lines")
                        response = json.loads(response_path.read_text(encoding="utf-8"))
                        if not isinstance(response, dict) or response.get("id") != request_id:
                            raise BridgeError("Invalid bridge response ID")
                        if "error" in response:
                            # Application errors preserve the usable transport.
                            published = False
                            raise BridgeError(str(response["error"]))
                        if not isinstance(response.get("result"), dict):
                            raise BridgeError("Invalid bridge result")
                        published = False
                        return response["result"]
                    if self.process is not None and self.process.poll() is not None:
                        raise BridgeError("Aegisub exited while processing the request")
                    await asyncio.sleep(0.025)
                raise BridgeError(
                    "Aegisub did not respond. Close any modal dialog and check that you built "
                    "the MCP fork. A timed-out edit may have applied; inspect the window before "
                    "retrying. Restart the MCP server to reconnect."
                )
            finally:
                if published:
                    # Cancel/timeout cannot safely be retried: the GUI may have consumed it.
                    self._failed = True
                request_path.unlink(missing_ok=True)
                response_path.unlink(missing_ok=True)


class LaunchedSession:
    """Keep the GUI alive on disconnect so unsaved work is never discarded."""
    def __init__(self, executable: Path, project: Path | None, allow_write: bool):
        executable = executable.resolve(strict=True)
        if not executable.is_file():
            raise ValueError("--aegisub must name the fork's executable")
        if project is not None:
            project = project.resolve(strict=True)
            if not project.is_file():
                raise ValueError("--project must name a subtitle file")
        from .sessions import ClientLease, registry_path
        registry = registry_path()
        registry.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The native window owns this directory until it closes or disables access.
        # Keeping it on disconnect allows another client to reconnect to unsaved work.
        self.directory = registry / secrets.token_hex(16)
        self.directory.mkdir(mode=0o700)
        self._lease = ClientLease(self.directory)
        token = secrets.token_hex(32)
        env = os.environ.copy()
        env.update(AEGISUB_MCP_DIR=str(self.directory), AEGISUB_MCP_TOKEN=token,
                   AEGISUB_MCP_WRITE="1" if allow_write else "0")
        command = [str(executable)]
        if project:
            command.append(str(project))
        try:
            # stdout belongs exclusively to MCP. Never inherit MCP stdin into the GUI.
            self.process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except BaseException:
            self._lease.close()
            (self.directory / "client.lock").unlink(missing_ok=True)
            self.directory.rmdir()
            raise
        self.bridge = Bridge(self.directory, token, allow_write=allow_write, process=self.process)
        self.bridge.client_lease = self._lease

    def close(self) -> None:
        # Disconnect only this client. Native access remains opt-in and can be
        # disabled in View; the GUI and unsaved work survive client restarts.
        self._lease.close()
