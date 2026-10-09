---
date: 2026-10-09
area: vuln-fix
kind: changed
---

# Fix tickets close the moment their PR merges

The agent now adds a `Fixes <ID>` line for each Linear ticket to the fix PR's
description, so Linear's GitHub integration closes the ticket on merge. Before,
tickets stayed open until the nightly `linear-ticket sync`. That sync still runs as
the backup for repos whose GitHub org is not linked to the Linear workspace.

- PR: [#4](https://github.com/faizrazadec/vuln-fix-agent/pull/4)
- Docs: [vuln-fix skill, step 8](../../app/skills/vuln-fix/SKILL.md)
