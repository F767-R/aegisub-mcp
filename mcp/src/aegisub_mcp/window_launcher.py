"""Short-lived GUI launcher: the window must outlive MCP process-tree cleanup."""
import argparse
from pathlib import Path
import time

from .bridge import LaunchedSession, atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--aegisub', required=True, type=Path)
    parser.add_argument('--result', required=True, type=Path)
    parser.add_argument('--project', type=Path)
    parser.add_argument('--allow-write', action='store_true')
    args = parser.parse_args()
    session = None
    result = {}
    try:
        session = LaunchedSession(args.aegisub, args.project, args.allow_write)
        result = dict(launched=True, pid=session.process.pid, session_id=session.directory.name)
        deadline = time.monotonic() + 20
        while not (session.directory / 'session.json').is_file():
            if session.process.poll() is not None:
                raise RuntimeError('Aegisub exited before enabling its connection')
            if time.monotonic() >= deadline:
                raise RuntimeError('Window started but connection is not ready; inspect the GUI and list_sessions before retrying')
            time.sleep(0.05)
        result['ready'] = True
    except Exception as exc:
        result['error'] = str(exc)
    finally:
        if session:
            # Release the lease before the parent connects. Never close the GUI.
            session.close()
        atomic_json(args.result, result)


if __name__ == '__main__':
    main()
