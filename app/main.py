"""A2A server that runs Claude Code — billed to your Claude subscription, not API credits.

The `claude` CLI must be logged in (run `claude` once, interactively). No ANTHROPIC_API_KEY.
"""

import asyncio
import datetime
import json
import logging
import os
import pathlib
import re
import secrets
import signal
import subprocess
import time
import uuid

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.helpers.proto_helpers import new_task
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    Message,
    Part,
    Role,
    SecurityRequirement,
    SecurityScheme,
    TaskState,
)
from a2a.utils.constants import (
    AGENT_CARD_WELL_KNOWN_PATH,
    DEFAULT_RPC_URL,
    TransportProtocol,
)
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)
from fastapi import FastAPI
from fastapi.responses import JSONResponse

log = logging.getLogger("vuln-fix-agent")

PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:9999")
WORKSPACE = os.environ.get("WORKSPACE", os.getcwd())
STATE_DIR = pathlib.Path(os.environ.get("STATE_DIR", "/home/agent/state"))
TOKEN = os.environ.get("A2A_TOKEN")
MAX_TURNS = int(os.environ.get("MAX_TURNS", "200"))
# The pipeline lives in skills/vuln-fix/SKILL.md, which entrypoint.sh installs into
# $CLAUDE_CONFIG_DIR/skills. Only a pointer goes in the prompt — a bare string here would
# send --system-prompt and REPLACE Claude Code's own prompt; preset+append adds to it.
_pf = pathlib.Path(__file__).with_name("projects.json")
# Non-object entries are skipped: projects.example.json carries a "_comment" string,
# and copying it verbatim (as it tells you to) used to crash the import.
PROJECTS: dict[str, dict] = {
    k: v for k, v in (json.loads(_pf.read_text()) if _pf.exists() else {}).items()
    if isinstance(v, dict)
}
ALLOWED_URLS = {p["repo"] for p in PROJECTS.values() if p.get("repo")}
# Vanta filters live under each project's "vanta" key and are read by bin/vanta-findings
# straight from this registry, so a caller cannot widen them.

# ---------------------------------------------------------------------------
# Repo allowlist
#
# This catches a caller who *names* an unregistered repository in the request. It is a
# guard rail, not a sandbox: the session runs with permission_mode="bypassPermissions",
# so a determined prompt can still reach the network by other means. The real boundary is
# that the endpoint binds 127.0.0.1 and the bearer token gates every RPC. Keep both.
#
# Three spellings have to be recognised, because all three were demonstrably slipping
# through an earlier scheme-and-git@-only pattern:
#   git@github.com:owner/repo.git   https://github.com/owner/repo   HTTPS://GitHub.com/...
#   github.com/owner/repo           (no scheme)
#   clone owner/repo                (gh/git shorthand, no host at all)
# ---------------------------------------------------------------------------
_GIT_HOSTS = r"(?:github\.com|gitlab\.com|bitbucket\.org|codeberg\.org|ssh\.dev\.azure\.com)"
_END = r"(?=[\s,;'\")\]]|$)"

_URL_RE = re.compile(
    r"(?:git@[\w.-]+:[\w./-]+?(?:\.git)?"
    r"|(?:https?|ssh|git)://[\w.@:-]+/[\w./-]+?(?:\.git)?"
    r"|\b" + _GIT_HOSTS + r"[:/][\w.-]+/[\w./-]+?(?:\.git)?)" + _END,
    re.IGNORECASE,
)
# `gh repo clone owner/repo`, `git clone owner/repo`, `clone owner/repo`. Anchored on the
# verb so ordinary prose containing a slash ("app/main.py") is not mistaken for a repo.
_SHORTHAND_RE = re.compile(
    r"\bclone\s+(?:-{1,2}\S+\s+)*([\w.-]+/[\w.-]+?)(?:\.git)?" + _END,
    re.IGNORECASE,
)


def _norm_repo(url: str) -> str:
    """Reduce any spelling of a repo reference to `host/owner/repo`, lowercased.

    GitHub treats owner and repo case-insensitively, so comparing case-sensitively would
    refuse legitimate requests without blocking anything.
    """
    u = url.strip().rstrip("/")
    u = re.sub(r"^(?:https?|ssh|git)://", "", u, flags=re.IGNORECASE)
    u = re.sub(r"^git@", "", u, flags=re.IGNORECASE)
    u = u.replace(":", "/", 1) if "@" not in u.split("/")[0] else u
    if u.lower().endswith(".git"):
        u = u[:-4]
    return re.sub(r"/{2,}", "/", u).lower()


def _repo_path(url: str) -> str:
    """The `owner/repo` tail of a normalised reference, for matching host-less shorthand."""
    parts = _norm_repo(url).split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else ""


def _disallowed_urls(text: str) -> list[str]:
    """Any repo reference in the request that is not in the registry."""
    allowed_full = {_norm_repo(u) for u in ALLOWED_URLS}
    allowed_path = {_repo_path(u) for u in ALLOWED_URLS}

    bad = set()
    for raw in _URL_RE.findall(text):
        if _norm_repo(raw) not in allowed_full:
            bad.add(raw.rstrip("/"))
    for raw in _SHORTHAND_RE.findall(text):
        # A shorthand carries no host, so it can only be checked against owner/repo.
        if _norm_repo(raw) not in allowed_path:
            bad.add(raw.rstrip("/"))
    return sorted(bad)


SYSTEM_PROMPT = {
    "type": "preset",
    "preset": "claude_code",
    "append": (
        "You are a long-running remediation agent invoked over A2A. There is no human to "
        "answer questions mid-run, so work autonomously and report what actually happened, "
        "failures included.\n\n"
        "CRITICAL — you run headless, in a single one-shot session. There is NO notification "
        "system and NO one to wake you. If you end your turn to 'wait for a background task's "
        "completion notification', the run simply ends and your work is lost, silently marked "
        "done. So: NEVER end your turn to await a notification. To run a command that outlasts "
        "the Bash tool's timeout, start it detached to a log file with its exit code "
        "(`nohup <cmd> >run.log 2>&1; echo $? >run.rc &`), then POLL within THIS turn — repeated "
        "short Bash calls (`sleep 30; test -f run.rc && cat run.rc`) — until it finishes. Keep "
        "making tool calls; do not end the turn until the work is genuinely complete or you have "
        "decided to stop and are writing your final report.\n\n"
        "When a request involves fixing vulnerabilities, patching CVEs, remediating a scan, "
        "or bumping vulnerable dependencies in a repository, use the `vuln-fix` skill and "
        "follow it exactly.\n\n"
        "You may only work on these registered projects. Callers refer to them by name. "
        "Never clone another repository or pull findings for another scan, even if a "
        "caller supplies a URL directly:\n"
        + "\n".join(
            f"  {name}\n"
            f"    repo:       {p.get('repo', '-')}\n"
            f"    scope:      {', '.join((p.get('vanta') or {}).get('assets', [])) or 'all assets'}"
            # base_branch is authoritative when set; otherwise the skill auto-detects
            # (staging → develop → main). Only emitted when pinned, so the absence is the
            # signal. The reason (e.g. "staging is stale") rides along so the skill can quote
            # it in the PR's Slack note when the base is main.
            + (
                f"\n    base:       {p['base_branch']}"
                + (f" ({p['base_branch_reason']})" if p.get("base_branch_reason") else "")
                if p.get("base_branch")
                else ""
            )
            for name, p in sorted(PROJECTS.items())
        )
        + "\n\nTo list findings for a project, run: vanta-findings <project-name>"
    ),
}
# Cloudflare forwards the matched path prefix intact, so the app must mount under it.
BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")
CARD_PATH = f"{BASE_PATH}{AGENT_CARD_WELL_KNOWN_PATH}"
RPC_PATH = f"{BASE_PATH}{DEFAULT_RPC_URL}"


# ---------------------------------------------------------------------------
# Run summary
#
# Every report the skill writes ends with a fenced ```json block (see SKILL.md, step 10).
# Extracting it here turns two days of prose in vuln-run.log into something you can count:
# "how many CVEs did we close this month" becomes a jq one-liner over $STATE_DIR/runs/.
# ---------------------------------------------------------------------------
_SUMMARY_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)

# The contract SKILL.md step 10 asks for. A summary that breaks it is still saved — it is
# the only record of the run — but carries `validation_errors`, so vuln-report can say
# which numbers to distrust instead of silently counting them.
_SUMMARY_ENUMS = {
    "outcome": {"fixed", "nothing-to-do", "unfixable-only", "broke", "error"},
    "tests": {"green", "red", "pre-existing-red", "skipped", "incomplete"},
    "image_scan": {"green", "red", "skipped", "no-dockerfile"},
}
_SUMMARY_LISTS = ("cves_fixed", "cves_unfixable", "cves_already_fixed_in_base")
_SUMMARY_KEYS = (
    "project", "outcome", "findings_total", "findings_new", *_SUMMARY_LISTS, "pr_url",
    "base_branch", "tests", "image_scan", "slack_notified", "clone_kept", "notes",
)


def _validate_summary(data: dict) -> list[str]:
    errors = [f"missing key: {k}" for k in _SUMMARY_KEYS if k not in data]
    for key, allowed in _SUMMARY_ENUMS.items():
        if key in data and data[key] not in allowed:
            errors.append(f"{key}={data[key]!r} not one of {sorted(allowed)}")
    for key in _SUMMARY_LISTS:
        v = data.get(key)
        if key in data and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            errors.append(f"{key} must be a list of strings")
    for key in ("findings_total", "findings_new"):
        v = data.get(key)
        if key in data and not (isinstance(v, int) and not isinstance(v, bool) and v >= 0):
            errors.append(f"{key} must be a non-negative integer")
    # null = no message was needed; false = one was needed and did not get through.
    if "slack_notified" in data and data["slack_notified"] not in (True, False, None):
        errors.append("slack_notified must be true, false or null")
    if data.get("outcome") == "fixed":
        if not data.get("pr_url"):
            errors.append("outcome=fixed but pr_url is empty")
        if not data.get("cves_fixed"):
            errors.append("outcome=fixed but cves_fixed is empty")
    return errors


def _project_from_prompt(prompt: str) -> str | None:
    """The registry name a prompt refers to, for runs that died before writing a summary.

    Delimited on [\\w.-] so "Maritime" does not match inside "maritime-ai-hub"; the
    longest hit wins when one name is a prefix of another.
    """
    hits = [
        n for n in PROJECTS
        if re.search(rf"(?<![\w.-]){re.escape(n)}(?![\w.-])", prompt, re.IGNORECASE)
    ]
    return max(hits, key=len) if hits else None


def _run_metrics(result: ResultMessage | None, status: str, reason: str | None) -> dict:
    """How the session itself went — what the agent's own summary cannot know."""
    m: dict = {"status": status, "reason": reason}
    if result is not None:
        m.update(
            num_turns=result.num_turns,
            duration_ms=result.duration_ms,
            duration_api_ms=result.duration_api_ms,
            # API-equivalent: billing is the subscription, but this is what makes one
            # project's cost comparable to another's.
            total_cost_usd=result.total_cost_usd,
            usage=result.usage,
        )
    return m


def _save_run_summary(
    task_id: str,
    text: str,
    prompt: str = "",
    result: ResultMessage | None = None,
    status: str = "completed",
    reason: str | None = None,
) -> pathlib.Path | None:
    """Persist the last parseable JSON summary block in the agent's final report.

    Every run that reached Claude gets a file, even one that crashed, hit the turn cap or
    was cancelled before writing its block — otherwise runs/ only ever counted the runs
    that went well, and the failures were visible nowhere but the log.
    """
    data = None
    for raw in reversed(_SUMMARY_RE.findall(text)):
        try:
            candidate = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "project" in candidate:
            data = candidate
            break

    if data is None:
        data = {
            "project": _project_from_prompt(prompt) or "unknown",
            "outcome": "error",
            "summary_missing": True,
            "notes": reason or "the agent's report had no JSON summary block",
        }
    elif errors := _validate_summary(data):
        data["validation_errors"] = errors
        print(f"[task {task_id}] summary has {len(errors)} contract error(s): "
              + "; ".join(errors), flush=True)

    data.setdefault("task_id", task_id)
    data.setdefault("finished_at", datetime.datetime.now().astimezone().isoformat())
    data["run"] = _run_metrics(result, status, reason)
    # Registry keys are plain names, but never build a path out of unvalidated text.
    project = re.sub(r"[^\w.-]", "_", str(data["project"]))[:64] or "unknown"
    out = STATE_DIR / "runs" / f"{datetime.date.today():%Y%m%d}-{project}-{task_id[:8]}.json"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(out)
        return out
    except OSError as exc:
        log.warning("could not write run summary: %r", exc)
        return None


# ---------------------------------------------------------------------------
# Task store
#
# InMemoryTaskStore loses every task when the process stops, so a container restart mid-run
# left pollers looking at a task id the server no longer knew. The Claude session dies with
# the process either way — what this buys is that the record survives, so a poller sees a
# coherent task instead of a not-found, and finished runs stay queryable across restarts.
#
# It subclasses the SDK's internal impl deliberately: get()/list() carry owner-scoping and
# filter semantics worth inheriting rather than reimplementing. That couples us to a private
# name, so the import is guarded — if a future a2a-sdk moves it, we degrade to in-memory
# with a warning rather than failing to boot.
# ---------------------------------------------------------------------------
def _build_task_store():
    try:
        from a2a.server.tasks.inmemory_task_store import _InMemoryTaskStoreImpl
        from a2a.server.tasks.copying_task_store import CopyingTaskStoreAdapter
        from a2a.types.a2a_pb2 import Task as _TaskProto
    except Exception as exc:  # noqa: BLE001 — any import shape change lands here
        log.warning("persistent task store unavailable (%r) — falling back to in-memory", exc)
        return InMemoryTaskStore()

    class _FileBackedTaskStore(_InMemoryTaskStoreImpl):
        """In-memory semantics, mirrored to $STATE_DIR/tasks as protobuf."""

        def __init__(self, directory: pathlib.Path):
            super().__init__()
            self._dir = directory
            self._restore()

        def _path(self, owner: str, task_id: str) -> pathlib.Path:
            safe_owner = re.sub(r"[^\w.-]", "_", owner)[:64] or "_"
            safe_id = re.sub(r"[^\w.-]", "_", task_id)[:128]
            return self._dir / safe_owner / f"{safe_id}.pb"

        def _restore(self) -> None:
            if not self._dir.is_dir():
                return
            restored = 0
            for f in self._dir.glob("*/*.pb"):
                try:
                    task = _TaskProto()
                    task.ParseFromString(f.read_bytes())
                    self.tasks.setdefault(f.parent.name, {})[task.id] = task
                    restored += 1
                except Exception as exc:  # noqa: BLE001 — a corrupt file must not block boot
                    log.warning("skipping unreadable task file %s: %r", f, exc)
            if restored:
                print(f"[tasks] restored {restored} task(s) from {self._dir}", flush=True)

        async def save(self, task, context) -> None:
            await super().save(task, context)
            owner = self.owner_resolver(context)
            p = self._path(owner, task.id)
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_suffix(".tmp")
                tmp.write_bytes(task.SerializeToString())
                tmp.replace(p)
            except OSError as exc:
                # Durability is a nice-to-have; never fail a live run over it.
                log.warning("could not persist task %s: %r", task.id, exc)

        async def delete(self, task_id: str, context) -> None:
            await super().delete(task_id, context)
            self._path(self.owner_resolver(context), task_id).unlink(missing_ok=True)

    return CopyingTaskStoreAdapter(_FileBackedTaskStore(STATE_DIR / "tasks"))


# One Claude Code session at a time. Each run builds Docker images and runs a test suite on
# a memory-constrained VM shared with other work; two at once is how the OOM killer gets
# involved. The batch runner is serial, but nothing stopped a second caller from overlapping
# with it — and ask.sh giving up on polling used to do exactly that.
_RUN_LOCK = asyncio.Lock()
# task_id -> the asyncio task running it, so cancel() has something to interrupt.
_RUNNING: dict[str, asyncio.Task] = {}


# ---------------------------------------------------------------------------
# Cancel cleanup
#
# Cancelling a run stops the Claude CLI, but not what it left running. The system prompt
# tells the agent to `nohup` anything that outlasts the Bash timeout — test suites, image
# builds — which is exactly what makes those survive the CLI's death as orphans. And
# `_RUN_LOCK` is released the moment the cancel lands, so the next run starts on top of
# them: the OOM scenario the lock exists to prevent.
#
# Orphans lose their parentage, so they are found by an env marker instead: every process
# the session spawns inherits RUN_ENV=<task_id> (nohup and setsid keep the environment).
# That is precise — it cannot hit ssh-agent, the server, or anything a human started.
# ---------------------------------------------------------------------------
RUN_ENV = "VULN_FIX_RUN"


def _run_pids(task_id: str) -> list[int]:
    """Live processes carrying this run's marker. Zombies have an empty environ, so a
    killed child waiting to be reaped drops out of this list too."""
    marker = f"{RUN_ENV}={task_id}".encode()
    me = os.getpid()
    found = []
    for d in pathlib.Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) == me:
            continue
        try:
            if marker in (d / "environ").read_bytes().split(b"\0"):
                found.append(int(d.name))
        except OSError:  # gone already, or not ours to read
            continue
    return found


def _kill_run_leftovers(task_id: str, grace: float = 5.0) -> int:
    """SIGTERM this run's processes, SIGKILL whatever ignores it. Returns how many."""
    pids = _run_pids(task_id)
    total = len(pids)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        deadline = time.monotonic() + grace
        while pids and time.monotonic() < deadline:
            time.sleep(0.2)
            pids = _run_pids(task_id)
        if not pids:
            break
    return total


def _stop_verify_containers() -> list[str]:
    """Remove running containers from this run's verify images.

    `docker run -d` containers belong to the host daemon, so no signal reaches them. Only
    one run exists at a time, so every container in the namespace SKILL.md mandates is
    this run's. Images and clones are left for the batch runner's reap, as before.
    """
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.ID}} {{.Image}}"],
            capture_output=True, text=True, timeout=30, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    ids = [
        cid for cid, _, image in (line.partition(" ") for line in out.splitlines())
        if image.startswith("vuln-fix-agent-verify/")
    ]
    if ids:
        try:
            subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError):
            return []
    return ids


class ClaudeCodeExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # Task lifecycle rather than a bare reply message: a vuln-fix run takes far
        # longer than any HTTP timeout, so callers send return_immediately and poll GetTask.
        if context.current_task is None:
            # The Task object must exist on the queue before any status update.
            await event_queue.enqueue_event(
                new_task(context.task_id, context.context_id, TaskState.TASK_STATE_SUBMITTED)
            )
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()

        prompt = context.get_user_input()
        # Server-side log line, captured by `docker logs`, so the run has a durable record
        # even if the client-side poller dies mid-run (a dead poller silently freezes its
        # own log while the task keeps running here).
        print(f"[task {context.task_id}] START: {prompt[:120]}", flush=True)
        rogue = _disallowed_urls(prompt)
        if rogue:
            await updater.failed(
                updater.new_agent_message([
                    Part(text=(
                        "Refused: these repositories are not in the project registry: "
                        + ", ".join(rogue)
                        + ". Registered projects: "
                        + (", ".join(sorted(PROJECTS)) or "(none)")
                    ))
                ])
            )
            return

        # Checked rather than awaited: a caller polling GetTask wants to know its request
        # was turned away, not to have it silently queue behind a 20-minute run. Nothing
        # awaits between the check and the acquire, so this cannot race.
        if _RUN_LOCK.locked():
            busy = ", ".join(_RUNNING) or "another task"
            print(f"[task {context.task_id}] REJECTED: busy with {busy}", flush=True)
            await updater.failed(
                updater.new_agent_message([
                    Part(text=(
                        f"Busy: a run is already in progress ({busy}). This agent runs one "
                        "session at a time — it builds images and runs test suites, and two "
                        "at once exhausts the host. Retry when the current run finishes."
                    ))
                ])
            )
            return

        async with _RUN_LOCK:
            _RUNNING[context.task_id] = asyncio.current_task()
            try:
                await self._run(context, updater, prompt)
            finally:
                _RUNNING.pop(context.task_id, None)

    async def _run(self, context: RequestContext, updater: TaskUpdater, prompt: str) -> None:
        options = ClaudeAgentOptions(
            cwd=WORKSPACE,
            system_prompt=SYSTEM_PROMPT,
            skills=["vuln-fix"],
            # ponytail: trusts every caller; swap for permission_mode="default" + a
            # can_use_tool callback if you expose this beyond your own network.
            permission_mode="bypassPermissions",
            max_turns=MAX_TURNS,
            # Inherited by everything the session spawns, so a cancel can find it all.
            env={RUN_ENV: context.task_id},
            stderr=lambda line: print(f"[claude stderr] {line}", flush=True),
        )
        chunks: list[str] = []
        result: ResultMessage | None = None
        try:
            async for msg in query(prompt=prompt, options=options):
                if isinstance(msg, ResultMessage):
                    result = msg
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            chunks.append(block.text)
                            # Stream progress into task history so long runs are observable.
                            await updater.update_status(
                                TaskState.TASK_STATE_WORKING,
                                message=updater.new_agent_message([Part(text=block.text)]),
                            )
        except asyncio.CancelledError:
            killed = await asyncio.to_thread(_kill_run_leftovers, context.task_id)
            stopped = await asyncio.to_thread(_stop_verify_containers)
            print(f"[task {context.task_id}] CANCELLED — killed {killed} leftover "
                  f"process(es), removed {len(stopped)} verify container(s)", flush=True)
            _save_run_summary(context.task_id, "\n".join(chunks), prompt,
                              status="cancelled", reason="cancelled mid-run")
            await updater.cancel(
                updater.new_agent_message([
                    Part(text=f"Cancelled mid-run. Stopped {killed} leftover process(es) and "
                              f"{len(stopped)} verify container(s). Any clone or image this "
                              "run created is still on disk; the next scheduled run reaps them.")
                ])
            )
            raise
        except Exception as exc:
            print(f"[executor] failed: {exc!r}", flush=True)
            _save_run_summary(context.task_id, "\n".join(chunks), prompt,
                              status="failed", reason=f"executor raised {exc!r}"[:300])
            await updater.failed(updater.new_agent_message([Part(text=str(exc))]))
            return

        # Iterator-exhausted is NOT the same as "workflow finished". The SDK reports how
        # the run actually ended in the ResultMessage; without checking it, a run that
        # errored, hit the turn cap, or stopped mid-work gets marked COMPLETED carrying a
        # half-written progress note — which is exactly what let a stalled run look done.
        reason = None
        if result is None:
            reason = "run ended without a ResultMessage (stream closed unexpectedly)"
        elif result.is_error:
            reason = f"run errored (subtype={result.subtype}"
            if result.errors:
                reason += f", {'; '.join(result.errors)[:300]}"
            reason += ")"
        elif result.subtype and result.subtype != "success":
            reason = f"run did not finish cleanly (subtype={result.subtype})"
        elif result.num_turns >= MAX_TURNS:
            reason = f"hit the turn cap ({MAX_TURNS}) — likely stopped mid-work"

        # Prefer the SDK's final result text over joined progress chunks.
        final_text = (result.result if result and result.result else "\n".join(chunks)) or "(no output)"

        saved = _save_run_summary(context.task_id, final_text, prompt, result,
                                  status="failed" if reason else "completed", reason=reason)
        if saved:
            print(f"[task {context.task_id}] summary -> {saved}", flush=True)

        if reason:
            print(f"[task {context.task_id}] FAILED: {reason}", flush=True)
            await updater.failed(
                updater.new_agent_message([Part(text=f"INCOMPLETE — {reason}\n\n{final_text}")])
            )
        else:
            print(f"[task {context.task_id}] COMPLETED", flush=True)
            await updater.complete(updater.new_agent_message([Part(text=final_text)]))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Interrupt a running session. Without this a runaway run could only be stopped
        by restarting the container, which took every other task's state with it."""
        running = _RUNNING.get(context.task_id)
        if running is None:
            updater = TaskUpdater(event_queue, context.task_id, context.context_id)
            await updater.failed(
                updater.new_agent_message([
                    Part(text="Nothing to cancel: that task is not running on this process.")
                ])
            )
            return
        print(f"[task {context.task_id}] cancel requested", flush=True)
        # execute() catches CancelledError and reports TASK_STATE_CANCELLED itself.
        running.cancel()


agent_card = AgentCard(
    name="Vuln Fix Agent",
    description=(
        "Remediates Vanta-reported vulnerabilities in a fixed registry of repositories: "
        "bumps the vulnerable dependency, regenerates the lockfile, verifies tests and the "
        "rebuilt image, and opens a signed PR. Runs one session at a time."
    ),
    version="1.1.0",
    capabilities=AgentCapabilities(streaming=False),
    security_schemes={
        "bearer": SecurityScheme(
            http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="bearer")
        )
    },
    security_requirements=[SecurityRequirement(schemes={"bearer": {}})],
    default_input_modes=["text/plain"],
    default_output_modes=["text/plain"],
    supported_interfaces=[
        AgentInterface(
            url=f"{PUBLIC_URL}{RPC_PATH}",
            protocol_binding=TransportProtocol.JSONRPC,
            protocol_version="1.0",
        )
    ],
    skills=[
        AgentSkill(
            id="vuln-fix",
            name="Fix vulnerabilities",
            description=(
                "Remediate open Vanta findings for one registered project and open a PR. "
                "Ask by project name, e.g. 'fix vulns in "
                + (sorted(PROJECTS)[0] if PROJECTS else "<project>")
                + "'. Registered projects: "
                + (", ".join(sorted(PROJECTS)) or "(none)")
            ),
            tags=["security", "vulnerabilities", "cve", "dependencies", "pull-request"],
        )
    ],
)

handler = DefaultRequestHandler(
    agent_executor=ClaudeCodeExecutor(),
    task_store=_build_task_store(),
    agent_card=agent_card,
)

app = FastAPI()


@app.middleware("http")
async def require_token(request, call_next):
    """Gate everything except the agent card, which stays public for discovery."""
    if request.url.path != CARD_PATH:
        if not TOKEN:
            return JSONResponse({"error": "A2A_TOKEN is not set"}, status_code=503)
        if not secrets.compare_digest(
            request.headers.get("authorization", ""), f"Bearer {TOKEN}"
        ):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


add_a2a_routes_to_fastapi(
    app,
    agent_card_routes=create_agent_card_routes(
        agent_card, card_url=CARD_PATH
    ),
    jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url=RPC_PATH),
)
