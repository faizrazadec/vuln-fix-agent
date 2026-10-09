---
date: 2026-10-09
area: vuln-fix
kind: changed
---

# Every fix PR gets a Linear fix ticket, any severity

The agent used to open a fix ticket only for a PR that fixed a Critical or High
finding. It now opens one for every fix PR, Medium/Low-only ones included. The
ticket lists every CVE the PR fixes, and its priority follows the highest
severity among them. Owners will see more tickets. The scan gates still block
only on Critical/High.

- PR: [#6](https://github.com/faizrazadec/vuln-fix-agent/pull/6)
- Docs: [the vuln-fix skill](../../app/skills/vuln-fix/SKILL.md)
