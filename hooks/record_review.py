#!/usr/bin/env python3
"""SubagentStop hook: when the plugin's second reader finishes, save its report where the EDGAR server looks, so that
check_answer can require that the review really ran and that each departure it raised was answered (11 Nov 2026:
declarations by the assistant that did the work did not change its reading of the question). Does nothing for any
other subagent, and never fails."""
import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if "second-reader" not in str(data.get("agent_type") or ""):
        return 0
    msg = data.get("last_assistant_message")
    if isinstance(msg, dict):
        msg = "\n".join(b.get("text", "") for b in msg.get("content") or [] if isinstance(b, dict)) or str(msg.get("text") or "")
    entry = {"at": time.time(), "text": str(msg or "")[:20000]}
    explicit = os.environ.get("EDGAR_STATE_DIR")
    places = [explicit] if explicit and not explicit.startswith("${") else [os.environ.get("CLAUDE_PLUGIN_DATA"), str(Path(data.get("cwd") or os.getcwd()) / ".edgar")]
    places = [d for d in places if d and not d.startswith("${")]
    if not (explicit and not explicit.startswith("${")):
        places.append(None)  # 21 Nov, Cowork: ~/.edgar-agent, only if none of the places above could be written
    written = False
    for d in places:
        if d is None:
            if written:
                continue
            d = str(Path.home() / ".edgar-agent")
        try:
            Path(d).mkdir(parents=True, exist_ok=True)
            p = Path(d) / "last_review.json"
            # 15 Nov: a draft was reviewed, corrected and reviewed again; only the last, clean review was kept, and the
            # answer said the reviewer "found no departures". The earlier reviews are kept so the answer can say what happened.
            try:
                earlier = json.loads(p.read_text())
                history = [e for e in (earlier.get("history") or [{"at": earlier.get("at", 0), "text": earlier.get("text", "")}])
                           if isinstance(e, dict) and time.time() - e.get("at", 0) < 6 * 3600][-7:]
            except (OSError, ValueError):
                history = []
            p.write_text(json.dumps({**entry, "agent_type": str(data.get("agent_type")), "history": history + [entry]}))
            written = True
        except OSError:
            continue
    return 0


if __name__ == "__main__":
    sys.exit(main())
