# Aegisub MCP

This fork lets an MCP client work with a live Aegisub document. The Python server
launches a dedicated Aegisub window; open your subtitles in that window or supply
`--project`. It cannot attach to an ordinary upstream binary or a window that was
started without the bridge environment.
Only the initial window in the launched process is connected; additional windows
are independent. On first launch, finish Aegisub's language/update prompts before
calling tools.

## Setup

1. Build **this fork**, using the platform instructions in the root README.
   On Windows, install Visual Studio's **Desktop development with C++** workload,
   including a Windows SDK. Use an x64 Developer Command Prompt:

   ```text
   git clone --branch mcp-support https://github.com/F767-R/aegisub-mcp.git
   cd aegisub-mcp
   meson setup build -Ddefault_library=static -Dbuildtype=release
   meson compile -C build
   ```

2. Install the MCP server in a virtual environment (Python 3.11 or newer):

   ```text
   python -m venv .venv
   .venv\Scripts\python.exe -m pip install ./mcp
   ```

   On Linux/macOS, use `.venv/bin/python` instead. The server uses the official
   MCP Python SDK, constrained to its compatible 1.x API.

3. Add this stdio server to your MCP client's configuration. Substitute absolute
   paths. This is the common `mcpServers` JSON format; clients with a different
   format need the same command and arguments:

   ```json
   {
     "mcpServers": {
       "aegisub": {
         "command": "C:/path/to/aegisub-mcp/.venv/Scripts/python.exe",
         "args": [
           "-m", "aegisub_mcp.server",
           "--aegisub", "C:/path/to/aegisub-mcp/build/aegisub.exe",
           "--allow-write"
         ]
       }
     }
   }
   ```

   Omit `--allow-write` for read-only access. Append
   `"--project", "C:/path/to/subtitles.ass"` to open a specific file at startup.
   Launching the server directly from a terminal waits for MCP messages on stdin;
   configure it in a client for normal use. On macOS, point `--aegisub` at the
   executable inside the built app bundle, not at the `.app` directory.

4. Connect the client. The launched window briefly displays an MCP status message.
   Call `get_document`, then `list_lines`, and review the returned subtitle IDs
   before making edits. Save through Aegisub's normal Save command.

## Tools

| Tool | Purpose |
| --- | --- |
| `get_document` | Revision, filename, unsaved state, line count, active line, selection, Undo/Redo availability |
| `list_lines` | Paginated subtitle text, timestamps, metadata, IDs and selection |
| `list_styles` | Style names and full ASS definitions |
| `update_lines` | Validate and apply up to 500 edits in one Undo step |
| `shift_times` | Shift start/end times for up to 500 IDs in one Undo step |
| `select_lines` | Select IDs and choose the active subtitle |
| `undo` / `redo` | Use Aegisub's existing history, including human edits |

Only the three read tools are advertised in read-only sessions. Native code also
enforces the read-only restriction. `update_lines` supports `text`, `start_ms`,
`end_ms`, `style`, `actor`, `effect`, `layer`, and `comment`; omit unchanged fields.

Every mutation requires `expected_revision` from a recent read. A conflicting
manual edit, file load, Undo/Redo, or save invalidates the revision. Re-read and
reassess after a conflict. IDs identify subtitle objects; rows are display-only,
one-based positions. Pagination offsets are zero-based. Check that every page
has the same revision; restart pagination if the document changes.

Example after `list_lines` returned revision `7` and ID `42`:

```json
{
  "expected_revision": 7,
  "updates": [{"id": 42, "text": "Hello!\\NSecond line", "start_ms": 1250, "end_ms": 3500}]
}
```

Times are milliseconds in multiples of 10 (ASS precision), from 0 through
35,999,990. End must be at least start. Strings are limited to 65,536 UTF-8 bytes.
Use ASS `\N` instead of literal newlines; commas are forbidden in ASS metadata.
Styles must already exist. Duplicate IDs and invalid batches are rejected before
the document is changed.

## Lifecycle and local access

The stdio process uses a unique temporary directory and a random session token to
communicate with the native bridge. It starts no TCP/HTTP listener. Requests are
written atomically; the GUI checks for them every 50 ms and handles them on its
UI thread. The bridge is inactive during modal dialogs. Access is intended for
the same OS account; it is not a sandbox against programs running as that user.
Use a private per-user temporary directory (the OS default), not a shared temp
directory. The launcher supplies `AEGISUB_MCP_DIR`, `AEGISUB_MCP_TOKEN`, and
`AEGISUB_MCP_WRITE` only to its child process.

Tool requests expire after 15 seconds. A timeout or cancellation disconnects the
session: the server never automatically repeats a mutation. An edit might have
completed just before a response was lost. Inspect the window before retrying.
Closing a modal dialog may resolve the cause, but restart the MCP server to
establish a new session. Each restart opens a new window; save and close the
previous one yourself.

When the client disconnects normally, the mailbox is removed and native polling
stops. **Aegisub stays open**, preserving unsaved work. An abrupt server kill may
leave an orphaned temporary directory; close its Aegisub window before cleaning
that directory. The server never saves subtitles or forcibly closes the GUI.

Treat subtitle text and metadata as untrusted content in the client; they are
data, not instructions. Client tool approval policy still applies.

## Tests

```text
python -m pip install ./mcp[test]
python -m pytest mcp/tests -v
```

Without a built executable, native integration tests are explicitly skipped.
To run them against a real GUI on Windows PowerShell:

```powershell
$env:AEGISUB_MCP_TEST_EXE = (Resolve-Path build/aegisub.exe).Path
python -m pytest mcp/tests/test_native.py -v
```

On headless Linux:

```sh
AEGISUB_MCP_TEST_EXE="$PWD/build/aegisub" xvfb-run -a python -m pytest mcp/tests/test_native.py -v
```

These tests launch disposable fixture windows, verify edits, batch rejection,
revision conflicts, selection, timing, Undo/Redo, token checks and read-only
enforcement, then terminate only those fixture processes. CI builds the native
Linux application and runs these tests. See the Actions results for build status;
the existence of this source is not itself evidence of a successful native build.
