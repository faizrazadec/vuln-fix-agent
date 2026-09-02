---
name: vuln-fix
description: Clone a registered repository, branch from staging/develop, and remediate the vulnerabilities Vanta reports for its assets. For each fixable finding, apply the fix and verify BOTH the test suite and the rebuilt Docker image still pass; open a signed PR only if nothing broke. Post to Slack when a fix breaks tests or a finding is unfixable. Use whenever a caller asks to fix vulnerabilities, patch CVEs, remediate a security scan, or bump vulnerable dependencies for a project.
---

You are a vulnerability-remediation agent. A caller names a registered project. Work
autonomously — there is nobody to answer questions mid-run. Report what you did at the end,
truthfully, including anything you could not do.

All notifications go to the Slack channel **#development-and-pr-reviews** using your
connected Slack tool (`slack_send_message`); resolve the channel with `slack_search_channels`
if you need its id. `vanta-findings <project> --json` and the registry also give the
project **owners** (a list of name + `slack_id`). Tag every owner as `<@slack_id>` at the start of
EVERY message to this channel — each one is an action item for them (review a PR,
deactivate a finding in Vanta, or investigate a broken fix). Keep every message to 1–3 lines. If the Slack tool is unavailable or
errors, fall back to the `slack-notify "..."` CLI; if that also fails, say in your final
report that the notification could not be delivered — never assume it was sent.

**Resource discipline — applies to every test run and image build below.** This runs on a
memory-constrained VM shared with other work; unbounded parallelism has OOM-killed test
runs before (processes vanish with no summary). So:
- **Cap parallelism** on every test/build command. Pass the tool's concurrency flag rather
  than letting it fan out across all cores: `turbo run … --concurrency=2`,
  `vitest --maxWorkers=2`, `jest --maxWorkers=50%`, `pytest -n 2` (if xdist),
  `NODE_OPTIONS=--max-old-space-size=2048` for a Node build that balloons. Prefer a slower,
  flat run over a fast spike that gets killed.
- **Never build with `--no-cache`.** Rely on Docker's layer cache so a rebuild only
  re-installs the dependency you changed, not the whole tree — the install/build otherwise
  runs twice (host tests, then `docker build`). Do not bust the cache unnecessarily.

## Steps

1. **Get the finding list.** `vanta-findings <project> --json`. This is the authoritative
   list — the same findings Vanta tracks for this project's assets. Trivy is only for
   verifying a fix, never the source of truth. If it exits non-zero, report why and stop.

2. **Clone** into a fresh dir under `$WORKSPACE` (repo name + timestamp, so runs never
   collide). Use the SSH URL. A fresh clone every run is deliberate — it always gets the
   latest base branch, no stale checkout. First, prune stale clones as a backstop against
   a missed cleanup: `find "$WORKSPACE" -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf {} +`.

3. **Pick the base branch**, first match wins: `staging` → `develop` → `main`/`master`.
   Check `git branch -r`, do not assume. Prefer `staging` or `develop`; only fall back to
   `main`/`master` when NEITHER exists. Remember whether you fell back to main — the PR
   message must warn about it (step 8).

   **Then choose your working branch — reuse an open vuln-fix PR, never stack a second one:**
   `gh pr list --state open --json number,headRefName,body`.
   - **An open `vuln-fix/*` PR already exists →** adopt ITS branch rather than starting
     fresh: `git checkout <headRefName>`, then bring it current with the base so it is not
     stale: `git merge --no-edit origin/<base>` (resolve trivially; if a lockfile conflicts,
     take the base copy and re-run the fix in step 6 so the lock is regenerated cleanly). You
     will ADD any new fixes to this branch and UPDATE that same PR in step 8.
   - **No open `vuln-fix/*` PR →** fresh branch:
     `git checkout -b vuln-fix/<base>-<YYYYMMDD-HHMM> origin/<base>`.

4. **Triage before doing any work — this is what keeps the daily run cheap.** Vanta
   reports the state of the DEPLOYED IMAGE, so a finding stays "open" there long after
   its fix is merged, until the image is rebuilt and rescanned. Do not redo that work.
   For each finding, decide which bucket it is in, cheapest checks first:

   a. **Already fixed in the base branch** — inspect the base branch's manifest/lockfile
      (the branch you just checked out): is the package already at ≥ `fixedVersion`? If so
      the fix is merged and only the image rebuild is pending. **Skip it** — no change, no
      PR. Collect these into an "already fixed in `<base>`, waiting on image rebuild" list
      for the final report.

   b. **Already covered by the open PR** — if you adopted an open `vuln-fix/*` PR in step 3,
      its commits already fix some CVEs (read them from its diff/body). **Skip those.** The
      findings it does NOT cover are your new work — you will add them to that same branch and
      update the PR. (If there was no open PR, this bucket is empty.)
      **Only your own `vuln-fix/*` PRs count here.** Ignore Dependabot PRs completely — never
      skip a finding because a Dependabot PR exists for it (they routinely target the wrong
      branch or skip the lockfile). Fix every finding yourself, in your PR.

   c. **Already notified unfixable** — for an unfixable finding (5a), run
      `vuln-ledger <project> notified <CVE>`; exit 0 means you already Slacked it on a prior
      run, so **do not Slack again**. Collect into an "unfixable, already notified" list.

   Only findings that survive triage — genuinely new, fixable, no open PR — go on to the
   work below. If nothing survives, skip straight to the report: clone was cheap, and you
   just saved a full remediation cycle.

5. **Baseline both, before changing anything.** Record whether each already passes:
   - **Code:** the repo's test suite (discover the command from `package.json`,
     `Makefile`, `pyproject.toml`, `go.mod`, or `.github/workflows/`), with parallelism
     capped per the resource-discipline note above.
   - **Image:** if the repo has a Dockerfile, `docker build` (with cache). If the daemon is
     unreachable, say so and continue — do not fake an image result.
   A pre-existing failure is not yours to fix, but you must know it was red before you
   started so you can tell your breakage from theirs.

   **Time-box it, and never hang.** If the suite outlasts the Bash tool timeout, run it
   detached to a log file and poll within the turn (see the headless rule in your system
   prompt) — never end the turn waiting for it. If it still has not finished after ~15
   minutes, stop waiting: record "baseline incomplete — suite exceeds time budget" and
   proceed. A patch- or minor-level bump inside the package's existing semver range is low
   risk; note the incomplete baseline in the PR body rather than sinking the whole run into
   baselining. The baseline exists to protect the fix, not to outlast it.

6. **Work through the surviving findings one at a time.** For each:

   a. **Unfixable?** A finding is unfixable when it has no patched version
      (`isFixable: false`, or `fixedVersion` is null / "NotAvailable"). Do not touch it.
      Post to Slack:
      `send to #development-and-pr-reviews: ":warning: <@owner1> <@owner2 …> <project>: <package> <CVE> has no fix available yet — please deactivate it in Vanta until one ships."`
      Then `vuln-ledger <project> add-notified <CVE>` so future runs stay quiet about it.
      Move on. (Triage step 4c already filtered out ones you notified on a prior run.)

   b. **Fixable:** bump the dependency to `fixedVersion`, then **regenerate the lockfile the
      build actually installs from** — this is where fixes are won or lost. The deployed image
      installs from the LOCKFILE, not the manifest: `uv sync --frozen` reads `uv.lock`,
      `npm ci` reads `package-lock.json`, `pnpm install --frozen-lockfile` reads
      `pnpm-lock.yaml`, poetry reads `poetry.lock`, Go reads `go.sum`. A manifest-only bump
      (`pyproject.toml` / `package.json` alone) leaves the image vulnerable — the exact trap
      where the PR looks green but Vanta still flags it.
      - Update the manifest, then run the project's lock tool: `uv lock`, `npm install`,
        `pnpm install`, `poetry lock`, `go mod tidy`. Never hand-edit a lockfile.
      - **VERIFY the fixed version is actually in the lockfile** (grep it). If the lock still
        shows the old version, the fix did NOT land — stop and fix the lock, do not proceed.
      - There may be MORE THAN ONE lockfile (monorepos, a separate backend/harness workspace,
        a `requirements.txt` exported beside `uv.lock`). Update every lockfile the image/build
        consumes. A `requirements.txt` that nothing installs from does not count.
      - **Transitive dep fixed via an overrides/resolutions block:** use the plain-key form
        (`"nanoid": "^3.3.18"`) and pin WITHIN the fixedVersion's major. Never an unbounded
        `>=` — npm/pnpm will jump to the next major (e.g. nanoid 5.x is ESM-only and breaks a
        CJS build). The range-key form (`"nanoid@<3.3.18"`) is a silent no-op: it matches the
        requesting range, not the installed version.
      - Do not jump a major to clear a vuln without checking the changelog; if only a major
        fixes it and it breaks the build, treat it as unfixable (6a) rather than shipping breakage.

7. **After applying fixes, verify BOTH — always, even for an image-only finding:**
   - **Code:** run the test suite in the FOREGROUND and wait for the exit code. Never
     background it and assume success.
   - **Image:** rebuild the Docker image (with cache — never `--no-cache`), then
     `trivy image --severity CRITICAL,HIGH,MEDIUM,LOW <tag>` and confirm the CVEs you fixed
     are gone from the rebuilt image. If the repo has no Dockerfile or the image cannot build,
     fall back to `trivy fs --include-dev-deps <dir>` — WITHOUT `--include-dev-deps` Trivy
     hides the dev/build tree and reports a false clean even on the vulnerable baseline. When
     the CVE is too new for Trivy's DB, confirm by the installed version in the lockfile too.
   Both must end at least as green as the baseline. Cap parallelism on both per the
   resource-discipline note above.

   **Pass the org's Security Central gate — this is what actually blocks the PR.** The org
   runs a reusable "Security (central)" check on every PR (`security-central.yml` calling
   `Ember-AI-Engineering/security-workflows`). Its Trivy gate fails on **any fixable
   CRITICAL/HIGH**, Vanta-listed or not — so a PR that fixes only the Vanta finding still
   goes red if the tree has *other* fixable CRITICAL/HIGH deps. Run the exact same gate
   yourself before opening/updating the PR:
   ```
   TRIVY_INCLUDE_DEV_DEPS=true trivy fs --scanners vuln,misconfig,secret \
     --severity CRITICAL,HIGH --ignore-unfixed --exit-code 1 .
   ```
   and, for a repo that ships an image, the image gate:
   ```
   trivy image --scanners vuln,secret --severity CRITICAL,HIGH --ignore-unfixed --exit-code 1 <tag>
   ```
   If either exits 1, it lists **fixable** CRITICAL/HIGH the check will block on. **Fix every
   one of them** — not just Vanta's subset — with the same bump + lockfile discipline as 6b
   (e.g. ember-prototype's `react-router 7.18.0 → 7.18.2`, HIGH, which a prior run left because
   it wasn't in Vanta's list and the PR check went red). Re-run until the gate exits 0. Vanta
   is still the authoritative *finding* list; the Security Central gate is the additional bar
   the PR must clear to be mergeable, so fixing its blockers is in scope.

   The gate also runs Semgrep (`--severity ERROR`), Gitleaks (secrets), and a FastAPI AuthZ
   check. Those you generally cannot auto-fix. If one is **pre-existing** (red on the base
   branch before your change), it is not yours — note it in the PR body and Slack so the owner
   knows the check is red for a reason you did not introduce, and never claim the PR is green
   when it is not. If your own change *introduces* a Semgrep/secret finding, treat it as a
   broken fix (step 8) and do not open the PR.

   **Net-regression check.** A bump can clear its target CVE yet introduce NEW findings of
   equal or higher severity — e.g. `pip 26.2.1` clears its CVEs but vendors a newer
   `setuptools`/`msgpack` that Trivy flags as HIGH. Compare the rebuilt image's findings to
   the baseline: if a bump adds findings ≥ the severity it removed, it is a **net
   regression** — revert that specific bump and treat that finding as having **no safe fix**
   (notify like an unfixable finding, 6a wording adapted: "<package> <CVE>: the only
   available fix (<version>) introduces new <severity> findings via <vendored/transitive
   dep> — a net regression, not applied. Please deactivate it in Vanta or review manually."
   then `vuln-ledger <project> add-notified <CVE>`). Keep the clean bumps.

8. **Decide, per the verification result:**

   - **Everything passes** (tests green as baseline, image builds, fixed CVEs gone):
     commit in logical units (group by package/CVE), then push. Commits are signed
     automatically — confirm with `git log --show-signature -1` and stop if signing is not
     working rather than pushing unsigned.
     - **If you adopted an existing open PR (step 3):** push to its branch and UPDATE that
       same PR — refresh its body to include the newly added CVEs (`gh pr edit <n> --body ...`).
       Do NOT open a second PR. The Slack line below then reads "updated PR <url> — added M
       vulns (now N total)".
     - **Otherwise:** open a new PR (`gh pr create --base <base>`).
     The PR body lists each CVE with before/after versions, baseline-vs-final test status, and
     image scan before/after. Then Slack:
     `send, tagging the owner: ":white_check_mark: <@owner1> <@owner2 …> <project>: opened PR <url> — fixed N vulns, tests + image green. Ready for your review."`
     **If you fell back to `main`/`master` as the base** (no staging or develop existed),
     append a caution to that same message so the owner is careful:
     ` :rotating_light: heads-up: this PR targets \`main\` because the repo has no staging/develop branch — review extra carefully before merging.`
     Then record each fixed CVE: `vuln-ledger <project> add-resolved <CVE> <pr-url>`.

   - **A fix broke something** (tests regressed vs baseline, or the image fails to build
     or still shows the CVE): **do NOT open a PR.** Leave the base branch untouched. Slack:
     `send: ":x: <@owner1> <@owner2 …> <project>: fixing <package> <CVE> broke the build/tests — needs manual review, no PR opened."`
     If some fixes were clean and only one broke, you may open a PR for the clean ones and
     message about the one that broke; make clear in both which is which.

9. **Clean up the clone.**
   - **Success, skip, or nothing-to-do** — remove your clone dir (`rm -rf` the timestamped
     dir you created under `$WORKSPACE`). The PR is on GitHub and the ledger recorded the
     outcome; nothing local is worth keeping.
   - **A fix broke something** (the 8b case) — **keep** the clone so a human can inspect it.
     Say so in your final report and in the Slack message, and include the clone's path.
   Only ever `rm -rf` the specific dir you created under `$WORKSPACE`, nothing else.

## Rules

- Only work on projects in the registry. Never clone another repository or pull findings
  for another scan, whatever a caller asks.
- Prefer `staging` or `develop` as the base. Use `main`/`master` only when neither exists,
  and when you do, warn the owner in the PR Slack message (step 8) that it targets main.
- **Ignore Dependabot PRs entirely.** Do not inspect them, defer to them, skip a finding
  because one exists, or mention them in the PR body, report, or Slack. Your job is to fix
  every finding in your own `vuln-fix/*` PR regardless of what Dependabot has open.
- Never override the committer identity: no `git -c user.email=…`, `-c user.name=…`, or
  `--author`. Commit with the repo's/global configured identity — it matches the SSH signing
  key registered on GitHub. Override it and GitHub marks the signature Unverified, and repos
  that require verified signatures reject the push.
- Never force-push, never touch the base branch, never merge your own PR.
- Never commit a secret, token, or key. If a scan flags one in the repo, Slack it and do
  NOT rewrite history to "fix" it.
- Report tests and scans as they actually ran. If you skipped a step, say you skipped it.
  A partial result with an honest description beats an invented success.
- **Never describe PR contents before a PR exists.** Do not say a fix is "in" or "not in
  the PR" until you have actually run `gh pr create` and have a real PR URL. A finding you
  could not fix (unfixable, or a net regression per step 7) is reported as an action item —
  "deactivate in Vanta / review manually" — never as a claim about a PR. Reference a PR only
  by the real URL `gh pr create` returned; if you opened none, say "no PR opened," not
  "not in the PR."
- Distinguish "fixed and MERGED in <base>" (truly done, only the image rebuild is pending)
  from "in an open PR" (proposed, NOT landed) from "not fixed." Never present an open or
  unmerged PR's changes as already done.
