# AGENTS.md

Operational state of the deployment — what is *running right now*, as opposed to how the
code is designed. [CLAUDE.md](CLAUDE.md) covers architecture and the load-bearing design
decisions; read that for *why* the code looks the way it does. Read this one first for
*where things stand*, so a fresh session doesn't have to rediscover it by poking at Docker.

**Maintenance rule:** update the dated sections below whenever infrastructure changes —
a rename, a new volume, a changed schedule, a rotated key. A stale entry here is worse
than no entry, because it will be trusted. Facts the repo already records (code
structure, module layout) do not belong here.

## Current state — verified 2026-09-10

| Thing | Value |
| --- | --- |
| Project directory | `/opt/vuln-fix-agent` (moved from `/opt/a2claude`) |
| Compose service + container | `vuln-fix-agent` (no `-api` suffix) |
| Image | `vuln-fix-agent:latest` |
| Network | `vuln-fix-agent-net` |
| Endpoint | `http://127.0.0.1:9999` — host-only, nothing fronts it |
| Agent card name | `Vuln Fix Agent` (`app/main.py`) |
| Volumes | `vuln-fix-agent_{claude-auth,workspace,state,ssh-keys}` |
| GitHub repo | `git@github.com:faizrazadec/vuln-fix-agent.git` |
| SSH key | `~/.ssh/vuln-fix-agent` (+ `.pub`), passphrase in `.env` |
| Key fingerprint | `SHA256:kJoYmMYPZeCvEMEnH5QlKURcVqJuIT/gJ45wyyeKmOc` |
| Scheduled run | user crontab, `0 3 * * 1-5`, logs to `/opt/vuln-fix-agent/vuln-run.log` |

The Claude subscription login is live in `vuln-fix-agent_claude-auth`
(`.credentials.json` + `.claude.json`). Do not delete that volume — see CLAUDE.md.

The SSH key is registered on GitHub **twice**, as an Auth key and as a Signing key.
Both are confirmed working: pushes succeed and GitHub reports commits as
`verified: true`. If the Verified badge ever disappears, check the Signing-key
registration before suspecting the code.

## Operational gotchas

These cost real time to rediscover. They are about running the stack, not about the code.

- **`docker compose exec` lands you as `root`, not `agent`.** The server itself runs as
  `agent` (uid 1000), and ssh resolves `~` from the *uid*, not `$HOME` — so a bare
  `exec ... ssh -T git@github.com` reads `/root/.ssh`, finds no `known_hosts`, and fails
  with `Host key verification failed`. That is a false alarm. Always test with
  `docker compose exec -u agent`.
- **The container's ssh-agent socket is not in the exec environment.** The entrypoint
  starts an agent and holds the unlocked key for the server process only. To test auth by
  hand: `S=$(find /tmp -type s -name 'agent.*' | head -1)` inside the container, then
  `SSH_AUTH_SOCK=$S ssh -T git@github.com`.
- **The host has no ssh-agent running.** The key is passphrase-protected, so pushing or
  signing *from the host* needs one started for the command: launch `ssh-agent`, add the
  key via `SSH_ASKPASS` + `SSH_ASKPASS_REQUIRE=force` (the same trick `app/entrypoint.sh`
  uses, since there is no tty), then kill it afterwards. A plain `git push` with no agent
  fails as `Permission denied (publickey)`, which looks like a key problem and is not.
- **Retagging an image does not update it.** `docker tag old new` carries the old *build*
  forward, so code changes silently do not take effect and only a symptom downstream
  (e.g. the agent card serving a stale name) reveals it. After any source change,
  `docker compose build`, not just `up -d`.
- **The SSH key filename is entirely `.env`-driven.** `SSH_AUTH_KEY`, `SSH_SIGN_KEY`, and
  `SSH_SIGN_KEY_PRIVATE` name it; `entrypoint.sh` and `load-keys.sh` only ever read those.
  Nothing tracked in git hardcodes the filename — so renaming the key touches `.env`,
  `~/.ssh/config`, `~/.config/git/allowed_signers`, `git config --global user.signingkey`,
  and the `ssh-keys` volume, and no source file at all.
- **`load-keys.sh` copies but never deletes.** Renaming or rotating a key leaves the old
  one in the volume, from where `entrypoint.sh` copies *everything* into `~/.ssh`. Wipe
  the volume's contents first, then re-run the script.

## Health check

```bash
docker compose ps                                   # container up, port bound
curl -s http://127.0.0.1:9999/.well-known/agent-card.json | head -c 200   # card is public
docker compose logs --tail 20 vuln-fix-agent        # expect "ssh-agent holds 1 key(s)"
docker compose exec -u agent vuln-fix-agent \
  sh -c 'ls ~/.claude/.credentials.json'            # login survived
uv run python tests/test_protocol.py                # offline, free, five checks
```

## Change log

Newest first. One line per infrastructure change, with the date.

- **2026-09-10** — SSH key renamed `a2claude_agent` → `vuln-fix-agent`; key material
  untouched (same fingerprint), so both GitHub registrations still match. Container and
  compose service renamed `vuln-fix-agent-api` → `vuln-fix-agent`. Image rebuilt: the
  running one was a retag of the pre-rename `a2claude:latest` and was still serving the
  old agent-card name. Crontab repointed to `/opt/vuln-fix-agent`.
- **2026-09-10** — Project renamed `a2claude` → `vuln-fix-agent`: directory moved, GitHub
  repo renamed, volumes copied to `vuln-fix-agent_*`, agent card renamed, launchd plist
  renamed to `com.faizraza.vuln-fix-agent.vulnrun.plist`. Cloudflare tunnel dropped; the
  stack is now local-only and `PUBLIC_URL` is empty on purpose.

## Known loose ends

- The pre-rename `a2claude_*` volumes and the `a2claude:latest` image tag still exist as a
  rollback path. Safe to delete once the current stack has run a full cycle; nothing
  references them.
- `CLAUDE.md` documents the one-time login as `docker compose exec -it vuln-fix-agent
  claude`, which runs as `root` per the gotcha above. It worked, but `-u agent` is the
  honest form if the login ever needs redoing.
