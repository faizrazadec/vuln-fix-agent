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
PROJECTS: dict[str, dict] = json.loads(_pf.read_text()) if _pf.exists() else {}
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


def _save_run_summary(task_id: str, text: str) -> pathlib.Path | None:
    """Persist the last parseable JSON summary block in the agent's final report."""
    for raw in reversed(_SUMMARY_RE.findall(text)):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or "project" not in data:
            continue
        data.setdefault("task_id", task_id)
        data.setdefault("finished_at", datetime.datetime.now().astimezone().isoformat())
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
            print(f"[task {context.task_id}] CANCELLED", flush=True)
            await updater.cancel(
                updater.new_agent_message([
                    Part(text="Cancelled mid-run. Any clone or image this run created is "
                              "still on disk; the next scheduled run reaps them.")
                ])
            )
            raise
        except Exception as exc:
            print(f"[executor] failed: {exc!r}", flush=True)
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

        saved = _save_run_summary(context.task_id, final_text)
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
