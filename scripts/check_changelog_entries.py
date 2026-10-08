#!/usr/bin/env python3
"""Validate changelog entries against changelog/README.md. Exits 1 on any problem."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRIES = ROOT / "changelog" / "entries"
NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
KINDS = {"added", "changed", "fixed", "deprecated", "removed", "security"}
MAX_LINES = 40


def check(path: Path) -> list[str]:
    errors = []
    name = NAME.match(path.name)
    if not name:
        return [f"{path.name}: name must be YYYY-MM-DD-kebab-slug.md"]
    text = path.read_text()
    if len(text.splitlines()) > MAX_LINES:
        errors.append(f"{path.name}: over {MAX_LINES} lines")
    fm = FRONTMATTER.match(text)
    if not fm:
        return errors + [f"{path.name}: missing --- frontmatter ---"]
    fields = dict(line.split(":", 1) for line in fm.group(1).splitlines() if ":" in line)
    fields = {k.strip(): v.strip() for k, v in fields.items()}
    if fields.get("date") != name.group(1):
        errors.append(f"{path.name}: date must match the filename")
    if fields.get("kind") not in KINDS:
        errors.append(f"{path.name}: kind must be one of {sorted(KINDS)}")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", fields.get("area", "")):
        errors.append(f"{path.name}: area must be lowercase kebab-case")
    body = text[fm.end():].lstrip()
    if not body.startswith("# "):
        errors.append(f"{path.name}: body must start with a '# ' title")
    links = LINK.findall(body)
    if not links:
        errors.append(f"{path.name}: needs at least one link (usually the PR)")
    for target in links:
        if not re.match(r"[a-z]+://|#", target):
            local = (path.parent / target.split("#")[0]).resolve()
            if not local.exists():
                errors.append(f"{path.name}: broken link {target}")
    return errors


def main() -> int:
    errors = [e for p in sorted(ENTRIES.glob("*")) if p.name != ".gitkeep" for e in check(p)]
    for e in errors:
        print(e)
    print(f"{len(list(ENTRIES.glob('*.md')))} entries, {len(errors)} problems")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
