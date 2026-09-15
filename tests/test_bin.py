"""The agent CLIs in app/bin have an exit-code contract SKILL.md depends on. Test it.

Nothing here touches the network: every case is a usage, config, or state-file path that
returns before the first API call. That is deliberate — these are exactly the paths that
used to raise tracebacks in production, where a crash exit is indistinguishable from a
meaningful one.

Run: uv run python tests/test_bin.py
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile

BIN = pathlib.Path(__file__).resolve().parent.parent / "app" / "bin"


def run(tool, *args, **env):
    e = {**os.environ, "STATE_DIR": STATE, **env}
    return subprocess.run(
        [sys.executable, str(BIN / tool), *args],
        capture_output=True, text=True, env=e,
    )


_tmp = tempfile.TemporaryDirectory()
STATE = _tmp.name


def test_ledger_usage_errors():
    """A missing argument must be a usage error, not an IndexError.

    `vuln-ledger <project> notified` used to raise IndexError. Triage reads this exit code
    to decide whether to re-Slack an owner, so a traceback exit(1) silently meant "notify".
    """
    r = run("vuln-ledger", "proj", "notified")
    assert r.returncode == 2, r
    assert "Traceback" not in r.stderr, r.stderr
    assert "usage" in r.stderr.lower(), r.stderr

    r = run("vuln-ledger", "proj", "add-resolved", "CVE-1")  # missing url
    assert r.returncode == 2 and "Traceback" not in r.stderr, r

    r = run("vuln-ledger", "proj", "nonsense")
    assert r.returncode == 2 and "unknown command" in r.stderr, r
    print("ledger usage errors OK: exit 2, no tracebacks")


def test_ledger_tolerates_partial_state_file():
    """A ledger missing a top-level key must not KeyError."""
    pathlib.Path(STATE, "legacy.json").write_text('{"resolved": {}}')
    r = run("vuln-ledger", "legacy", "notified", "CVE-1")
    assert r.returncode == 1, r          # not recorded -> notify
    assert "Traceback" not in r.stderr, r.stderr

    pathlib.Path(STATE, "corrupt.json").write_text("{not json")
    r = run("vuln-ledger", "corrupt", "notified", "CVE-1")
    assert r.returncode == 2 and "unreadable" in r.stderr, r
    print("ledger tolerates partial and corrupt state files")


def test_ledger_rejects_path_traversal():
    """The project name becomes a filename; it must stay one path segment."""
    for bad in ("../../etc/passwd", "a/b", ".."):
        r = run("vuln-ledger", bad, "add-notified", "CVE-1")
        assert r.returncode == 3, (bad, r)
        assert "invalid project name" in r.stderr, (bad, r.stderr)
    print("ledger rejects path traversal in the project name")


def test_notified_suppression_lifts_when_a_fix_appears():
    """The bug this exists for: an unfixable CVE stayed suppressed forever.

    Suppression must end the moment Vanta starts reporting a patched version.
    """
    assert run("vuln-ledger", "p1", "add-notified", "CVE-9",
               "--fixed-version", "none").returncode == 0

    r = run("vuln-ledger", "p1", "notified", "CVE-9", "--fixed-version", "none")
    assert r.returncode == 0, "nothing changed -> must stay quiet"

    r = run("vuln-ledger", "p1", "notified", "CVE-9", "--fixed-version", "1.2.3")
    assert r.returncode == 1, "a patch shipped -> must notify again"
    assert "fixed version changed" in r.stderr, r.stderr

    # "NotAvailable" and friends are Vanta's other spellings of "no patch".
    r = run("vuln-ledger", "p1", "notified", "CVE-9", "--fixed-version", "NotAvailable")
    assert r.returncode == 0, "NotAvailable == none -> still quiet"
    print("notified suppression lifts when a fixed version appears")


def test_notified_ttl():
    """An entry nobody ever acted on resurfaces rather than being buried forever."""
    run("vuln-ledger", "p2", "add-notified", "CVE-8", "--fixed-version", "none")
    f = pathlib.Path(STATE, "p2.json")
    d = json.loads(f.read_text())
    d["notified"]["CVE-8"]["date"] = "2020-01-01"
    f.write_text(json.dumps(d))

    r = run("vuln-ledger", "p2", "notified", "CVE-8", "--fixed-version", "none")
    assert r.returncode == 1 and "resurfacing" in r.stderr, r

    r = run("vuln-ledger", "p2", "notified", "CVE-8", "--fixed-version", "none",
            LEDGER_NOTIFY_TTL_DAYS="0")
    assert r.returncode == 0, "TTL 0 disables ageing"
    print("notified TTL resurfaces stale entries, and can be disabled")


def test_resolved_clears_notified():
    """A CVE that just landed in a PR is no longer one to stay quiet about."""
    run("vuln-ledger", "p3", "add-notified", "CVE-7", "--fixed-version", "none")
    run("vuln-ledger", "p3", "add-resolved", "CVE-7", "https://example/pull/1")
    d = json.loads(run("vuln-ledger", "p3").stdout)
    assert "CVE-7" not in d["notified"], d
    assert d["resolved"]["CVE-7"]["pr"] == "https://example/pull/1", d
    print("add-resolved clears the notified suppression")


def test_vanta_unknown_project():
    """Exit 3 for a name that is not in the registry — before any network call."""
    r = run("vanta-findings", "definitely-not-a-project")
    assert r.returncode == 3, r
    assert "not a registered project" in r.stderr, r.stderr
    print("vanta-findings rejects an unregistered project with exit 3")


def test_slack_unconfigured_is_exit_4():
    """Exit 4 means 'could not notify'. SKILL.md requires the agent to report that rather
    than assume the message was delivered, so the code must stay distinguishable."""
    r = run("slack-notify", "hello", SLACK_WEBHOOK_URL="", SLACK_BOT_TOKEN="")
    assert r.returncode == 4, r
    assert "not configured" in r.stderr, r.stderr
    print("slack-notify reports unconfigured as exit 4")


if __name__ == "__main__":
    test_ledger_usage_errors()
    test_ledger_tolerates_partial_state_file()
    test_ledger_rejects_path_traversal()
    test_notified_suppression_lifts_when_a_fix_appears()
    test_notified_ttl()
    test_resolved_clears_notified()
    test_vanta_unknown_project()
    test_slack_unconfigured_is_exit_4()
    print("ok")
