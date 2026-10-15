# Build and monitor Aegisub MCP for Windows

## GitHub Actions (recommended)

Pushing a code change to `mcp-support` automatically starts **MCP Windows release**.

1. Open [MCP Windows release](https://github.com/F767-R/aegisub-mcp/actions/workflows/mcp-windows.yml).
2. Select the run matching your commit. Open the **build** job to watch its log.
   The stages are dependency setup, native compilation and unit tests, cache
   saving, installer/portable packaging, and real Windows MCP integration tests.
3. Wait for both **build** and **publish** to turn green. A clean build can take
   around an hour; a usable cache can shorten it. Build-step counts are not a
   reliable time estimate. A failure prevents publication.
4. Download the matching prerelease from
   [Releases](https://github.com/F767-R/aegisub-mcp/releases). The tag is
   `mcp-windows-` followed by the first 12 characters of the source commit.
   Alternatively, download **aegisub-mcp-windows-x64** from the run's Artifacts.
   Downloading Actions artifacts requires GitHub sign-in.

To start a build manually: on the workflow page click **Run workflow**, select
`mcp-support`, and click **Run workflow** again. To retry a failed run after a
transient download problem, use **Re-run failed jobs**. For code failures, fix and
push the change so the next run uses the corrected commit.

Optional GitHub CLI monitoring (after `gh auth login`):

```powershell
gh run list --repo F767-R/aegisub-mcp --workflow mcp-windows.yml --branch mcp-support --limit 5
# Replace RUN_ID with the ID from the list:
gh run watch RUN_ID --repo F767-R/aegisub-mcp --exit-status
gh run view RUN_ID --repo F767-R/aegisub-mcp --log-failed
gh run download RUN_ID --repo F767-R/aegisub-mcp --name aegisub-mcp-windows-x64
```

The artifacts are `Aegisub-MCP-Windows-x64-setup.exe`,
`Aegisub-MCP-Windows-x64-portable.zip`, `Aegisub-MCP-source.zip`, and
`SHA256SUMS.txt`. These are unsigned community builds. The portable ZIP includes
the matching Python server. Extract it and run `setup-mcp.cmd` with Python 3.11+
installed, then copy the generated `mcp-config.json` server entry into your MCP
client. For ordinary editing, launch `aegisub.exe` directly.

## Build locally

Install Git, Python 3.11+, and Visual Studio's **Desktop development with C++**
workload including an x64 Windows SDK. Packaging additionally needs Inno Setup,
7-Zip, and gettext (`msgfmt`) on PATH. The workflow contains the exact dependency
installation recipe. Use **Developer PowerShell for Visual Studio**, configured
for x64, and a fresh build directory:

```powershell
git clone --branch mcp-support https://github.com/F767-R/aegisub-mcp.git
cd aegisub-mcp
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install meson ninja setuptools './mcp[test]'

meson setup build -Dbuildtype=release -Ddefault_library=static --force-fallback-for=zlib,harfbuzz,freetype2,fribidi -Dfallback_nasm=true -Dfreetype2:harfbuzz=disabled -Dharfbuzz:freetype=disabled -Dharfbuzz:cairo=disabled -Dharfbuzz:glib=disabled -Dharfbuzz:gobject=disabled -Dharfbuzz:tests=disabled -Dharfbuzz:docs=disabled -Dfribidi:tests=false -Dfribidi:docs=false -Dlibass:fontconfig=disabled
meson compile -C build
meson test -C build --verbose --suite Aegisub
```

Check that each command succeeds before continuing. The executable is
`build/aegisub.exe`. Package it with its resources and runtime installer:

```powershell
meson compile win-installer -C build
meson compile win-portable -C build
```

Run the native MCP tests against the disposable portable copy. This sets local
test configuration in the build directory, bypassing first-start dialogs:

```powershell
$config = Join-Path $PWD 'build/aegisub-portable/config.json'
$original = [IO.File]::ReadAllText($config)
try {
    [IO.File]::WriteAllText($config, '{"App":{"Language":"en_US","First Start":false,"Auto":{"Check For Updates":false,"Save":false}}}')
    $env:AEGISUB_MCP_TEST_EXE = Join-Path $PWD 'build/aegisub-portable/aegisub.exe'
    python -m pytest mcp/tests -v
    if ($LASTEXITCODE) { throw 'MCP tests failed' }
} finally {
    [IO.File]::WriteAllText($config, $original)
}
./tools/mcp-package-windows.ps1
```

The final assets are in `build/mcp-release/`. The packaging script expects fresh
staging directories; for a later release use a fresh build directory. It extracts
the pristine portable archive, so test configuration and history are excluded.
Local commands do not publish a GitHub release. The GitHub workflow publishes
only after its native unit tests and packaged MCP tests pass.
