"""A2A server that runs Claude Code — billed to your Claude subscription, not API credits.

The `claude` CLI must be logged in (run `claude` once, interactively). No ANTHROPIC_API_KEY.
"""

import os
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
from a2a.server.tasks import InMemoryTaskStore
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
)
from a2a.utils.constants import (
    AGENT_CARD_WELL_KNOWN_PATH,
    DEFAULT_RPC_URL,
    TransportProtocol,
)
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query
from fastapi import FastAPI
from fastapi.responses import JSONResponse

PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:9999")
WORKSPACE = os.environ.get("WORKSPACE", os.getcwd())
TOKEN = os.environ.get("A2A_TOKEN")
# Cloudflare forwards the matched path prefix intact, so the app must mount under it.
BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")
CARD_PATH = f"{BASE_PATH}{AGENT_CARD_WELL_KNOWN_PATH}"
RPC_PATH = f"{BASE_PATH}{DEFAULT_RPC_URL}"


class ClaudeCodeExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        options = ClaudeAgentOptions(
            cwd=WORKSPACE,
            # ponytail: trusts every caller; swap for permission_mode="default" + a
            # can_use_tool callback if you expose this beyond your own network.
            permission_mode="bypassPermissions",
            max_turns=30,
            stderr=lambda line: print(f"[claude stderr] {line}", flush=True),
        )
        chunks = []
        async for msg in query(prompt=context.get_user_input(), options=options):
            if isinstance(msg, AssistantMessage):
                chunks += [b.text for b in msg.content if isinstance(b, TextBlock)]

        await event_queue.enqueue_event(
            Message(
                message_id=str(uuid.uuid4()),
                context_id=context.context_id,
                task_id=context.task_id,
                role=Role.ROLE_AGENT,
                parts=[Part(text="\n".join(chunks))],
            )
        )

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
