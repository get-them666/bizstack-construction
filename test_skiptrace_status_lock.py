"""The status endpoint must never wait on the worker's lock.

WHY THIS FILE EXISTS
--------------------
On 2026-11-15 the whole site stopped answering -- /health included -- while
uvicorn was alive and listening. A SIGUSR1 stack dump put the event loop here:

    File "/app/lead_skiptrace_job.py", line 64 in status
    File "/app/construction_main.py", line 7881 in lead_skiptrace_status

`/api/leads/skiptrace-status` is polled by the browser every two seconds, and
it took the same lock the batch worker held while it wrote a lead row and sent
an email. One poll parked the event loop behind that lock, and every other
request queued behind it. These tests pin the two things that prevent it:

  1. reading the state never blocks, even while the worker holds the lock
  2. the worker does not hold the lock across database or network work
"""
from __future__ import annotations

import time

import lead_skiptrace_job
import owner_backfill_job


def test_skiptrace_status_does_not_block_while_worker_holds_the_lock():
    lock = lead_skiptrace_job._LOCK
    assert lock.acquire(), "could not take the worker lock for the test"
    try:
        started = time.time()
        snapshot = lead_skiptrace_job.status()
        elapsed = time.time() - started
    finally:
        lock.release()
    assert elapsed < 0.5, f"status() waited {elapsed:.2f}s on a contended lock"
    assert isinstance(snapshot, dict)
    assert "running" in snapshot


def test_owner_backfill_status_does_not_block_while_worker_holds_the_lock():
    lock = owner_backfill_job._LOCK
    assert lock.acquire()
    try:
        started = time.time()
        snapshot = owner_backfill_job.status()
        elapsed = time.time() - started
    finally:
        lock.release()
    assert elapsed < 0.5, f"status() waited {elapsed:.2f}s on a contended lock"
    assert isinstance(snapshot, dict)


def test_worker_does_not_hold_the_lock_across_io():
    """A `with _LOCK` block in _run may only touch counters.

    The deadlock was not the read; it was the write. The lock was held across
    an UPDATE, a commit, a lead recompute and an email send, so the reader could
    not have finished even with a generous timeout. Checked on the syntax tree
    so it cannot be fooled by indentation or comments.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(lead_skiptrace_job._run).lstrip())
    locked = []
    for node in ast.walk(tree):
        if isinstance(node, ast.With) and any(
            isinstance(item.context_expr, ast.Name) and item.context_expr.id == "_LOCK"
            for item in node.items
        ):
            locked.append(node)
    assert locked, "no `with _LOCK` blocks found in _run"

    allowed = (ast.Assign, ast.AugAssign, ast.If, ast.Expr, ast.Pass)
    for block in locked:
        for stmt in block.body:
            assert isinstance(stmt, allowed), (
                f"{type(stmt).__name__} runs while _LOCK is held in _run "
                f"(line {stmt.lineno}); only counter updates belong there")
            if isinstance(stmt, ast.Assign):
                targets = stmt.targets
            elif isinstance(stmt, ast.AugAssign):
                targets = [stmt.target]
            else:
                continue
            for target in targets:
                assert isinstance(target, ast.Subscript), (
                    "a lock block assigns something other than _STATE[...]")
                assert getattr(target.value, "id", "") == "_STATE", (
                    "a lock block touches state outside _STATE")
