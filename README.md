# vuln-fix-agent

An [A2A (Agent2Agent)](https://a2a-protocol.org) server that exposes Claude Code as a callable
agent for fixing security vulnerabilities. A caller sends a JSON-RPC `SendMessage` such as
`fix vulns in <project>`. The server runs a real Claude Code session that pulls the project's
findings from Vanta, patches the repo, rebuilds and scans the image, runs the test suite, and
opens a signed pull request. It waits for the PR's GitHub checks, fixes what fails, and only
then reports the result to Slack. Findings with no reachable fix
are deactivated in Vanta as time-boxed exceptions, and every exception and every
Critical/High fix PR is tracked in a Linear ticket assigned to the project owner.

A cron job runs every registered project on weekday nights, one at a time.

## How it works

```
cron ──> scripts/VulnFixAgent ──> scripts/ask.sh ──JSON-RPC──> app/main.py (A2A server)
                                                                     │
                                                         claude_agent_sdk.query()
                                                                     │
                                                    skills/vuln-fix/SKILL.md pipeline:
                          Vanta findings → clone → triage → baseline → fix → rebuild +
                          trivy scan + tests → PR + CI checks or Vanta exception → Linear ticket
                          → Slack note → JSON run summary
```

- **`app/main.py`**: the whole server. It bridges the A2A task lifecycle to the Claude Agent
  SDK. A run can outlast any HTTP timeout, so callers send `returnImmediately` and poll
  `GetTask`. Only one session runs at a time; a second concurrent request is rejected.
- **`app/skills/vuln-fix/SKILL.md`**: the fix pipeline, written as instructions rather than
  orchestration code.
- **`app/bin/`**: CLI tools the agent calls:
  - `vanta-findings`: fetches open findings for a project, and deactivates one
    (`deactivate <CVE> --reason …`)
  - `vuln-ledger`: tracks CVEs already fixed or already reported, and the ticket each is
    tracked under
  - `linear-ticket`: creates, links, extends and closes the compliance tickets
  - `slack-notify`: posts to Slack through an incoming webhook
  - `vuln-report`: a 30-day rollup of CVEs fixed, PRs opened, cost, and anything that needs
    a look
- **Repo allowlist**: the agent only works on repos listed in `app/projects.json`.

## Requirements

- Docker with Compose
- A Claude subscription. Billing goes through an interactive `claude` login, not
  `ANTHROPIC_API_KEY`.
- A GitHub SSH key for push and signing, plus a fine-grained PAT for opening PRs
- Vanta API credentials with **write** scope (the agent deactivates findings), and a Slack
  incoming webhook
- A Linear API key, plus the team and project the compliance tickets go to
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
| `owners` | yes | `{name, slack_id, linear_id}` entries. Everyone is tagged in every Slack message; the first owner is assigned the Linear tickets |
| `base_branch` | no | Fixed PR base. If omitted, the agent tries `staging`, then `develop`, then `main` |
| `base_branch_reason` | no | Why the base is pinned |

`projects.json` is gitignored and bind-mounted into the container, so after editing it you
only need `docker compose restart vuln-fix-agent`, not a rebuild. To check that every asset
name still resolves:

```bash
docker compose exec -u agent vuln-fix-agent vanta-findings --check-registry
```

### Compliance tickets

Two ISO controls drive this: every risk-relevant Critical/High vulnerability has a ticket
assigned to the system owner, and every exception has a remediation plan and review date.

| Situation | Vanta | Linear |
|---|---|---|
| No fix exists | Deactivated for `EXCEPTION_REVIEW_DAYS` (30); lifts early when Vanta sees a fix | Backlog ticket per package: CVEs, reason, risk treatment, due on the review date |
| Vanta lists a fix that cannot be applied (vendored binary, net regression) | Deactivated with no end date, since Vanta would otherwise keep re-raising it | Same, and the ticket's due date is the only review trigger |
| Fix PR touches a Critical/High finding | Nothing to do | One ticket linked to the PR, or the existing exception ticket reused |

`scripts/VulnFixAgent` closes a ticket when its PR merges (`linear-ticket sync`), because
Linear's GitHub integration is not assumed. It also starts a run when an exception's
review date arrives, even if Vanta shows no open findings. A deactivated finding does not
appear in `vanta-findings`, so the review date is what brings it back. A review that finds
no fix extends the deactivation and the due date by another 30 days.
`linear-ticket` only edits issues in `LINEAR_PROJECT_ID`.

Deactivating and ticketing are separate steps, so an interrupted run can leave an
exception with no ticket, and exceptions made by hand in Vanta never had one.
`vanta-findings <project> --json --untracked` lists deactivated findings that no open
ticket covers. The runner starts a session when that list is not empty, and the agent
files the missing tickets without changing the Vanta exception itself.

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

## Changelog

Significant changes get one file under [changelog/entries/](changelog/entries/); the rules
are in [changelog/README.md](changelog/README.md). Enable the hook that validates them, once
per clone:

```bash
git config core.hooksPath .githooks
```

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
