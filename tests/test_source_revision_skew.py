"""The owner stands down for newer CODE, not just a newer version tag.

This fork is served straight out of a working tree (`uv --directory <repo> run
...`), so `package_version` is the same string on every commit and cannot
distinguish a daemon carrying last week's code from a frontend started with
today's fix in it. Measured on 2026-09-18: a fix was committed, and every new
client attached to the running owner and served the old code anyway, because
both sides honestly reported 4.24.3.
"""

from __future__ import annotations

import pytest

from linkedin_mcp_server import daemon_version, source_revision
from linkedin_mcp_server.daemon_version import Skew

OLD = "aaaaaaaaaaaa"
NEW = "bbbbbbbbbbbb"
OTHER = "cccccccccccc"


@pytest.fixture
def ancestry(monkeypatch):
    """Stand in for git: NEW descends from OLD, OTHER is diverged from both."""
    edges = {(NEW, OLD)}  # (candidate, ancestor)

    def fake(*, candidate: str, ancestor: str) -> bool:
        if not candidate or not ancestor or candidate == ancestor:
            return False
        return (candidate, ancestor) in edges

    monkeypatch.setattr(source_revision, "is_descendant", fake)
    return edges


class TestCommitDecidesWhenBothPublishOne:
    def test_newer_commit_unseats_owner_on_an_identical_version(self, ancestry):
        """The exact case that let a stale daemon serve old code all day."""
        assert (
            daemon_version.compare(
                owner="4.24.3",
                frontend="4.24.3",
                owner_revision=OLD,
                frontend_revision=NEW,
            )
            is Skew.OWNER_IS_STALE
        )

    def test_same_commit_attaches(self, ancestry):
        assert (
            daemon_version.compare(
                owner="4.24.3",
                frontend="4.24.3",
                owner_revision=NEW,
                frontend_revision=NEW,
            )
            is Skew.SERVICEABLE
        )

    def test_older_commit_attaches_rather_than_downgrading_the_browser(self, ancestry):
        assert (
            daemon_version.compare(
                owner="4.24.3",
                frontend="4.24.3",
                owner_revision=NEW,
                frontend_revision=OLD,
            )
            is Skew.SERVICEABLE
        )

    def test_diverged_commits_do_not_evict_each_other(self, ancestry):
        """The ping-pong guard, asserted from both directions.

        "Different" is not an ordering. If mere difference unseated an owner,
        two frontends on diverged commits would each read the other as stale
        and take the browser away from it in turn, forever.
        """
        assert (
            daemon_version.compare(
                owner="4.24.3",
                frontend="4.24.3",
                owner_revision=NEW,
                frontend_revision=OTHER,
            )
            is Skew.SERVICEABLE
        )
        assert (
            daemon_version.compare(
                owner="4.24.3",
                frontend="4.24.3",
                owner_revision=OTHER,
                frontend_revision=NEW,
            )
            is Skew.SERVICEABLE
        )

    def test_commit_outranks_the_version(self, ancestry):
        """A newer tag does not unseat an owner running later code.

        The commit is the better evidence when both are available, so it is not
        merely consulted first, it settles the question.
        """
        assert (
            daemon_version.compare(
                owner="4.24.3",
                frontend="9.0.0",
                owner_revision=NEW,
                frontend_revision=OLD,
            )
            is Skew.SERVICEABLE
        )


class TestInstalledCopiesAreUnaffected:
    """No revisions means the original version comparison, untouched."""

    def test_newer_version_still_unseats_when_no_revisions(self):
        assert (
            daemon_version.compare(owner="4.24.3", frontend="4.25.0")
            is Skew.OWNER_IS_STALE
        )

    def test_same_version_still_attaches(self):
        assert (
            daemon_version.compare(owner="4.24.3", frontend="4.24.3")
            is Skew.SERVICEABLE
        )

    def test_unparseable_version_still_attaches(self):
        assert (
            daemon_version.compare(owner="not-a-version", frontend="4.24.3")
            is Skew.SERVICEABLE
        )

    @pytest.mark.parametrize(
        "owner_rev, frontend_rev",
        [(OLD, ""), ("", NEW), ("", "")],
        ids=["owner-only", "frontend-only", "neither"],
    )
    def test_one_sided_revision_falls_back_to_versions(self, owner_rev, frontend_rev):
        """A half-published revision is not evidence, so versions decide.

        An owner predating the field publishes none, and a frontend installed
        from a wheel has none. Either way there is nothing to compare, and
        guessing from one side would be worse than the fallback.
        """
        assert (
            daemon_version.compare(
                owner="4.24.3",
                frontend="4.25.0",
                owner_revision=owner_rev,
                frontend_revision=frontend_rev,
            )
            is Skew.OWNER_IS_STALE
        )


class TestRevisionResolution:
    def test_is_descendant_is_false_without_both_sides(self):
        """Guarded before git is invoked, so a missing value cannot evict."""
        assert not source_revision.is_descendant(candidate="", ancestor=OLD)
        assert not source_revision.is_descendant(candidate=NEW, ancestor="")
        assert not source_revision.is_descendant(candidate=NEW, ancestor=NEW)

    def test_current_revision_never_raises(self):
        """Any git failure must read as "no revision", never as an error.

        This runs in the launch path of a server that otherwise needs no git.
        """
        assert isinstance(source_revision.current_revision(), str)

    def test_a_git_failure_reads_as_no_revision(self, monkeypatch):
        monkeypatch.setattr(source_revision, "_git", lambda *a: None)
        source_revision.current_revision.cache_clear()
        try:
            assert source_revision.current_revision() == ""
            assert not source_revision.is_descendant(candidate=NEW, ancestor=OLD)
        finally:
            source_revision.current_revision.cache_clear()
