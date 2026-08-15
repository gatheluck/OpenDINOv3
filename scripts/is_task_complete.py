#!/usr/bin/env python3
"""Does this task's marker account for the whole task?

  is_task_complete.py <DONE.json> <plan.json> <task id>

Prints one word and exits 0: `skip`, or a reason beginning `redo`.

WHY IT IS A SCRIPT

The runner and the wave summary both need this answer, and they had
separate implementations. The runner compared the recorded candidates
against the plan's row count; the summary still looked for a `partial`
flag that the runner had stopped writing as a decision input. So a wave
reported "3 complete, will be skipped" for three tasks it was about to
redo — harmless, but the two rules were already disagreeing about the
corpus.

One implementation, called from both.

THE RULE

A marker is trusted only when it accounts for as many candidates as the
plan allots the task. Tasks 8-10 carry markers written before the flag
existed — 100,000 candidates against a plan that allots 1,000,000 — so a
flag-based check skips them forever and 2.7 million URLs stay missing.

A marker that cannot be checked is redone. Resumption makes that nearly
free: finished shards are kept, nothing is re-downloaded, and the marker is
rewritten with the numbers to verify against next time.
"""

from __future__ import annotations

import json
import sys


def verdict(marker_path: str, plan_path: str, task_id: int) -> str:
    try:
        done = json.load(open(marker_path))
        plan = json.load(open(plan_path))
    except Exception:
        return "redo unverifiable"

    got = done.get("candidates")
    want = next((int(t["rows"]) for t in plan.get("tasks", [])
                 if int(t["task_id"]) == task_id), None)
    if got is None or want is None:
        return "redo unverifiable"
    if int(got) < want:
        return f"redo short {got} of {want}"
    return "skip"


def main() -> int:
    if len(sys.argv) != 4:
        print("redo unverifiable")
        return 0
    print(verdict(sys.argv[1], sys.argv[2], int(sys.argv[3])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
