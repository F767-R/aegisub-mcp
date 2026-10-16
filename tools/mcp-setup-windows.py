"""Install the bundled MCP server without changing global Python packages."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import venv


def configure_desktop(destination, server=None):
    """Merge or remove only Aegisub, backing up existing settings before replacement."""
    original = destination.read_bytes() if destination.exists() else None
    config = json.loads(original.decode("utf-8-sig")) if original is not None else {}
    if not isinstance(config, dict) or not isinstance(config.get("mcpServers", {}), dict):
        raise ValueError("Claude settings and mcpServers must be JSON objects; file left unchanged.")
    servers = config.setdefault("mcpServers", {})
    if server is None:
        if "aegisub" not in servers:
            return None
        del servers["aegisub"]
    else:
        if servers.get("aegisub") == server:
            return None
        servers["aegisub"] = server
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if original is not None:
        backup = destination.with_name(f"{destination.name}.{time.time_ns()}.bak")
        shutil.copy2(destination, backup)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                         delete=False) as output:
            temporary = Path(output.name)
            output.write(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return backup


def server_config(root, read_only=False):
    args = ["-m", "aegisub_mcp.server", "--aegisub", str(root / "aegisub.exe")]
    if not read_only:
        args.append("--allow-write")
    return {"command": str(root / ".venv" / "Scripts" / "python.exe"), "args": args}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    desktop = parser.add_mutually_exclusive_group()
    desktop.add_argument("--claude-desktop", action="store_true", help="Register with Claude Desktop")
    desktop.add_argument("--remove-claude-desktop", action="store_true", help="Disconnect from Claude Desktop")
    parser.add_argument("--skip-install", action="store_true", help="Reuse the existing Python environment")
    parser.add_argument("--read-only", action="store_true", help="Expose only read tools")
    options = parser.parse_args()
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11 or newer is required.")
    desktop_path = None
    if options.claude_desktop or options.remove_claude_desktop:
        if not os.environ.get("APPDATA"):
            raise SystemExit("APPDATA is missing. Run setup as your Windows user, without administrator privileges.")
        desktop_path = Path(os.environ["APPDATA"]) / "Claude" / "claude_desktop_config.json"
    if options.remove_claude_desktop:
        backup = configure_desktop(desktop_path)
        print(f"Aegisub disconnected in {desktop_path}. Fully quit and reopen Claude Desktop.")
        if backup:
            print(f"Previous settings: {backup}")
        return
    root = Path(__file__).resolve().parent
    executable = root / "aegisub.exe"
    if not executable.is_file():
        raise SystemExit("Run setup-mcp.cmd inside the extracted portable package.")
    environment = root / ".venv"
    python = environment / "Scripts" / "python.exe"
    if options.skip_install:
        if not python.is_file():
            raise SystemExit("No local Python environment. Run setup-mcp.cmd first.")
    else:
        if not python.is_file():
            venv.create(environment, with_pip=True)
        subprocess.run([str(python), "-m", "pip", "install", str(root / "mcp")], check=True)
    subprocess.run([str(python), "-c", "import aegisub_mcp.server"], check=True)
    server = server_config(root, options.read_only)
    config = {"mcpServers": {"aegisub": server}}
    destination = root / "mcp-config.json"
    destination.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"Ready. Copy the server entry from {destination} into your MCP client.")
    if desktop_path is not None:
        backup = configure_desktop(desktop_path, server)
        print(f"Claude Desktop configured: {desktop_path}")
        if backup:
            print(f"Previous settings: {backup}")
        print("Fully quit and reopen Claude Desktop, then ask it to use Aegisub's get_document tool.")
        print("To disconnect later: setup-mcp.cmd --remove-claude-desktop")
    print("Read-only access." if options.read_only else "Subtitle editing enabled. Use --read-only for read-only access.")
    print("Keep this folder in place: the generated configuration uses absolute paths.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Setup failed: {error}") from error
