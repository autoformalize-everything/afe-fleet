"""A stand-in for the agent CLI, speaking the same JSONL protocol.

FAKE_AGENT selects the behaviour:
  work         write solution.txt ("ok"), commit, result.json success
  fix          first turn writes a wrong solution; the repair turn fixes it
  nothing      change nothing; result.json failure
  uncommitted  write solution.txt but do not commit (the worker commits it)
  approve      review.json approve
  changes      review.json changes
  hang         print init, then never answer
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

mode = os.environ.get("FAKE_AGENT", "work")
work = Path(os.environ.get("AFE_WORK", "."))
out = Path(os.environ.get("AFE_OUT", "."))


def emit(obj):
    print(json.dumps(obj), flush=True)


def commit(msg):
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=work, check=True)


emit({"type": "system", "subtype": "init", "session_id": f"fake-{os.getpid()}"})
turn = 0
for line in sys.stdin:
    msg = json.loads(line)
    assert msg["type"] == "user"
    turn += 1
    if mode == "hang":
        time.sleep(3600)
    if mode in ("work", "uncommitted"):
        (work / "solution.txt").write_text("ok\n")
        with (work / "attempts.txt").open("a") as f:  # every attempt changes something
            f.write(f"attempt by pid {os.getpid()}\n")
        if mode == "work":
            commit("solve")
        (out / "result.json").write_text(json.dumps({"outcome": "success", "summary": "wrote solution.txt"}))
    elif mode == "fix":
        (work / "solution.txt").write_text("wrong\n" if turn == 1 else "ok\n")
        commit(f"attempt {turn}")
        (out / "result.json").write_text(json.dumps({"outcome": "success", "summary": f"turn {turn}"}))
    elif mode == "nothing":
        (out / "result.json").write_text(json.dumps({"outcome": "failure", "debug": "could not do it"}))
    elif mode in ("approve", "changes"):
        (out / "review.json").write_text(json.dumps({"verdict": mode, "summary": f"fake review: {mode}",
                                                     "comments": [{"path": "solution.txt", "line": 1,
                                                                   "body": "looked at it"}]}))
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": f"turn {turn}"}]}})
    emit({"type": "result", "subtype": "success", "num_turns": turn})
