"""Verify the A2A protocol layer without spawning Claude Code.

Stubs the executor so this runs free and offline. test_smoke.py covers the real thing.
Run: uv run python test_protocol.py
"""

import asyncio
import os
import threading
import time
import uuid

os.environ.setdefault("A2A_TOKEN", "test-token")
os.environ.setdefault("BASE_PATH", "/a2a")  # exercise the prefixed mount

from a2a.helpers.proto_helpers import new_task  # noqa: E402
from a2a.server.tasks import TaskUpdater  # noqa: E402
from a2a.types import Part, TaskState  # noqa: E402
import httpx  # noqa: E402
import uvicorn  # noqa: E402

import main  # noqa: E402

AUTH = {"A2A-Version": "1.0", "Authorization": "Bearer test-token"}
BASE = "http://127.0.0.1:8931"


async def _stub(self, ctx, q):
    """Mimics the real executor's lifecycle, but slow enough to observe as a Task."""
    if ctx.current_task is None:
        await q.enqueue_event(
            new_task(ctx.task_id, ctx.context_id, TaskState.TASK_STATE_SUBMITTED)
        )
    updater = TaskUpdater(q, ctx.task_id, ctx.context_id)
    await updater.start_work()
    await asyncio.sleep(1.5)
    await updater.complete(
        updater.new_agent_message([Part(text="STUB:" + ctx.get_user_input())])
    )


main.ClaudeCodeExecutor.execute = _stub

# A real server, not TestClient: TestClient tears the background task down when the
# request returns, so a task started with returnImmediately never finishes there.
_server = uvicorn.Server(
    uvicorn.Config(main.app, host="127.0.0.1", port=8931, log_level="warning")
)
threading.Thread(target=_server.run, daemon=True).start()
for _ in range(100):
    try:
        httpx.get(BASE + main.CARD_PATH, timeout=1)
        break
    except Exception:
        time.sleep(0.1)
else:
    raise RuntimeError("server did not start")

client = httpx.Client(base_url=BASE, timeout=30)


def send(text, return_immediately):
    return client.post(
        main.RPC_PATH,
        headers=AUTH,
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": str(uuid.uuid4()),
                    "role": "ROLE_USER",
                    "parts": [{"text": text}],
                },
                "configuration": {"returnImmediately": return_immediately},
            },
        },
    ).json()


def test_card():
    card = client.get(main.CARD_PATH).json()
    assert card["name"] == "Claude Code", card
    assert card["supportedInterfaces"][0]["url"] == main.PUBLIC_URL + main.RPC_PATH, card
    print("card OK")


def test_blocking():
    body = send("hello", False)
    assert "error" not in body, body["error"]
    task = body["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED", task["status"]
    print("blocking OK: completed inline")


def test_return_immediately():
    """The whole point: a long run must hand back a task id before it finishes."""
    body = send("slow work", True)
    assert "error" not in body, body["error"]
    task = body["result"]["task"]
    state = task["status"]["state"]
    assert state in ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"), (
        f"returned {state} — it blocked instead of returning immediately"
    )
    task_id = task["id"]
    print(f"return_immediately OK: {state}, id={task_id[:8]}")

    for _ in range(40):
        got = client.post(
            main.RPC_PATH,
            headers=AUTH,
            json={"jsonrpc": "2.0", "id": "2", "method": "GetTask",
                  "params": {"id": task_id}},
        ).json()
        assert "error" not in got, got["error"]
        polled = got["result"]  # GetTask returns the task directly, unwrapped
        if polled["status"]["state"] == "TASK_STATE_COMPLETED":
            text = "".join(p.get("text", "") for p in polled["status"]["message"]["parts"])
            assert text == "STUB:slow work", polled
            print("GetTask OK: polled to completion, result intact")
            return
        time.sleep(0.25)
    raise AssertionError(f"task never completed: {polled['status']}")


def test_auth():
    bad = {"jsonrpc": "2.0", "id": "3", "method": "SendMessage", "params": {}}
    assert client.post(main.RPC_PATH, json=bad).status_code == 401, "no token accepted"
    assert client.post(
        main.RPC_PATH, headers={"Authorization": "Bearer wrong"}, json=bad
    ).status_code == 401, "wrong token accepted"
    assert client.get(main.CARD_PATH).status_code == 200, "card must stay public"
    print("auth OK: 401 without token, 401 on wrong token, card public")


if __name__ == "__main__":
    test_card()
    test_blocking()
    test_return_immediately()
    test_auth()
    _server.should_exit = True
    print("ok")
