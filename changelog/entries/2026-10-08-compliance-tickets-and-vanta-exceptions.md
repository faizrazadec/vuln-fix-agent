---
date: 2026-10-08
area: compliance
kind: added
---

# Unfixable findings are Vanta exceptions with a Linear ticket and a review date

The agent now deactivates findings with no reachable fix in Vanta itself, and every
exception and every Critical/High fix PR is tracked in a Linear ticket assigned to the
project owner. Exceptions are reviewed every 30 days, and fix tickets close when their
PR merges. This closes two ISO findings: fixes untracked in tickets, and exceptions with
no end date or remediation plan.

Operators need `LINEAR_API_KEY`, `LINEAR_TEAM_ID` and `LINEAR_PROJECT_ID` in `.env`, a
`linear_id` on each owner in `projects.json`, and Vanta credentials with write scope.

- PR: [#1](https://github.com/faizrazadec/vuln-fix-agent/pull/1)
- Docs: [Compliance tickets](../../README.md#compliance-tickets)
