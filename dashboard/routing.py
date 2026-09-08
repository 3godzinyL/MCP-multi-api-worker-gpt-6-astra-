"""Bind provider roles only to identities reported by the owned App Server.

Codex 0.153.4 keeps the parent's base URL and session-id for spawned agents,
but sends a distinct thread-id and turn_id. The authoritative parent/child
relationship is the App Server subAgentActivity event, not an HTTP header or
the order in which model requests arrive.
"""
from __future__ import annotations

from collections.abc import Mapping


def _child_id(value, parent_thread_id, thread_tasks, task_id):
    if not isinstance(value, str) or not value or value == parent_thread_id:
        return None
    if thread_tasks.get(value) not in (None, task_id):
        return None
    return value


def verified_child_threads(parent_thread_id, item, thread_tasks, task_id):
    """Extract child ids from a trusted notification on a known parent thread.

    Call this for App Server notifications only. User payloads, provider
    responses and x-codex-* request headers are not proof of a relationship.
    A finished/failed activity cannot introduce a previously unknown child.
    """
    if (not isinstance(parent_thread_id, str) or parent_thread_id not in thread_tasks
            or thread_tasks[parent_thread_id] != task_id):
        return ()
    if not isinstance(item, Mapping):
        return ()
    kind = item.get("type")
    if kind == "subAgentActivity" and item.get("kind") == "started":
        candidates = [item.get("agentThreadId")]
    elif kind == "collabAgentToolCall" and item.get("tool") == "spawnAgent":
        if item.get("status") in ("failed", "errored", "interrupted"):
            return ()
        candidates = item.get("receiverThreadIds", [])
        if not isinstance(candidates, list):
            return ()
    else:
        return ()
    return tuple(dict.fromkeys(child for value in candidates
                               if (child := _child_id(value, parent_thread_id, thread_tasks, task_id))))


def verified_child_thread(parent_thread_id, item, thread_tasks, task_id):
    """Return the single child of a spawn activity, or None if not verified."""
    children = verified_child_threads(parent_thread_id, item, thread_tasks, task_id)
    return children[0] if len(children) == 1 else None


def verified_thread_start(thread, thread_tasks):
    """Support older App Servers that announce a child via thread/started."""
    if not isinstance(thread, Mapping):
        return None
    source = thread.get("source")
    subagent = source.get("subAgent") if isinstance(source, Mapping) else None
    spawned = subagent.get("thread_spawn") if isinstance(subagent, Mapping) else None
    if not isinstance(spawned, Mapping):
        spawned = {}
    parent = thread.get("parentThreadId") or spawned.get("parent_thread_id")
    if not isinstance(parent, str) or parent not in thread_tasks:
        return None
    task_id = thread_tasks[parent]
    child = _child_id(thread.get("id"), parent, thread_tasks, task_id)
    return (task_id, child) if child else None
