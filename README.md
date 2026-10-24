# Aegisub

## MCP fork

This fork adds opt-in, local Model Context Protocol support for the live Aegisub
window: read subtitles and styles, edit text and metadata, shift timing, select
lines, create/delete subtitle lines, create/edit styles, and Undo/Redo. Edits are
revision-checked and participate in normal Undo. The assistant can build layered
ASS effects using new lines, styles, and override tags.

Version 0.3 adds opt-in connections to already-open fork windows, live editing
context and MCP resource subscriptions, rendered PNG previews of unsaved subtitles,
video seeking, and bounded audio audition. Enable a window in **View > Agent
connection**, then configure the server with `--attach`. Existing launch-mode
configuration remains supported. A matching 0.3 native build is required.

Server 0.3.1 adds an explicit `open_window` tool. Setup defaults to
`--attach --aegisub PATH`: connecting/discovering tools never opens a GUI;
the agent can open one window when requested. The new window survives client
disconnects, including Windows clients that terminate MCP process jobs. Windows
uses the local process provider to launch the requested GUI independently.

Version 0.4.0 adds **View > Agent connection status...** with the verified native
lease owner, client identity, last command/error, pending proposals and access
controls. `propose_batch` stages subtitle and style changes for a before/after
diff and optional `render_preview(operation_id=...)`; `apply_batch` commits them
as one Undo step. Query `get_operation` after a lost response to avoid repeating
an edit. Receipts survive client handoff for the native window's lifetime, but do
not survive application exit. The upstream media-path/index-cache fix (#752) is
also included. Update the native application and Python server together.

Version 0.3.2 added **View > Disconnect current agent (allow another)**. It revokes
the current client's connection while keeping the document open and agent access
enabled. The other agent can use `list_sessions` and `connect_session` on demand.
The disconnected client cannot reconnect through background polling; it must
explicitly connect again. `disconnect_session` also lets an agent release its own
connection. **Disable all agent access** keeps every client disconnected instead.

See **[MCP setup and usage](docs/mcp.md)**. The integration consists of a native
bridge in this fork plus a Python stdio server in `mcp/`. An upstream Aegisub
binary does not contain the bridge. Normal launches leave MCP disabled.

**[Windows x64 downloads](https://github.com/F767-R/aegisub-mcp/releases)**:
installer and portable ZIP. The portable package includes the MCP server and
`setup-mcp.cmd` (Python 3.11+ required for MCP). Windows releases are published
only after the packaged application passes the native MCP integration tests.

See **[Windows build and monitoring instructions](docs/build-mcp-windows.md)**.
MCP does not open media, execute Lua, rename/delete styles, or save files remotely.
Save and review changes in the GUI.

---

For binaries and general information [see the homepage](http://aegisub.org).

The bug tracker can be found at https://github.com/TypesettingTools/Aegisub/issues.

Support is available on [Discord](https://discord.com/invite/AZaVyPr) or [IRC](irc://irc.rizon.net/aegisub).

## Building Aegisub

### Windows

Prerequisites:

1. Visual Studio (Community edition of any recent version is fine, needs the Windows SDK included)
2. Python 3
3. Meson
4. CMake

There are a few optional dependencies that must be installed and on your PATH:

1. msgfmt, to build the translations (installing from https://mlocati.github.io/articles/gettext-iconv-windows.html seems to be the easiest option)
2. InnoSetup, to build the regular installer (iscc.exe on your PATH)
3. 7zip, to build the regular installer (7z.exe on your PATH)

All other dependencies are either stored in the repository or are included as submodules.

Building:

1. Clone Aegisub's repository: `git clone https://github.com/TypesettingTools/Aegisub.git`
2. From the Visual Studio "x64 Native Tools Command Prompt", generate the build directory: `meson build -Ddefault_library=static` (if building for release, add `--buildtype=release`)
3. Build with `cd build` and `ninja`

You should now have a binary: `aegisub.exe`.

Installer:

You can generate the installer with `ninja win-installer` after a successful build. This assumes a working internet connection and installation of the optional dependencies.

You can generate the portable zip with `ninja win-portable` after a successful build.

### OS X

A vaguely recent version of Xcode and the corresponding command-line tools are required.

For personal usage, you can use pip and homebrew to install almost all of Aegisub's dependencies:

    pip3 install meson      # or brew install meson if you installed Python via brew
    brew install cmake ninja pkg-config  libass boost zlib ffms2 fftw hunspell uchardet
    export LDFLAGS="-L/usr/local/opt/icu4c/lib"
    export CPPFLAGS="-I/usr/local/opt/icu4c/include"
    export PKG_CONFIG_PATH="/usr/local/opt/icu4c/lib/pkgconfig"

When compiling on Apple Silicon, replace `/usr/local` with `/opt/homebrew`.

Once the dependencies are installed, build Aegisub with `meson build && meson compile -C build`.

#### Build a local DMG

Homebrew bottles target the macOS release they were built for, which may be
newer than Aegisub's default deployment target. For a local package, target
the current macOS major release and explicitly request an ad-hoc signature.
The resulting DMG is for development and personal use on that macOS release;
release packages use CI's source-built dependencies and the
[macOS release-signing process](docs/developer_docs.md#macos-release-signing).

```bash
deployment_target="$(sw_vers -productVersion)"
meson setup build_static \
  -Ddefault_library=static \
  -Dbuildtype=debugoptimized \
  -Dbuild_osx_bundle=true \
  -Dmacos_deployment_target="${deployment_target}" \
  --force-fallback-for=boost
meson compile -C build_static
meson test -C build_static --verbose
export AEGISUB_BUNDLE_SIGNATURE=-
meson compile osx-bundle -C build_static
meson compile osx-build-dmg -C build_static
```

### Linux or other

#### Build dependencies for Debian-based systems

```
compiler:    build-essential
pkgconfig:   pkg-config  or  pkgconf
meson:       meson ninja-build
gettext:     gettext intltool
fontconfig:  libfontconfig1-dev
libass:      libass-dev
boost:       libboost-chrono-dev libboost-locale-dev libboost-regex-dev libboost-system-dev libboost-thread-dev
zlib:        zlib1g-dev
WxWidgets:   wx3.2-headers libwxgtk3.2-dev
ICU:         icu-devtools libicu-dev
pulse-audio: libpulse-dev
ALSA:        libasound2-dev
OpenAL:      libopenal-dev
ffms2:       libffms2-dev
fftw3:       libfftw3-dev
hunspell:    libhunspell-dev
uchardet:    libuchardet-dev
libcurl:     libcurl4-openssl-dev  or  libcurl4-gnutls-dev
opengl:      libgl1-mesa-dev
gtest:       libgtest-dev
gmock:       libgmock-dev
libportal:   libportal-gtk3-dev
```

I.e. to install on Ubuntu 24.04 run this command:
``` bash
sudo apt install build-essential pkg-config meson ninja-build gettext intltool libfontconfig1-dev libass-dev libboost-chrono-dev libboost-locale-dev libboost-regex-dev libboost-system-dev libboost-thread-dev zlib1g-dev wx3.2-headers libwxgtk3.2-dev icu-devtools libicu-dev libpulse-dev libasound2-dev libopenal-dev libffms2-dev libfftw3-dev libhunspell-dev libuchardet-dev libcurl4-gnutls-dev libgl1-mesa-dev libgtest-dev libgmock-dev libportal-gtk3-dev
```

#### Build Aegisub

``` bash
meson setup build --prefix=/usr/local --buildtype=release --strip -Dsystem_luajit=false -Ddefault_library=static
meson compile -C build
meson install -C build --skip-subprojects luajit
```

#### Packaging
If you are packaging Aegisub for a Linux distribution, here are a few things you may need to know:
- Aegisub depends on LuaJIT and *requires* LuaJIT to be build with Lua 5.2 compatibility enabled.
  We are aware that most distributions do not compile LuaJIT with this flag, and that this complicates packaging for them, see https://github.com/TypesettingTools/Aegisub/issues/239 for a detailed discussion of the situation.

  Like for its other dependencies, Aegisub includes a meson subproject for LuaJIT that can be used to statically link a version of LuaJIT with 5.2 compatibility.
  For distributions that do not allow downloading additional sources at build time, the downloaded LuaJIT subproject is included in the source tarballs distributed with releases.
- When linked against libstdc++, Aegisub needs libstdc++ 6.0.32 or later due to https://gcc.gnu.org/bugzilla/show_bug.cgi?id=95048.
  Aegisub's tests will detect this bug, but if you're not running tests on packaging you'll need to make sure the libstdc++ version is recent enough.
- Aegisub uses OpenGL through wxWidgets. For Aegisub to work directly on Wayland (as opposed to Xwayland), wxWidgets needs to be built with EGL enabled.
  Aegisub will automatically fall back to X11 when it detects missing EGL support.

The following commands are an example for how to build Aegisub with the goal of creating a distribution package:

```bash
meson subprojects download luajit              # Or use the tarball
meson subprojects packagefiles --apply luajit

meson setup builddir --wrap-mode=nodownload --prefix=/usr --buildtype=release -Dsystem_luajit=false -Ddefault_library=static -Dtests=false

meson compile -C builddir
meson install -C builddir --skip-subprojects luajit
```

## Developer Documenation
Some documentation for developers is available in [docs/developer_docs.md](docs/developer_docs.md).

## License

All files in this repository are licensed under various GPL-compatible BSD-style licenses; see LICENCE and the individual source files for more information.
The official Windows and OS X builds are GPLv2 due to including fftw3.
