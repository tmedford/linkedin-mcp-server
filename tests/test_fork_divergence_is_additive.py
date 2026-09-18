"""The fork may add to upstream's files. It may not edit them.

This fork exists to track upstream *and* carry tools that read LinkedIn's API
directly instead of scraping the rendered page. Those two goals only coexist
while our divergence never deletes a line upstream wrote: the nightly
``sync-upstream.yml`` merge stays clean because every diverged file is
additions only, and a merge that conflicts in code upstream is actively
developing would block the sync in the path that decides whether the server can
start at all.

Measured on 2026-09-18, the entire divergence was additions and nothing else.
The rule held by convention until a daemon change edited three upstream-owned
files and deleted thirteen of their lines, including in ``daemon_election.py``
-- upstream's most active file of the three, eight commits in ninety days. It
was reverted. This test is what makes the rule enforceable rather than
remembered, because the cost of breaking it lands weeks later on somebody
merging, not on the person who broke it.

**The rule is about DELETIONS, not about size.** A large addition is fine; it is
our code sitting beside theirs. Changing one of their lines is not, however
small, because that is the line a future upstream edit collides with.

**If something cannot be done additively, that is the signal it does not belong
in this fork.** Upstream owns its own behaviour; changing a decision they own
means proposing it to them, not overriding it here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: Where upstream might be found. The nightly workflow adds an "upstream"
#: remote; a developer clone usually has upstream as "origin" and the fork as
#: "fork". Both are tried rather than assuming either.
UPSTREAM_CANDIDATES = ("upstream/main", "origin/main")

#: Paths whose divergence is not covered by this rule. Generated files are
#: re-derived from the tree rather than written by hand, so a deletion in one
#: is an output of a change rather than the change itself.
EXEMPT_PREFIXES = ("docs/scraping-architecture.md",)

#: The rule is enforced strictly here: a deletion in shipped code is what a
#: future upstream edit collides with, and it changes what the server does.
SOURCE_ROOT = "linkedin_mcp_server/"

#: Upstream tests that assert an EXACT inventory, which adding a tool
#: necessarily breaks. ``assert len(tool_names) == 19`` has no additive form:
#: registering a twentieth tool makes the old number wrong, and leaving it
#: wrong would mean shipping a red suite. Each entry is a deliberate decision
#: with a reason, not a blanket exemption for tests -- a test file that starts
#: diverging for any other reason still fails this.
TEST_INVENTORY_EXEMPTIONS = {
    "tests/scraping/test_facade_contracts.py": "counts tools and facade delegates",
    "tests/scraping/test_policy_traces.py": "counts policy schemas",
    "tests/test_daemon_election.py": "asserts the served tool inventory",
}


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _upstream_ref() -> str | None:
    for ref in UPSTREAM_CANDIDATES:
        if _git("rev-parse", "--verify", "--quiet", ref).returncode == 0:
            return ref
    return None


def _numstat_against_upstream() -> list[tuple[str, str, str]]:
    """Rows of (added, deleted, path) for this tree against upstream.

    Compared against the **working tree** rather than HEAD, deliberately. In CI
    the two are identical so nothing is lost there, and locally it means the
    rule fires while the edit is still in the editor rather than after it is
    committed and pushed. Measured while writing this: a synthetic deletion in
    the working tree passed a HEAD-based check cleanly, so the first version of
    this guard would have reported green on exactly the mistake it exists to
    catch.
    """
    upstream = _upstream_ref()
    if upstream is None:
        pytest.skip("no upstream remote to compare against")
    numstat = _git("diff", "--numstat", upstream)
    if numstat.returncode != 0:
        pytest.skip(f"could not diff against {upstream}: {numstat.stderr.strip()}")
    rows = []
    for line in numstat.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            rows.append((parts[0], parts[1], parts[2]))
    return rows


def test_the_fork_only_adds_to_upstream_owned_files():
    """No file upstream owns may lose a line to us.

    Reported per file with the count, because "something diverged" is not
    actionable and the first question on a failure is always which file and how
    much.
    """
    upstream = _upstream_ref()
    offenders: list[str] = []
    for added, deleted, path in _numstat_against_upstream():
        # A binary file reports "-" for both counts and has no lines to speak of.
        if deleted in ("-", "0") or path.startswith(EXEMPT_PREFIXES):
            continue
        # A file we created cannot have had upstream lines deleted from it; git
        # reports deletions against a file of the same name only if it existed.
        if _git("cat-file", "-e", f"{upstream}:{path}").returncode != 0:
            continue
        if path in TEST_INVENTORY_EXEMPTIONS and not path.startswith(SOURCE_ROOT):
            continue
        offenders.append(f"  {path}: {added} added, {deleted} DELETED")

    assert not offenders, (
        "The fork deleted lines from files upstream owns, which is what makes "
        "the nightly upstream merge conflict:\n"
        + "\n".join(offenders)
        + "\n\nAdd alongside upstream's code instead of editing it. If the "
        "change cannot be expressed as an addition, it belongs in an upstream "
        "issue or PR rather than in this fork."
    )


def test_every_exemption_is_still_load_bearing():
    """An exemption that stopped being needed is drift, so it must be removed.

    An allowlist nobody prunes becomes a blanket permission with a comment on
    it. If upstream stops asserting an exact count, or we stop needing to touch
    the file, the entry has to go, or the next person reads it as "tests are
    exempt" -- which is exactly what this file exists to deny.
    """
    deleting = {
        path
        for _a, deleted, path in _numstat_against_upstream()
        if deleted not in ("-", "0")
    }

    stale = sorted(set(TEST_INVENTORY_EXEMPTIONS) - deleting)
    assert not stale, (
        "These files are exempted but no longer delete anything upstream wrote, "
        "so the exemption is stale and should be deleted:\n"
        + "\n".join(f"  {p} ({TEST_INVENTORY_EXEMPTIONS[p]})" for p in stale)
    )


def test_source_is_never_exempt():
    """No shipped module may be exempted, whatever the allowlist says.

    The exemptions exist for upstream tests that count things. Letting one
    cover ``linkedin_mcp_server/`` would quietly reintroduce the exact failure
    this file was written for, so the guard refuses it structurally rather than
    trusting whoever edits the list next.
    """
    leaked = sorted(p for p in TEST_INVENTORY_EXEMPTIONS if p.startswith(SOURCE_ROOT))
    assert not leaked, (
        "Shipped code cannot be exempted from the additive rule: "
        f"{leaked}. If it cannot be done additively, propose it upstream."
    )


def test_our_own_modules_are_not_counted_as_violations():
    """A control, so a green result cannot come from comparing nothing.

    Without this, a broken upstream ref or a diff that silently returned
    nothing would read exactly like compliance. This asserts the comparison
    actually sees our divergence.
    """
    changed = {path for _a, _d, path in _numstat_against_upstream()}
    assert changed, (
        "The diff against upstream reported no changed files at all. This fork "
        "carries its own tools, so an empty diff means the comparison is "
        "broken, not that the fork is clean."
    )
