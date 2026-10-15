# SPDX-License-Identifier: BSD-3-Clause
"""One serialized, authenticated mailbox per launched Aegisub instance."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import uuid

MAX_REQUEST = 4 * 1024 * 1024
MAX_RESPONSE = 64 * 1024 * 1024
READ_METHODS = frozenset({"get_document", "list_lines", "list_styles"})
WRITE_METHODS = frozenset({"update_lines", "shift_times", "select_lines", "undo", "redo"})


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
                 timeout: float = 15.0, process: subprocess.Popen | None = None):
        self.directory = directory
        self.token = token
        self.allow_write = allow_write
        self.timeout = timeout
        self.process = process
        self._lock = asyncio.Lock()
        self._failed = False

    async def call(self, method: str, args: dict | None = None) -> dict:
        if method not in READ_METHODS | WRITE_METHODS:
            raise BridgeError("Unknown bridge method")
        if method in WRITE_METHODS and not self.allow_write:
            raise BridgeError("Read-only session; restart with --allow-write to edit")
        async with self._lock:
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
        self._temporary = tempfile.TemporaryDirectory(prefix="aegisub-mcp-")
        self.directory = Path(self._temporary.name)
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
            self._temporary.cleanup()
            raise
        self.bridge = Bridge(self.directory, token, allow_write=allow_write, process=self.process)

    def close(self) -> None:
        # Removing the mailbox disables the native bridge on its next timer tick.
        # Do not terminate the GUI: the user may have unsaved changes.
        self._temporary.cleanup()
