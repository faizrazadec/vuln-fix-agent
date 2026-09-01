# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An A2A (Agent2Agent) protocol server that exposes Claude Code as a callable agent.
A remote caller sends a JSON-RPC `SendMessage`; the server runs a real Claude Code
session in a workspace and returns the result.

Billing runs through an interactive **Claude subscription login**, not `ANTHROPIC_API_KEY`.
The `claude` CLI must be logged in once inside the container; credentials persist in the
`claude-auth` volume. If that volume is wiped, the server cannot run until you log in again.

## Layout

```
app/       what runs inside the container: main.py, entrypoint.sh, bin/ (agent
           CLI tools: vanta-findings, slack-notify, vuln-ledger), skills/vuln-fix,
           and projects.json (the registry — gitignored; projects.example.json is the template)
scripts/   host-side ops: ask.sh (fire one run), VulnFixAgent (the scheduled batch runner),
           load-keys.sh (load SSH keys into the volume)
deploy/    the launchd plist for the weekday schedule
tests/     assert-based scripts (add app/ to sys.path, then import main)
Dockerfile, compose.yml, pyproject.toml, uv.lock, .env  — at the root
```

## Commands

```bash
uv sync                                # install deps (Python >=3.14)

uv run python tests/test_protocol.py         # protocol tests — offline, free, stubs the executor
uv run python tests/test_smoke.py            # end-to-end — spawns real Claude Code, BURNS QUOTA
uv run python tests/test_smoke.py --card-only  # agent-card assertions only, no quota

docker compose up -d --build
docker compose logs -f a2claude-api
docker compose exec -it a2claude-api claude   # the one-time interactive login
```

Tests are plain scripts with `assert` and an `__main__` block — no pytest, no fixtures.
Each test file adds `app/` to `sys.path`, then imports `main`. Importing `test_protocol` has
side effects by design — it stubs `ClaudeCodeExecutor.execute` and starts a real uvicorn
server on port 8931 at module level.

## Architecture

**`app/main.py`** is the whole server. `ClaudeCodeExecutor` implements the a2a-sdk
`AgentExecutor` interface and bridges A2A's task lifecycle to `claude_agent_sdk.query()`.

Three decisions there are load-bearing and easy to break:

- **Task lifecycle, not a bare reply message.** A run can outlast any HTTP timeout, so the
  executor enqueues a Task, then streams each `TextBlock` as a `TASK_STATE_WORKING` status
  update. Callers send `returnImmediately` and poll `GetTask`. The Task object must exist on
  the queue *before* any status update or the update is dropped.
- **`BASE_PATH`.** Cloudflare forwards the matched path prefix intact, so the agent card and
  the JSON-RPC route must both mount under it — hence `CARD_PATH` / `RPC_PATH` rather than
  literal paths. Always reference `main.CARD_PATH` / `main.RPC_PATH`, never hardcode.
  `test_protocol.py` sets `BASE_PATH=/a2a` specifically to keep this honest.
- **Auth middleware.** Everything is bearer-gated except the agent card, which stays public
  so callers can discover the endpoint. Comparison uses `secrets.compare_digest`.

**`app/skills/vuln-fix/SKILL.md`** is the vulnerability-fix pipeline expressed as
*instructions*, not orchestration code — Claude Code already has the agent loop. It is
installed as a skill (`skills=["vuln-fix"]`) and the system prompt only points at it; the
prompt is appended to Claude Code's own via `preset` + `append`, never replacing it.

**`app/entrypoint.sh`** builds git/SSH identity at container start, before exec'ing the server.
Keys are bind-mounted read-only at `/ssh-keys` and **copied** to `~/.ssh` — ssh rejects keys
carrying the host's uid and permissions. Commits are SSH-signed; the signing key must be
registered on GitHub *twice*, as an Auth key and separately as a Signing key, or the Verified
badge never appears.

**Container constraints** (`Dockerfile`) — both were bugs before they were comments:

- Runs as unprivileged user `agent`, because Claude Code refuses
  `--dangerously-skip-permissions` (what `permission_mode="bypassPermissions"` sends) as root.
- `CLAUDE_CONFIG_DIR` is pointed at the mounted volume. By default `.claude.json` lands beside
  `$HOME/.claude` and therefore *outside* the volume, so onboarding state was lost every start.

The image also carries `gh`, `docker-ce-cli`, and `trivy` — the vuln-fix workflow shells out
to all three. `compose.yml` pairs the API with a `cloudflared` tunnel; `PUBLIC_URL` is baked
into the agent card and must match the hostname routed to that tunnel.

`GH_TOKEN` is separate from the SSH keys on purpose: SSH can push and sign, but opening a PR
needs the REST API.

## Gotchas

- `permission_mode="bypassPermissions"` trusts every caller. It is marked with a `ponytail:`
  comment in `main.py`. Anything beyond a private network needs `permission_mode="default"`
  plus a `can_use_tool` callback.
- `InMemoryTaskStore` means task state dies with the process. A restart mid-run loses the task.
- `cancel()` raises `NotImplementedError`.
