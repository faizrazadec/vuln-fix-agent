---
date: 2026-10-09
area: vuln-fix
kind: changed
---

# Owners are asked to review a fix PR only after its GitHub checks pass

The agent now waits for every check on a fix PR it opened or updated. It fixes and
re-pushes anything that fails before it posts "ready for review" in Slack. A fixable
Critical/High finding in the image or dependency scan is no longer handed off with a
warning. A PR goes to review with a red check only when no fix exists, the failure is
already on the base branch, or the cause is infrastructure. In those cases the Slack
message and PR body name the check and the reason. A check still running after about
30 minutes is reported as pending, not green.

- PR: [#3](https://github.com/faizrazadec/vuln-fix-agent/pull/3)
- Docs: [vuln-fix skill, step 8](../../app/skills/vuln-fix/SKILL.md)
