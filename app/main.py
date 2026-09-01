"""A2A server that runs Claude Code — billed to your Claude subscription, not API credits.

The `claude` CLI must be logged in (run `claude` once, interactively). No ANTHROPIC_API_KEY.
"""

import json
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

PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:9999")
WORKSPACE = os.environ.get("WORKSPACE", os.getcwd())
TOKEN = os.environ.get("A2A_TOKEN")
MAX_TURNS = int(os.environ.get("MAX_TURNS", "200"))
# The pipeline lives in skills/vuln-fix/SKILL.md, which entrypoint.sh installs into
# $CLAUDE_CONFIG_DIR/skills. Only a pointer goes in the prompt — a bare string here would
# send --system-prompt and REPLACE Claude Code's own prompt; preset+append adds to it.
# Only these repos may be cloned. Enforced here rather than in the prompt: the endpoint
# is reachable from the internet, and a prompt rule is advice, not a boundary.
_pf = pathlib.Path(__file__).with_name("projects.json")
PROJECTS: dict[str, dict] = json.loads(_pf.read_text()) if _pf.exists() else {}
ALLOWED_URLS = {p["repo"] for p in PROJECTS.values() if p.get("repo")}
# Vanta filters live under each project's "vanta" key and are read by bin/vanta-findings
# straight from this registry, so a caller cannot widen them.

# git@host:path.git, https://host/path(.git), ssh://…
_URL_RE = re.compile(
    r"(?:git@[\w.-]+:[\w./-]+?(?:\.git)?|(?:https?|ssh|git)://[\w.@:-]+/[\w./-]+?(?:\.git)?)(?=[\s,;'\")\]]|$)"
)


def _disallowed_urls(text: str) -> list[str]:
    """Any repo URL in the request that is not in the registry."""
    found = {u.rstrip("/") for u in _URL_RE.findall(text)}
    allowed = {u.rstrip("/") for u in ALLOWED_URLS}
    # Compare ignoring a trailing .git so both spellings of the same repo match.
    norm = lambda u: u[:-4] if u.endswith(".git") else u  # noqa: E731
    allowed_n = {norm(u) for u in allowed}
    return sorted(u for u in found if norm(u) not in allowed_n)


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
            for name, p in sorted(PROJECTS.items())
        )
        + "\n\nTo list findings for a project, run: vanta-findings <project-name>"
    ),
}
# Cloudflare forwards the matched path prefix intact, so the app must mount under it.
BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")
CARD_PATH = f"{BASE_PATH}{AGENT_CARD_WELL_KNOWN_PATH}"
RPC_PATH = f"{BASE_PATH}{DEFAULT_RPC_URL}"


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

        if reason:
            print(f"[executor] incomplete: {reason}", flush=True)
            await updater.failed(
                updater.new_agent_message([Part(text=f"INCOMPLETE — {reason}\n\n{final_text}")])
            )
        else:
            await updater.complete(updater.new_agent_message([Part(text=final_text)]))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("cancel not supported")


agent_card = AgentCard(
    name="Claude Code",
    description="Runs a Claude Code session in a project workspace and returns the result.",
    version="1.0.0",
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
            id="code",
            name="Code",
            description="Read, write, and run code in the configured workspace.",
            tags=["code", "files", "shell"],
        )
    ],
)

handler = DefaultRequestHandler(
    agent_executor=ClaudeCodeExecutor(),
    task_store=InMemoryTaskStore(),
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
