"""Executor semantics: what counts as finished, and what happens when one is already running.

Regression test for the bug where an agent that stopped mid-work (ended its turn to
await a notification, or hit the turn cap) was marked COMPLETED carrying a mid-work note.
Also covers the single-flight lock, which keeps a second caller from starting a Claude
session — and a second docker build — on top of a run already in progress.
Run: uv run python tests/test_completion.py
"""

import asyncio
import os

os.environ.setdefault("A2A_TOKEN", "test-token")

import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent / "app"))
import main  # noqa: E402
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock  # noqa: E402


def R(**kw):
    """A real ResultMessage with sensible defaults, overridable per case."""
    base = dict(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                num_turns=5, session_id="s", result="final report text", errors=None)
    base.update(kw)
    return ResultMessage(**base)


class FakeUpdater:
    def __init__(self):
        self.completed = self.failed_with = self.cancelled = None
    def new_agent_message(self, parts):
        return "".join(p.text for p in parts)
    async def start_work(self): ...
    async def update_status(self, *a, **k): ...
    async def complete(self, msg): self.completed = msg
    async def failed(self, msg): self.failed_with = msg
    async def cancel(self, msg=None): self.cancelled = msg


class _Ctx:
    task_id = "t1"; context_id = "c1"; current_task = object()  # skip new_task enqueue
    def get_user_input(self): return "fix vulns in demo"


class _Q:
    async def enqueue_event(self, *a, **k): ...


def drive(result_msg, blocks=("Progress so far:",)):
    up = FakeUpdater()

    async def fake_query(prompt, options):
        yield AssistantMessage(content=[TextBlock(text=t) for t in blocks], model="m")
        if result_msg is not None:
            yield result_msg

    orig_q, orig_u = main.query, main.TaskUpdater
    main.query = fake_query
    main.TaskUpdater = lambda *a, **k: up
    try:
        asyncio.run(main.ClaudeCodeExecutor().execute(_Ctx(), _Q()))
    finally:
        main.query, main.TaskUpdater = orig_q, orig_u
    return up


def test_rejects_a_concurrent_run():
    """A second SendMessage while a run is in flight must be refused, not queued.

    Queueing would hand the caller a task that silently sits for 20 minutes; running it
    would put two Claude sessions and two docker builds on a memory-constrained VM at once.
    """
    up = FakeUpdater()

    async def scenario():
        await main._RUN_LOCK.acquire()            # stand in for a run already going
        main._RUNNING["busy-task"] = None
        try:
            orig = main.TaskUpdater
            main.TaskUpdater = lambda *a, **k: up
            try:
                await main.ClaudeCodeExecutor().execute(_Ctx(), _Q())
            finally:
                main.TaskUpdater = orig
        finally:
            main._RUNNING.pop("busy-task", None)
            main._RUN_LOCK.release()

    asyncio.run(scenario())
    assert up.completed is None, up.__dict__
    assert up.failed_with and "Busy" in up.failed_with, up.__dict__
    assert "busy-task" in up.failed_with, "should name what it is busy with"
    print("concurrent run -> rejected, not queued ✓")


def test_lock_is_released_after_a_run():
    """A failed run must not leave the lock held — that would wedge every later request."""
    drive(R(is_error=True, subtype="error_during_execution", errors=["boom"]))
    assert not main._RUN_LOCK.locked(), "lock still held after a failed run"
    assert not main._RUNNING, f"registry not cleaned: {main._RUNNING}"
    print("lock and run registry released after a failed run ✓")


if __name__ == "__main__":
    up = drive(R(result="DONE: PR opened"))
    assert up.completed == "DONE: PR opened" and up.failed_with is None, up.__dict__
    print("success -> complete, uses result text (not the progress chunk) ✓")

    up = drive(R(num_turns=main.MAX_TURNS))
    assert up.completed is None and "turn cap" in up.failed_with, up.__dict__
    print("max_turns -> failed ✓")

    up = drive(R(is_error=True, subtype="error_during_execution", errors=["boom"]))
    assert up.completed is None and "errored" in up.failed_with, up.__dict__
    print("is_error -> failed ✓")

    up = drive(R(subtype="error_max_turns", is_error=False))
    assert up.completed is None and "did not finish cleanly" in up.failed_with, up.__dict__
    print("non-success subtype -> failed ✓")

    up = drive(None)
    assert up.completed is None and "without a ResultMessage" in up.failed_with, up.__dict__
    print("no result message -> failed ✓")

    test_rejects_a_concurrent_run()
    test_lock_is_released_after_a_run()

    print("ok")
