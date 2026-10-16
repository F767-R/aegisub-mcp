# Aegisub MCP

This fork lets an MCP client work with a live Aegisub document. The Python server
launches a dedicated Aegisub window; open your subtitles in that window or supply
`--project`. It cannot attach to an ordinary upstream binary or a window that was
started without the bridge environment.
Only the initial window in the launched process is connected; additional windows
are independent. On first launch, finish Aegisub's language/update prompts before
calling tools.

## Setup

Line creation/deletion and style creation/editing require the **0.2.0 server and
matching native build**. Older Windows releases do not contain these tools.
See [Windows build and monitoring instructions](build-mcp-windows.md) to build
the current branch and follow its progress yourself.

### Windows downloads

Download the **Windows x64 portable ZIP** from this fork's
[Releases page](https://github.com/F767-R/aegisub-mcp/releases). Extract it to a
writable folder and double-click `aegisub.exe` for ordinary editing. For MCP,
install Python 3.11 or newer with `python` on PATH, then run `setup-mcp.cmd` in
the extracted folder. Setup downloads the Python dependencies into a local
`.venv` and generates `mcp-config.json`; copy its server entry into your MCP
client. Keep the folder in place because the configuration uses absolute paths.
The generated entry enables editing; remove `--allow-write` for read-only use.

The installer EXE is also available, but it installs only the native application.
To use it with MCP, install `mcp/` from the matching source ZIP as below and point
`--aegisub` to the installed executable. Releases are unsigned community builds.
`BUILD-INFO.txt` in the portable package records the source commit, and release
assets include SHA-256 checksums. Python is required for MCP, not ordinary editing.

### Claude Desktop (Windows)

Install and run Claude Desktop, then fully quit it (including the system tray).
In the portable folder, double-click **`setup-claude-desktop.cmd`**. It installs
the server if needed, or reuses the environment from `setup-mcp.cmd`. Reopen
Claude Desktop and ask: "Use Aegisub's get_document tool, then list its styles."
Open your subtitle file in the Aegisub window that Claude launches.

Setup merges only `mcpServers.aegisub` into
`%APPDATA%\Claude\claude_desktop_config.json`, preserving other servers and
preferences. An existing Aegisub entry is replaced; changed settings receive a
timestamped `.bak` copy first. Invalid settings are rejected without overwriting
them. Run setup as your regular Windows user, without administrator privileges.
For read-only access, run `setup-claude-desktop.cmd --read-only` from a terminal.

Claude Desktop may start configured servers when it opens. To disconnect Aegisub
and prevent it reopening, fully quit Claude, run
`setup-mcp.cmd --remove-claude-desktop`, then reopen Claude. This removes only its
Aegisub entry. It does not close a running Aegisub window or save your subtitles.
Save your work yourself before closing the window.

### Claude Code (Windows)

Install Claude Code separately and run `setup-mcp.cmd` once. From your subtitle
project's terminal, run the portable folder's **`start-claude-code.cmd`**, or:

```powershell
claude --mcp-config 'D:\path\to\aegisub-portable\mcp-config.json'
```

The launcher preserves the current project directory and supplies the generated
configuration for that session. It does not register Aegisub globally or change
Claude Code settings. Use `/mcp` in Claude Code to check the connection and allow
requested tools. Start ordinary sessions with `claude` when you do not want
Aegisub. See the official [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)
and [Claude Desktop MCP guide](https://modelcontextprotocol.io/docs/develop/connect-local-servers).

Both clients use the same reading, subtitle/timing/style editing, effect creation,
selection, and Undo/Redo tools as other MCP hosts. Each connection launches its
own Aegisub process; Claude cannot take over a window already launched by Codex.
Open the saved `.ass` file in Claude's window and press Ctrl+S to save edits.
The Aegisub bridge does not need an Anthropic API key; authenticate with your
Claude client as usual. Local stdio requires Claude Desktop or local Claude Code,
rather than a browser-only chat at claude.ai.

### Build from source and configure manually

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
| `get_document` | Revision, filename, unsaved state, script width/height, line count, active line, selection, Undo/Redo availability |
| `list_lines` | Paginated subtitle text, timestamps, metadata, IDs and selection |
| `list_styles` | Style names, structured properties, and full ASS definitions |
| `create_lines` | Insert/append up to 500 new lines; return IDs in input order and select them |
| `delete_lines` | Delete up to 500 IDs; preserve one blank line if the whole document is cleared |
| `create_style` | Create a uniquely named style, optionally cloning an existing style |
| `update_style` | Change supplied properties of an existing style, affecting all lines using it |
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

## Creating effects

1. Read `get_document`, `list_lines`, and `list_styles`. Use `script_width` and
   `script_height` when calculating positions for ASS tags.
2. Call `create_style` for a new effect style. An optional `base_style` copies
   an existing style; otherwise Aegisub's default style properties are used.
3. Call `create_lines` with explicit `text`, `start_ms`, and `end_ms` for every
   line. `style` defaults to `Default`; `layer`, `actor`, `effect`, and `comment`
   are optional. Omit `after_id` to append, or supply an existing ID to insert
   immediately after it. The input order is preserved.
4. Combine ASS tags such as `\blur`, `\fad`, `\move`, `\t`, and `\k` in text
   with multiple layers. Preview in Aegisub. Keep the returned `created_ids` if
   you want to remove the generated lines using `delete_lines`.

Example `create_style` arguments (substitute a current revision):

```json
{
  "expected_revision": 7,
  "name": "Glow",
  "base_style": "Default",
  "properties": {"primary_color": "&H0000FFFF", "outline_width": 6, "font_size": 60}
}
```

Then call `create_lines` with the revision returned by that call:

```json
{
  "expected_revision": 8,
  "lines": [
    {"text": "{\\blur6\\fad(300,500)}Hello", "start_ms": 1000, "end_ms": 3000, "style": "Glow", "layer": 0},
    {"text": "{\\bord0\\fad(300,500)}Hello", "start_ms": 1000, "end_ms": 3000, "style": "Glow", "layer": 1}
  ]
}
```

Style properties include `font`, `font_size`, the four `*_color` fields,
`bold`, `italic`, `underline`, `strikeout`, `scale_x`, `scale_y`, `spacing`,
`angle`, `border_style`, `outline_width`, `shadow_distance`, `alignment`,
`margin_left`, `margin_right`, `margin_vertical`, and `encoding`.
Colors use **ASS `&HAABBGGRR`**, with `AA=00` opaque and `AA=FF` transparent.
For example, opaque red is `&H000000FF` and opaque yellow is `&H0000FFFF`.
Border styles are 1 (outline), 3 (opaque box), or 4 (libass shadow box).
The tool schema specifies numeric bounds. Style/font names must be nonempty,
trimmed, at most 256 UTF-8 bytes, and contain no commas or line breaks. New
style names must be unique ignoring case. `update_style` uses the exact existing
name and requires at least one property; it does not rename styles. Use a newly
created style to avoid changing other subtitles that share an existing style.

Each create, update, or delete call is one Undo step. A multi-call effect is
therefore several Undo steps. Invalid line batches apply nothing. Deleting all
lines leaves one empty line, whose ID is returned as `replacement_id`; otherwise
that field is null. Re-read after Undo/Redo or manual edits. The server does not
evaluate scripts: effect tags are rendered by Aegisub. Lua execution, style
renaming/deletion, media opening, and remote saving are not exposed.

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
Linux application and runs these tests. The `MCP Windows release` workflow builds
the Windows x64 application, runs native unit tests and all MCP tests against the
packaged executable, and publishes a prerelease only after those checks pass.
It can also be run manually from Actions. Windows packaging follows the upstream
MSVC release configuration, including normal audio/video support, translations,
and Automation scripts. See the workflow for the complete build arguments.
