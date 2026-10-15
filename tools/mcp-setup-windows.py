"""Install the bundled MCP server without changing global Python packages."""
import json
from pathlib import Path
import subprocess
import sys
import venv


def main():
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11 or newer is required.")
    root = Path(__file__).resolve().parent
    executable = root / "aegisub.exe"
    if not executable.is_file():
        raise SystemExit("Run setup-mcp.cmd inside the extracted portable package.")
    environment = root / ".venv"
    venv.create(environment, with_pip=True)
    python = environment / "Scripts" / "python.exe"
    subprocess.run([str(python), "-m", "pip", "install", str(root / "mcp")], check=True)
    config = {"mcpServers": {"aegisub": {
        "command": str(python),
        "args": ["-m", "aegisub_mcp.server", "--aegisub", str(executable), "--allow-write"],
    }}}
    destination = root / "mcp-config.json"
    destination.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"Ready. Copy the server entry from {destination} into your MCP client.")
    print("This configuration permits subtitle edits. Remove --allow-write for read-only access.")
    print("Keep this folder in place: the generated configuration uses absolute paths.")


if __name__ == "__main__":
    main()
