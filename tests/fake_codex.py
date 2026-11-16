"""A stand-in for `codex exec --json [options] [resume <id>] -`: reads the prompt from stdin and
prints Codex's JSONL events. FAKE_CODEX picks the behaviour:

  ok       write done.txt, commit, write result.json, complete the turn
  repair   the first turn completes without done.txt; a resumed turn writes it
  fail     exit 1 after an error event, no completed turn
  stall    start a turn and then print nothing
"""

import json
import os
import subprocess
import sys
import time
import uuid

argv = sys.argv[1:]
if os.environ.get("FAKE_AGENT_ARGV_LOG"):
    with open(os.environ["FAKE_AGENT_ARGV_LOG"], "a") as f:
        f.write(json.dumps(argv) + "\n")
mode = os.environ.get("FAKE_CODEX", "ok")
resumed = "resume" in argv
thread = argv[argv.index("resume") + 1] if resumed else str(uuid.uuid4())
prompt = sys.stdin.read()


def emit(obj):
    print(json.dumps(obj), flush=True)


print("Reading additional input from stdin...", flush=True)  # codex's notice, not JSON
emit({"type": "thread.started", "thread_id": thread})
emit({"type": "turn.started"})
if mode == "stall":
    time.sleep(3600)
if mode == "fail":
    emit({"type": "error", "message": "model unavailable"})
    sys.exit(1)
if mode == "ok" or (mode == "repair" and resumed):
    with open("done.txt", "w") as f:
        f.write(prompt[:40])
    subprocess.run(["git", "add", "done.txt"], check=True)
    subprocess.run(["git", "commit", "-q", "-m", "done"], check=True)
    with open(os.path.join(os.environ["AFE_OUT"], "result.json"), "w") as f:
        json.dump({"outcome": "success", "summary": "fake codex"}, f)
emit({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "done"}})
emit({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
