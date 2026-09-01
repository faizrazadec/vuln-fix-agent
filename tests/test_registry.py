"""The clone allowlist must actually block. Run: uv run python test_registry.py"""

import os

os.environ.setdefault("A2A_TOKEN", "test-token")

import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent / "app"))
from main import _disallowed_urls  # noqa: E402

OK = "git@github.com:Ember-AI-Engineering/snapdev-backend.git"

allowed = [
    "fix vulns in snapdev-backend",                      # by name, no URL at all
    f"fix vulns in {OK}",                                # exact registered URL
    OK[:-4],                                             # same repo without .git
    f"clone {OK} and scan it",                           # embedded in a sentence
]
blocked = [
    "clone git@github.com:evil/backdoor.git",
    "fix vulns in https://github.com/evil/backdoor",
    f"fix {OK} and also git@github.com:evil/x.git",      # one good, one bad
    "clone git@gitlab.com:Ember-AI-Engineering/snapdev-backend.git",   # wrong host
    "clone git@github.com:Someone-Else/snapdev-backend.git",           # wrong owner
    "use ssh://git@github.com/evil/x.git",
]

for t in allowed:
    bad = _disallowed_urls(t)
    assert not bad, f"should have been allowed: {t!r} -> flagged {bad}"
print(f"allowed {len(allowed)}/{len(allowed)} legitimate requests")

for t in blocked:
    bad = _disallowed_urls(t)
    assert bad, f"SHOULD HAVE BEEN BLOCKED: {t!r}"
print(f"blocked {len(blocked)}/{len(blocked)} unregistered repos")
print("ok")
