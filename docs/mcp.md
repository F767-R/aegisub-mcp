# Aegisub MCP

This fork lets an MCP client work with a live Aegisub document. Version **0.3.2**
supports both launching a dedicated window and connecting to an already-open
window that you enable in the fork's View menu. It cannot attach to an unmodified
upstream binary. On first launch, finish language/update prompts before calling tools.

## Connect to an open window (0.3)

1. In the matching 0.3 native fork, open your subtitle project normally.
2. Choose **View > Agent connection: allow editing**, or **read only**.
3. Configure the Python server with `--attach --aegisub PATH`:

   ```json
   {"mcpServers":{"aegisub":{"command":"C:/path/to/.venv/Scripts/python.exe",
     "args":["-m","aegisub_mcp.server","--attach","--aegisub","C:/path/to/aegisub.exe","--allow-write"]}}}
   ```

   Portable Windows setup also accepts `setup-mcp.cmd --attach`, or
   `setup-claude-desktop.cmd --attach`. Add `--read-only` for a read-only client.
4. Ask the agent to use `list_sessions`, then `connect_session` with the intended
   window's ID. A single enabled window is selected automatically on the first read.

### Open a new window on request (server 0.3.1)

With `--attach --aegisub PATH`, the agent also receives `open_window`.
Call `open_window()` for a blank document, or
`open_window(project_file="C:/path/to/subtitles.ass")` to open an existing local
subtitle file. Each explicit call creates one new window and connects to it;
existing documents and unsaved work stay open. Native 0.3 builds are compatible.
Tool discovery, enabling MCP, and client reconnects never invoke this tool.

The window is created through a short-lived independent launcher. On Windows,
the local process provider keeps it outside the MCP client's kill-on-close job;
the base Python interpreter avoids extra venv redirector jobs. It therefore
survives clients that terminate their process jobs on disconnect. Finish any first-run
or linked-media prompts in the GUI. If a call fails or is cancelled, inspect
`list_sessions` before retrying; a window might already have started.

Windows setup now defaults to this mode. `--attach` without `--aegisub` remains
supported for connecting only, and exposes no window-opening tool. Use
`setup-mcp.cmd --launch-on-connect` only if you deliberately want the legacy
behavior of opening a new GUI for every MCP connection.

The client holds an exclusive OS lease, so two clients cannot overwrite each
other's mailbox. Disconnecting or restarting the client releases that lease and
leaves the GUI and unsaved work open. Native access remains enabled until you
choose **View > Disable all agent access** or close the window. Changing access mode
rotates the session ID and credentials, revoking the previous connection.
Additional windows can each be enabled independently through their View menu.
The server never enables access in a window by itself.

### Switch between Codex and Claude (0.3.2)

Choose **View > Disconnect current agent (allow another)** in the Aegisub window.
This revokes the current client's session and publishes a fresh session for the
same window. It preserves read-only/editing permissions, unsaved subtitles,
selection, media, and Undo history. It never starts or closes a window.

Ask the other agent to call `list_sessions`, then `connect_session` with the
window's new ID. Only the client that explicitly connects takes control.
The disconnected client cannot reclaim the window through background resource
polling or ordinary tool calls; it must explicitly connect again. To switch back,
repeat the menu command and ask the first agent to connect.

An agent can also call `disconnect_session` to release its own connection without
rotating the native session. Its background polling stays disconnected. The
separate **Disable all agent access** option makes the window unavailable to
every client until you enable read-only or editing access again. The handoff
menu requires the 0.3.2 native build; the disconnect tool works with native 0.3.
If a request was in flight when disconnected, inspect the document before
retrying it: the GUI may already have applied it. Requests are never retried.

`python -m aegisub_mcp.server --list-sessions` prints discoverable windows without
exposing tokens. Discovery uses per-user local application data, not a network
listener. Only windows running a compatible native protocol are listed.

## Live context and previews (0.3)

`get_context` returns document state plus `context_version`, session ID, selection,
loaded media, video dimensions, frame/time, adjacent keyframes, and playback state.
Document `revision` still changes only for document edits/open/save; selection and
seeking advance the separate context version. Continue using `revision` for edits.

Clients supporting MCP resource subscriptions can read and subscribe to
**`aegisub://context`**. The server emits resource-updated notifications after
native context changes, coalesced to at most four notifications per second.
Read the resource after notification. Other clients can call `wait_for_context`
with the last context version and a timeout of up to ten seconds. Watching reads
an atomic event snapshot and does not occupy the edit mailbox or retry edits.
Snapshots pause during modal dialogs; they resume when the dialog closes.

Open video or dummy video in the GUI, then use `render_preview(time_ms=1500)`.
It returns an MCP PNG image of the actual frame with current unsaved subtitles,
using Aegisub's renderer. It does not move the playhead or save the document.
Use `raw=true` for a comparison without subtitles. Output defaults to fitting
1280x720, preserves frame aspect ratio, and is capped at 1920x1080. Requested times
snap to the corresponding video frame. The preview uses encoded frame pixels;
GUI zoom, pan, and display aspect-ratio overrides are not included.

`seek_video`, `play_audio` (at most 30 seconds), and `stop_playback` control the
editor's loaded media. These controls require editing access and a current
document revision, but do not create subtitle Undo steps. Media opening stays in
the GUI.

## Setup

This server requires a **0.3 or newer native build** for session
discovery, live context, and previews. Older Windows releases do not contain these
features; update both the application and Python server together.
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
Claude Desktop and ask: "List the enabled Aegisub windows and connect to mine."
Enable your window in its View menu, or explicitly ask Claude to open a new one.

Setup merges only `mcpServers.aegisub` into
`%APPDATA%\Claude\claude_desktop_config.json`, preserving other servers and
preferences. For the Microsoft Store installation, setup detects its data folder
and uses `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json`
instead. If checking manually, Settings > Developer > Edit Config opens the file
used by your installation. An existing Aegisub entry is replaced; changed settings receive a
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
selection, and Undo/Redo tools as other MCP hosts. Launch-mode connections each
start an Aegisub process. Attach mode can connect to a previously enabled window
after its previous MCP client disconnects; only one client holds its lease at once.
Press Ctrl+S in Aegisub to save edits.
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
| `list_sessions` / `connect_session` | Discover and select an enabled open window (attach mode) |
| `open_window` | Open one new window on request, optionally with a local subtitle file (attach mode plus executable path) |
| `get_context` / `wait_for_context` | Read or wait for selection, playhead, media and document context |
| `render_preview` | Render the current unsaved subtitles over a video frame as a PNG image |
| `seek_video` / `play_audio` / `stop_playback` | Control loaded media without editing subtitle content |
| `create_lines` | Insert/append up to 500 new lines; return IDs in input order and select them |
| `delete_lines` | Delete up to 500 IDs; preserve one blank line if the whole document is cleared |
| `create_style` | Create a uniquely named style, optionally cloning an existing style |
| `update_style` | Change supplied properties of an existing style, affecting all lines using it |
| `update_lines` | Validate and apply up to 500 edits in one Undo step |
| `shift_times` | Shift start/end times for up to 500 IDs in one Undo step |
| `select_lines` | Select IDs and choose the active subtitle |
| `undo` / `redo` | Use Aegisub's existing history, including human edits |

Read-only sessions advertise document, line, style, context, wait and preview tools
(plus discovery/connection in attach mode). Native code also
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

The stdio process uses a unique private session directory and a random session token to
communicate with the native bridge. It starts no TCP/HTTP listener. Requests are
written atomically; the GUI checks for them every 50 ms and handles them on its
UI thread. The bridge is inactive during modal dialogs. Access is intended for
the same OS account; it is not a sandbox against programs running as that user.
Session descriptors contain credentials and are private to the current OS user.
They live under `%LOCALAPPDATA%/Aegisub/mcp-sessions` on Windows,
`~/Library/Application Support/Aegisub/mcp-sessions` on macOS, or
`~/.local/share/aegisub/mcp-sessions` on Linux (directories mode 0700).
The launcher supplies `AEGISUB_MCP_DIR`, `AEGISUB_MCP_TOKEN`, and
`AEGISUB_MCP_WRITE` only to its child process.

Tool requests expire after 15 seconds. A timeout or cancellation disconnects the
session: the server never automatically repeats a mutation. An edit might have
completed just before a response was lost. Inspect the window before retrying.
Closing a modal dialog may resolve the cause. Use an explicit `connect_session`
in attach mode, or restart the client in attach mode, to establish a new transport
after inspecting the window. Launch mode starts a new window on each restart.

When the client disconnects, its exclusive lease is released. **Aegisub stays
open**, preserving unsaved work and its opt-in connection for later attachment.
The native window removes discovery credentials when it closes or disables access.
An abrupt application kill may leave a stale directory; discovery ignores sessions
whose recorded process has exited. The server never saves or forcibly closes the GUI.

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
