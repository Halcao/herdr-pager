#!/usr/bin/env python3
"""herdr agent list + each agent's transcript mtime, as one JSON object.

cardputer-pager polls this once per five seconds instead of calling
`herdr --machine` — same number of requests, but the list can be sorted by the
real last-activity time of the Claude transcript.

Output:
    {"agents": [ <herdr agent fields>, ..., "mtime": <unix seconds or 0> ]}
"""

import glob
import json
import os
import shutil
import subprocess
import sys


def herdr_binary():
    return shutil.which("herdr") or os.path.expanduser("~/.local/bin/herdr")


def transcript(path_session):
    root = os.path.expanduser("~/.claude/projects")
    hits = glob.glob(os.path.join(root, "**", f"{path_session}.jsonl"), recursive=True)
    if not hits:
        return None
    return max(hits, key=os.path.getmtime)


def main():
    p = subprocess.run(
        [herdr_binary(), "agent", "list"],
        capture_output=True, text=True, timeout=20,
    )
    if p.returncode != 0:
        print((p.stderr or p.stdout or "herdr agent list failed").strip(), file=sys.stderr)
        return 1
    try:
        d = json.loads(p.stdout)
    except json.JSONDecodeError as e:
        print(f"herdr agent list: {e}", file=sys.stderr)
        return 1
    agents = d.get("result", {}).get("agents", [])
    for a in agents:
        session = (a.get("agent_session") or {}).get("value")
        mtime = 0
        if session:
            path = transcript(session)
            if path:
                mtime = int(os.path.getmtime(path))
        a["mtime"] = mtime
    print(json.dumps({"agents": agents}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
