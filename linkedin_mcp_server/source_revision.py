"""Which commit this process is running, when it runs from a checkout.

``package_version`` answers "which release is this" and that is the right
question for an installed copy. It is the wrong question for this fork, which
is run straight out of a working tree: the MCP client is configured with
``--directory <repo>``, so the code that gets served is **whatever is checked
out**, and the version string stays ``4.24.3`` across every commit we make.
Two processes can therefore be running materially different code and agree
completely about their versions, which is how a daemon carrying last week's
code keeps serving a frontend started from a tree with today's fix in it.

So when the package sits inside a git repository, publish the commit as well,
and let the election compare *that*. Outside a repository this resolves to the
empty string and nothing changes: an installed copy has no commit to speak of
and the version comparison remains the whole story.

**A commit, deliberately, and not the contents of the tree.** An uncommitted
edit is not a push and does not evict a running daemon; if it did, every save
while working on this code would tear the shared browser down underneath
whoever was using it. The rule is "the latest code pushed to our branch wins",
so the unit is the commit.
"""

from __future__ import annotations

import logging
import subprocess
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

#: Long enough that a collision is not a practical concern, short enough to
#: read in a log line next to a version.
_ABBREV = 12

#: git is local and answering from an index it already has. A second is
#: generous; the point of the bound is that a hung git can never become a hung
#: server launch.
_TIMEOUT_SECONDS = 1.0


def _repository_root() -> Path:
    """The directory the package lives in, which is the repo when run from one."""
    return Path(__file__).resolve().parent.parent


def _git(*args: str) -> subprocess.CompletedProcess[str] | None:
    """Run a read-only git command in the package's directory, or give up.

    Every failure mode is the same answer: no revision. git missing, the path
    not being a repository, a timeout, a permissions problem on the object
    store. None of those mean "replace the daemon", and treating any of them as
    an error the caller must handle would put a git dependency in the launch
    path of a server that does not otherwise need one.
    """
    try:
        return subprocess.run(
            ["git", "-C", str(_repository_root()), *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("git %s did not run (%s); treating as no revision", args, exc)
        return None


@lru_cache(maxsize=1)
def current_revision() -> str:
    """This process's commit, or "" when it is not running from a checkout.

    Cached: the answer cannot change inside one process. A running server keeps
    serving the code it imported at start, whatever the working tree does
    afterwards, so re-reading HEAD later would report a commit this process is
    not actually running -- which is precisely the lie this module exists to
    stop telling.
    """
    result = _git("rev-parse", f"--short={_ABBREV}", "HEAD")
    if result is None or result.returncode != 0:
        return ""
    return result.stdout.strip()


def is_descendant(*, candidate: str, ancestor: str) -> bool:
    """True when *candidate* is strictly later than *ancestor* on this history.

    Ancestry rather than "different", because "different" is not an ordering
    and two frontends on two diverged commits would each read the other as
    stale and evict it, forever. Requiring a descendant makes the relation
    one-way: only a commit that genuinely builds on the owner's can displace
    it, and anything diverged, unrelated, or unknown loses the comparison and
    attaches instead.
    """
    if not candidate or not ancestor or candidate == ancestor:
        return False
    result = _git("merge-base", "--is-ancestor", ancestor, candidate)
    if result is None:
        return False
    # 0 = ancestor, 1 = not. Anything else is git failing to answer (an unknown
    # commit, a broken object store), which is not evidence of staleness.
    return result.returncode == 0
