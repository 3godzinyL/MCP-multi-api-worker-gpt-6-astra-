"""Deterministic concurrent App Server peer, with no network or real model calls."""
import json
import sys
import threading
import time
import uuid
from pathlib import Path

threads = {}
lock = threading.Lock()
barrier = threading.Barrier(2)
counter = 0

def send(value):
    with lock:
        print(json.dumps(value), flush=True)

def notify(method, tid, **params):
    send({"method": method, "params": {"threadId": tid, **params}})

def work(tid, turn_id, params):
    entry = threads[tid]
    if params.get("outputSchema"):
        plan = {"summary": "Two workers", "contract": "Separate files", "workers": [
            {"name": "Worker A", "task": "Write a.txt", "owned_paths": ["a.txt"], "validation": "Read a.txt"},
            {"name": "Worker B", "task": "Write b.txt", "owned_paths": ["b.txt"], "validation": "Read b.txt"}]}
        text = json.dumps(plan)
    else:
        # Neither worker can finish unless the panel actually starts both.
        barrier.wait(timeout=8)
        name = "a.txt" if entry["cwd"].endswith("worker1") else "b.txt"
        Path(entry["cwd"], name).write_text("first\nsecond\n", encoding="utf-8")
        time.sleep(.15)
        text = "Completed " + name
    notify("thread/tokenUsage/updated", tid, tokenUsage={"total": {"inputTokens": 80, "outputTokens": 20, "totalTokens": 100}})
    notify("item/completed", tid, item={"id": tid + "-message", "type": "agentMessage", "text": text})
    notify("turn/completed", tid, turnId=turn_id, turn={"id": turn_id, "status": "completed", "items": [], "error": None})

for line in sys.stdin:
    message = json.loads(line)
    params = message.get("params", {})
    method, rid = message.get("method"), message.get("id")
    if method == "initialize":
        send({"id": rid, "result": {}})
    elif method == "model/list":
        send({"id": rid, "result": {"data": [{"model": "fixture-model", "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ["low", "max", "ultra"]]}]}})
    elif method in {"thread/start", "thread/resume"}:
        counter += 1
        tid = params.get("threadId", "team-thread-" + uuid.uuid4().hex)
        threads[tid] = params
        send({"id": rid, "result": {"thread": {"id": tid}}})
    elif method == "turn/start":
        tid = params["threadId"]
        turn_id = "team-turn-" + uuid.uuid4().hex
        threads[tid]["turn_id"] = turn_id
        send({"id": rid, "result": {"turn": {"id": turn_id}}})
        notify("turn/started", tid, turnId=turn_id, turn={"id": turn_id})
        threading.Thread(target=work, args=(tid, turn_id, params), daemon=True).start()
    elif method == "turn/interrupt":
        send({"id": rid, "result": {}})
        notify("turn/completed", params["threadId"], turnId=threads[params["threadId"]]["turn_id"], turn={"id": threads[params["threadId"]]["turn_id"], "status": "interrupted"})
