"""Verify the A2A protocol layer without spawning Claude Code.

Stubs the executor so this runs free and offline. test_smoke.py covers the real thing.
Run: uv run python test_protocol.py
"""

import uuid

from a2a.types import Message, Part, Role
from fastapi.testclient import TestClient

import os
os.environ.setdefault("A2A_TOKEN", "test-token")
os.environ.setdefault("BASE_PATH", "/a2a")  # exercise the prefixed mount

import main

async def _stub(self, ctx, q):
    await q.enqueue_event(
        Message(
            message_id=str(uuid.uuid4()),
            context_id=ctx.context_id,
            task_id=ctx.task_id,
            role=Role.ROLE_AGENT,
            parts=[Part(text="STUB:" + ctx.get_user_input())],
        )
    )


main.ClaudeCodeExecutor.execute = _stub

client = TestClient(main.app)

card = client.get(main.CARD_PATH).json()
assert card["name"] == "Claude Code", card
assert card["supportedInterfaces"][0]["url"] == main.PUBLIC_URL + main.RPC_PATH, card
print("card OK")

resp = client.post(
    main.RPC_PATH,
    headers={"A2A-Version": "1.0", "Authorization": "Bearer test-token"},
    json={
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": str(uuid.uuid4()),
                "role": "ROLE_USER",
                "parts": [{"text": "hello"}],
            }
        },
    },
)
body = resp.json()
assert "error" not in body, body["error"]

text = "".join(p.get("text", "") for p in body["result"]["message"]["parts"])
assert text == "STUB:hello", body
print("SendMessage OK:", text)

# The auth gate must actually reject, not just let the good token through.
bad = {"jsonrpc": "2.0", "id": "2", "method": "SendMessage", "params": {}}
assert client.post(main.RPC_PATH, json=bad).status_code == 401, "no token was accepted"
assert (
    client.post(
        main.RPC_PATH, headers={"Authorization": "Bearer wrong"}, json=bad
    ).status_code
    == 401
), "wrong token was accepted"
assert client.get(main.CARD_PATH).status_code == 200, "card must stay public"
print("auth OK: 401 without token, 401 on wrong token, card public")
print("ok")
