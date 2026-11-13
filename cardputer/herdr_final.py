#!/usr/bin/env python3
"""Print the last assistant text for a Claude Code session.

Used by cardputer-pager over SSH:

    herdr-final <session-id> [--chars N]

It finds ~/.claude/projects/**/<session-id>.jsonl, walks the JSONL and prints
the last assistant message whose content has a text block. Thinking and
tool_use blocks are ignored, so the CardputerZero only sees the final answer.
"""

import argparse
import glob
import json
import os
import sys


def find_transcript(session):
    root = os.path.expanduser("~/.claude/projects")
    hits = glob.glob(os.path.join(root, "**", f"{session}.jsonl"), recursive=True)
    if not hits:
        return None
    return max(hits, key=os.path.getmtime)


def last_text(path, limit):
    text = ""
    ts = ""
    with open(path, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") != "assistant":
                continue
            content = (row.get("message") or {}).get("content") or []
            if not isinstance(content, list):
                continue
            parts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            joined = "\n".join(p for p in parts if p.strip()).strip()
            if joined:
                text = joined
                ts = row.get("timestamp", "")
    if limit > 0 and len(text) > limit:
        text = text[:limit].rstrip() + "\n…"
    return text, ts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("--chars", type=int, default=4000)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    path = find_transcript(a.session)
    if not path:
        print(f"no transcript for {a.session}", file=sys.stderr)
        return 1
    text, ts = last_text(path, a.chars)
    if not text:
        print(f"no assistant text in {path}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps({"text": text, "timestamp": ts, "path": path}, ensure_ascii=False))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
