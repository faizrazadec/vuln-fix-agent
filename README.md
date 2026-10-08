# vuln-fix-agent

An [A2A (Agent2Agent)](https://a2a-protocol.org) server that exposes Claude Code as a callable
agent for fixing security vulnerabilities. A caller sends a JSON-RPC `SendMessage` such as
`fix vulns in <project>`. The server runs a real Claude Code session that pulls the project's
findings from Vanta, patches the repo, rebuilds and scans the image, runs the test suite, and
opens a signed pull request. It reports the result to Slack.

A cron job runs every registered project on weekday nights, one at a time.

## How it works

```
cron ──> scripts/VulnFixAgent ──> scripts/ask.sh ──JSON-RPC──> app/main.py (A2A server)
                                                                     │
                                                         claude_agent_sdk.query()
                                                                     │
                                                    skills/vuln-fix/SKILL.md pipeline:
                          Vanta findings → clone → triage → baseline → fix → rebuild +
                          trivy scan + tests → PR or Slack note → JSON run summary
```

- **`app/main.py`**: the whole server. It bridges the A2A task lifecycle to the Claude Agent
  SDK. A run can outlast any HTTP timeout, so callers send `returnImmediately` and poll
  `GetTask`. Only one session runs at a time; a second concurrent request is rejected.
- **`app/skills/vuln-fix/SKILL.md`**: the fix pipeline, written as instructions rather than
  orchestration code.
- **`app/bin/`**: CLI tools the agent calls:
  - `vanta-findings`: fetches open findings for a project
  - `vuln-ledger`: tracks CVEs already fixed or already reported
  - `slack-notify`: posts to Slack through an incoming webhook
  - `vuln-report`: a 30-day rollup of CVEs fixed, PRs opened, cost, and anything that needs
    a look
- **Repo allowlist**: the agent only works on repos listed in `app/projects.json`.

## Requirements

- Docker with Compose
- A Claude subscription. Billing goes through an interactive `claude` login, not
  `ANTHROPIC_API_KEY`.
- A GitHub SSH key for push and signing, plus a fine-grained PAT for opening PRs
- Vanta API credentials and a Slack incoming webhook
- [uv](https://docs.astral.sh/uv/) and Python 3.14+, only for running the tests

## Setup

```bash
cp .env.example .env                              # fill in tokens, keys, credentials
cp app/projects.example.json app/projects.json    # register your projects
./scripts/load-keys.sh                            # copy SSH keys into the ssh-keys volume
docker compose up -d --build
docker compose exec -it vuln-fix-agent claude     # one-time interactive login
```

The login is stored in the `claude-auth` volume. If that volume is wiped, log in again.

Register the SSH signing key on GitHub twice: once as an **Authentication** key and once as
a **Signing** key. Without the second, commits never show as Verified.

### Registering projects

Each key in `app/projects.json` is the name a caller uses. Fields:

| Field | Required | Meaning |
|---|---|---|
| `repo` | yes | SSH clone URL |
| `vanta.assets` | yes | Vanta vulnerable-asset names to pull findings for |
| `owners` | yes | `{name, slack_id}` entries tagged in every Slack message |
| `base_branch` | no | Fixed PR base. If omitted, the agent tries `staging`, then `develop`, then `main` |
| `base_branch_reason` | no | Why the base is pinned |

`projects.json` is gitignored and bind-mounted into the container, so after editing it you
only need `docker compose restart vuln-fix-agent`, not a rebuild. To check that every asset
name still resolves:

```bash
docker compose exec -u agent vuln-fix-agent vanta-findings --check-registry
```

## Usage

```bash
./scripts/ask.sh "fix vulns in <project>"     # one ad-hoc run; polls until done
./scripts/VulnFixAgent                        # every registered project, serially
./scripts/VulnFixAgent <project> [...]        # selected projects only
./scripts/VulnFixAgent --no-skip              # run even when Vanta reports nothing

docker compose exec -u agent vuln-fix-agent vuln-report   # last 30 days at a glance
docker compose logs -f vuln-fix-agent
```

To schedule the nightly batch, install [deploy/crontab](deploy/crontab) with `crontab -e`.
Batch output goes to `vuln-run.log`.

## Tests

Tests are plain `assert` scripts with no test framework:

```bash
uv sync
uv run python tests/test_protocol.py      # A2A protocol, executor stubbed
uv run python tests/test_registry.py      # repo allowlist blocks unregistered repos
uv run python tests/test_completion.py    # a stalled run is not reported as completed
uv run python tests/test_bin.py           # app/bin exit-code contract
uv run python tests/test_smoke.py --card-only   # agent card only, no quota
uv run python tests/test_smoke.py         # end-to-end: runs real Claude Code, uses quota
```

CI runs the four offline suites on every push. It never runs `test_smoke.py`.

## Security notes

- The server binds to `127.0.0.1:9999` only, and every route except the public agent card
  requires a bearer token (`A2A_TOKEN`). That bind and the token are the real security
  boundary.
- Claude Code runs with `permission_mode="bypassPermissions"`, so every caller is fully
  trusted. Before exposing this beyond a private network, switch to
  `permission_mode="default"` with a `can_use_tool` callback.
- The container mounts the host Docker socket so it can build and scan images. That gives
  it root-equivalent access to the host.
- The repo allowlist catches requests that name an unregistered repo. It is a guard rail,
  not a sandbox.
