# Changelog entries

The changelog is a short map of significant changes, not a work log. Each entry
is its own file, so branches never compete for one shared document.

## When to add an entry

By default, add no entry. Add one only when the change would still be worth a
line in a monthly summary six weeks later. Qualifying changes materially affect
at least one of:

- what users, admins or operators can do;
- security, roles and permissions, stored data or data migrations;
- deployment, rollback, recovery or other operational procedures;
- the architecture, or contracts with other services;
- developer workflows that the whole repo must follow; or
- deprecations and removals.

One PR usually produces zero entries or one.

Do not add an entry for:

- cosmetic changes;
- small bug fixes;
- tests;
- refactors;
- dependency bumps;
- documentation fixes.

Put those details in the pull request.

## File format

Name each file:

```text
changelog/entries/YYYY-MM-DD-short-descriptive-slug.md
```

Use a lowercase kebab-case slug, and never overwrite an existing entry. Every
entry has this shape:

```markdown
---
date: YYYY-MM-DD
area: lowercase-area
kind: added
---

# Outcome-oriented title

One short paragraph on what changed and why it matters.

- PR: [#<number>](https://github.com/faizrazadec/vuln-fix-agent/pull/<number>)
- Docs: [the current doc](../../path/to/doc.md)
```

`kind` is one of `added`, `changed`, `fixed`, `deprecated`, `removed` or
`security`. An entry stays under 40 lines and contains at least one link,
normally to its PR. Describe the result and its impact, not how it was built.

## Monthly summary

Once a month has been over for 14 days, replace that month's entries with one
hand-written `changelog/archive/YYYY-MM.md` summary. Never generate a combined
changelog.

## Check

```bash
python3 scripts/check_changelog_entries.py
```

The script checks entry filenames, frontmatter, length and the heading, and that
local links point to files that exist.
