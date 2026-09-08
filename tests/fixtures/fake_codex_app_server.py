"""Offline JSON-RPC peer: independent chats/turns, never contacts an AI API."""
import json
import sys
import uuid
from pathlib import Path

threads = {}
pending = {}
request_counter = 0

def send(value):
    print(json.dumps(value), flush=True)

def notify(method, thread_id, params):
    send({"method": method, "params": {"threadId": thread_id, "turnId": threads[thread_id].get("turn_id"), **params}})

def complete(thread_id, write=True, status="completed"):
    entry = threads[thread_id]
    turn_id = entry["turn_id"]
    if write:
        (entry["folder"] / "created-by-task.txt").write_text("one\ntwo\n", encoding="utf-8")
        notify("item/completed", thread_id, {"item": {"id": turn_id + "-command", "type": "commandExecution", "command": "write fixture file",
                                          "aggregatedOutput": "FIXTURE_OK", "exitCode": 0, "status": "completed"}})
    notify("thread/tokenUsage/updated", thread_id, {"tokenUsage": {"total": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120, "cachedInputTokens": 50, "reasoningOutputTokens": 5}}})
    notify("item/agentMessage/delta", thread_id, {"itemId": turn_id + "-message", "delta": "Task "})
    notify("item/agentMessage/delta", thread_id, {"itemId": turn_id + "-message", "delta": "complete"})
    notify("item/completed", thread_id, {"item": {"id": turn_id + "-message", "type": "agentMessage", "text": "Task complete"}})
    notify("turn/completed", thread_id, {"turn": {"id": turn_id, "status": status, "items": [], "error": None}})

for line in sys.stdin:
    value = json.loads(line)
    method, request_id = value.get("method"), value.get("id")
    params = value.get("params") or {}
    if method == "initialize":
        send({"id": request_id, "result": {"userAgent": "offline-fixture"}})
    elif method == "model/list":
        send({"id": request_id, "result": {"data": [{"model": "fixture-model", "supportedReasoningEfforts": [{"reasoningEffort": effort} for effort in ["low", "medium", "max", "ultra"]]}]}})
    elif method in {"thread/start", "thread/resume"}:
        thread_id = params["threadId"] if method == "thread/resume" else "fixture-thread-" + uuid.uuid4().hex
        threads[thread_id] = {"folder": Path(params["cwd"])}
        send({"id": request_id, "result": {"thread": {"id": thread_id}}})
    elif method == "turn/start":
        thread_id = params["threadId"]
        turn_id = "fixture-turn-" + uuid.uuid4().hex
        threads[thread_id]["turn_id"] = turn_id
        send({"id": request_id, "result": {"turn": {"id": turn_id, "status": "inProgress", "items": []}}})
        notify("turn/started", thread_id, {"turn": {"id": turn_id, "status": "inProgress", "items": []}})
        prompt = params["input"][0]["text"]
        if prompt in {"approve", "ask"}:
            request_counter += 1
            approval_id = "server-request-" + str(request_counter)
            pending[approval_id] = (thread_id, prompt)
            details = {"threadId": thread_id, "turnId": turn_id, "itemId": turn_id + "-approval", "reason": "fixture only", "command": "fixture write"}
            if prompt == "ask":
                details["questions"] = [{"id": "name", "header": "Name", "question": "Which name?"}]
            send({"id": approval_id, "method": "item/tool/requestUserInput" if prompt == "ask" else "item/commandExecution/requestApproval", "params": details})
        elif prompt != "hold":
            complete(thread_id)
    elif method == "turn/interrupt":
        send({"id": request_id, "result": {}})
        complete(params["threadId"], False, "interrupted")
    elif request_id in pending:
        thread_id, prompt = pending.pop(request_id)
        result = value.get("result", {})
        complete(thread_id, prompt == "ask" or result.get("decision") == "accept")
