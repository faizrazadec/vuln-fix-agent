"""The executor must distinguish a finished run from a stalled/errored one.

Regression test for the bug where an agent that stopped mid-work (ended its turn to
await a notification, or hit the turn cap) was marked COMPLETED carrying a mid-work note.
Run: uv run python test_completion.py
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
        self.completed = self.failed_with = None
    def new_agent_message(self, parts):
        return "".join(p.text for p in parts)
    async def start_work(self): ...
    async def update_status(self, *a, **k): ...
    async def complete(self, msg): self.completed = msg
    async def failed(self, msg): self.failed_with = msg


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

    print("ok")
