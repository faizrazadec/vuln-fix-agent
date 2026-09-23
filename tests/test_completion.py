"""Executor semantics: what counts as finished, and what happens when one is already running.

Regression test for the bug where an agent that stopped mid-work (ended its turn to
await a notification, or hit the turn cap) was marked COMPLETED carrying a mid-work note.
Also covers the single-flight lock, which keeps a second caller from starting a Claude
session — and a second docker build — on top of a run already in progress.
Run: uv run python tests/test_completion.py
"""

import asyncio
import json
import os
import pathlib
import subprocess
import tempfile

os.environ.setdefault("A2A_TOKEN", "test-token")
# Every run now writes a summary, so point that at a scratch dir, not /home/agent/state.
_state = tempfile.TemporaryDirectory()
os.environ["STATE_DIR"] = _state.name

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


SEEN_OPTIONS = []


def drive(result_msg, blocks=("Progress so far:",)):
    up = FakeUpdater()

    async def fake_query(prompt, options):
        SEEN_OPTIONS.append(options)
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


def summaries():
    return [json.loads(f.read_text()) for f in sorted(pathlib.Path(_state.name, "runs").glob("*.json"))]


def latest_summary():
    runs = pathlib.Path(_state.name, "runs")
    newest = max(runs.glob("*.json"), key=lambda f: f.stat().st_mtime_ns)
    return json.loads(newest.read_text())


GOOD = {
    "project": "demo", "outcome": "fixed", "findings_total": 3, "findings_new": 2,
    "cves_fixed": ["CVE-1"], "cves_unfixable": [], "cves_already_fixed_in_base": [],
    "pr_url": "https://example/pull/1", "base_branch": "develop", "tests": "green",
    "image_scan": "green", "slack_notified": True, "clone_kept": None, "notes": "",
}


def report(summary):
    return f"All done.\n\n```json\n{json.dumps(summary)}\n```"


def test_summary_carries_run_metrics():
    """The server-measured numbers ride along with the agent's own summary."""
    drive(R(result=report(GOOD), num_turns=7, duration_ms=1234, total_cost_usd=0.42,
            usage={"input_tokens": 10, "output_tokens": 5}))
    s = latest_summary()
    assert s["run"]["status"] == "completed", s
    assert s["run"]["num_turns"] == 7 and s["run"]["duration_ms"] == 1234, s
    assert s["run"]["total_cost_usd"] == 0.42 and s["run"]["usage"]["output_tokens"] == 5, s
    assert "validation_errors" not in s, s
    print("summary carries turns, duration, cost and usage ✓")


def test_summary_contract_is_checked():
    """A summary that breaks SKILL.md step 10 is kept — it is the only record — but flagged."""
    bad = dict(GOOD, outcome="done", pr_url=None, slack_notified="yes")
    del bad["notes"]
    drive(R(result=report(bad)))
    errs = latest_summary().get("validation_errors", [])
    assert any("missing key: notes" in e for e in errs), errs
    assert any("outcome='done'" in e for e in errs), errs
    assert any("slack_notified" in e for e in errs), errs

    drive(R(result=report(dict(GOOD, pr_url=None))))
    errs = latest_summary().get("validation_errors", [])
    assert errs == ["outcome=fixed but pr_url is empty"], errs

    drive(R(result=report(dict(GOOD, outcome="nothing-to-do", slack_notified=None,
                               cves_fixed=[], pr_url=None))))
    assert "validation_errors" not in latest_summary(), "null slack_notified is legal"
    print("summary contract violations are flagged, not dropped ✓")


def test_failed_run_without_summary_still_recorded():
    """A run that died before its JSON block used to leave no trace in runs/ at all."""
    drive(R(num_turns=main.MAX_TURNS, result="Progress: halfway"))
    s = latest_summary()
    assert s["summary_missing"] is True and s["outcome"] == "error", s
    assert s["run"]["status"] == "failed" and "turn cap" in s["run"]["reason"], s
    print("a run with no summary block still gets a failure record ✓")


def test_project_from_prompt():
    orig = main.PROJECTS
    main.PROJECTS = {"Maritime": {}, "maritime-ai-hub": {}, "maritime_billing": {}}
    try:
        assert main._project_from_prompt("fix vulns in maritime-ai-hub") == "maritime-ai-hub"
        assert main._project_from_prompt("fix vulns in MARITIME") == "Maritime"
        assert main._project_from_prompt("fix vulns in maritime_billing") == "maritime_billing"
        assert main._project_from_prompt("fix vulns in nothing-known") is None
    finally:
        main.PROJECTS = orig
    print("project is recovered from the prompt without prefix confusion ✓")


def test_session_env_carries_run_marker():
    drive(R())
    assert SEEN_OPTIONS[-1].env.get(main.RUN_ENV) == _Ctx.task_id, SEEN_OPTIONS[-1].env
    print("the session env carries the run marker ✓")


def spawn_orphan(marker):
    """What `nohup cmd &` in the agent's Bash tool leaves behind: the shell exits, the
    sleep is reparented away from us, and only its environment says whose it is."""
    env = {**os.environ, main.RUN_ENV: marker}
    subprocess.run(["sh", "-c", "nohup sleep 300 >/dev/null 2>&1 &"], env=env, check=True)
    for _ in range(50):
        if main._run_pids(marker):
            return
        asyncio.run(asyncio.sleep(0.05))
    raise AssertionError("orphan never appeared")


def test_cancel_kills_leftovers():
    """Cancel must stop what the session left running, and only that."""
    orig_stop = main._stop_verify_containers
    # The host daemon may be running a live run's containers; never touch it from a test.
    main._stop_verify_containers = lambda: ["c1"]
    spawn_orphan("someone-else")
    up = FakeUpdater()

    async def hanging_query(prompt, options):
        spawn_orphan(options.env[main.RUN_ENV])
        yield AssistantMessage(content=[TextBlock(text="building…")], model="m")
        await asyncio.sleep(3600)

    async def scenario():
        t = asyncio.create_task(main.ClaudeCodeExecutor().execute(_Ctx(), _Q()))
        while _Ctx.task_id not in main._RUNNING or not main._run_pids(_Ctx.task_id):
            await asyncio.sleep(0.05)
        await main.ClaudeCodeExecutor().cancel(_Ctx(), _Q())
        try:
            await t
        except asyncio.CancelledError:
            pass

    orig_q, orig_u = main.query, main.TaskUpdater
    main.query, main.TaskUpdater = hanging_query, (lambda *a, **k: up)
    try:
        asyncio.run(scenario())
        assert not main._run_pids(_Ctx.task_id), "orphan survived the cancel"
        assert main._run_pids("someone-else"), "cancel killed a process that was not its own"
        assert up.cancelled and "1 leftover process" in up.cancelled, up.__dict__
        assert "1 verify container" in up.cancelled, up.__dict__
        s = latest_summary()
        assert s["run"]["status"] == "cancelled" and s["summary_missing"], s
        assert not main._RUN_LOCK.locked(), "lock still held after cancel"
    finally:
        main.query, main.TaskUpdater = orig_q, orig_u
        main._stop_verify_containers = orig_stop
        main._kill_run_leftovers("someone-else", grace=1)
    print("cancel kills the run's orphans, spares everyone else's, records it ✓")


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
    test_summary_carries_run_metrics()
    test_summary_contract_is_checked()
    test_failed_run_without_summary_still_recorded()
    test_project_from_prompt()
    test_session_env_carries_run_marker()
    test_cancel_kills_leftovers()

    print("ok")
