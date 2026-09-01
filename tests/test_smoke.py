"""Smoke test: agent card is served, and a message round-trips through Claude Code.

Run: uv run python test_smoke.py
The message test spawns a real Claude Code session (uses your subscription quota).
"""

import sys
import uuid

from fastapi.testclient import TestClient

import os
os.environ.setdefault("A2A_TOKEN", "test-token")

import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent / "app"))
from main import PUBLIC_URL, app

client = TestClient(app)


def test_agent_card():
    card = client.get("/.well-known/agent-card.json")
    assert card.status_code == 200, card.text
    body = card.json()
    assert body["name"] == "Claude Code"
    assert body["supportedInterfaces"][0]["url"]== PUBLIC_URL + "/"
    assert body["skills"][0]["id"] == "code"
    print("agent card OK:", body["supportedInterfaces"][0]["url"])


def test_message_send():
    resp = client.post(
        "/",
        headers={"A2A-Version": "1.0", "Authorization": "Bearer test-token"},  # absent => server assumes 0.3 and rejects
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": str(uuid.uuid4()),
                    "role": "ROLE_USER",
                    "parts": [{"text": "Reply with exactly: PONG. Do not use any tools."}],
                }
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" not in body, body["error"]
    text = "".join(p.get("text", "") for p in body["result"]["task"]["status"]["message"]["parts"])
    assert "PONG" in text, f"unexpected reply: {text!r}"
    print("message/send OK:", text.strip())


if __name__ == "__main__":
    test_agent_card()
    if "--card-only" not in sys.argv:
        test_message_send()
    print("ok")
