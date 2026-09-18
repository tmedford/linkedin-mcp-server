"""Whether a frontend may be served by the owner it found, version-wise.

``@latest`` is the documented install (README.md), so a machine can easily hold
two versions at once: a long-lived owner from last week and a frontend that was
downloaded a minute ago. Something has to give, and both obvious answers are
wrong on their own.

*Attach anyway* is what the shipped code did, because ``package_version`` was
published and never compared. A months-old owner then serves its own tools to
every new frontend indefinitely, which is exactly the "a pinned version quietly
rots" failure the README warns users about — except invisible, because the user
did update.

*Refuse* wedges the client. A frontend that declines an incompatible owner and
stops has no way forward: it cannot take the lock, so it cannot start a
replacement, and the owner it refused will still be there next time.

So the rule is neither. A **newer** frontend asks the owner to stand down and
elects a replacement; an **older** one attaches, because the owner it found is
at least as new as it is and downgrading a shared browser to satisfy one stale
client would be the worse trade. Same version, obviously, attaches.

Since this fork is served out of a working tree rather than installed, the
version string cannot distinguish two commits and the comparison prefers the
commit whenever both sides publish one. See ``source_revision``.

``protocol_version`` stays a separate and stricter thing (``daemon_descriptor``
enforces equality on it). That is for the wire contract, where disagreement means
two processes cannot talk. This is for behaviour, where they can talk perfectly
well and one of them is simply out of date.
"""

from __future__ import annotations

import enum
import logging

from packaging.version import InvalidVersion, Version

from linkedin_mcp_server import source_revision

logger = logging.getLogger(__name__)


class Skew(enum.Enum):
    """How a published owner's version relates to this frontend's."""

    #: Same version, or the owner is newer. Attach.
    SERVICEABLE = "serviceable"

    #: This frontend is newer. Ask the owner to stand down, then elect.
    OWNER_IS_STALE = "owner_is_stale"


def compare(
    *,
    owner: str,
    frontend: str,
    owner_revision: str = "",
    frontend_revision: str = "",
) -> Skew:
    """Say whether *frontend* should be served by an owner running *owner*.

    **The commit decides when both sides have one.** This fork is served
    straight out of a working tree, so ``package_version`` is the same string
    on every commit we make and cannot tell a daemon carrying last week's code
    from a frontend started with today's. Where a revision is published on both
    sides, the question becomes the one actually being asked -- is this
    frontend running later code than the owner -- and the version is not
    consulted at all.

    Only a **descendant** displaces the owner. Two frontends on diverged
    commits would otherwise each read the other as stale and evict it in turn,
    and a shared browser cannot survive that. Diverged, unrelated or unknown
    loses the comparison and attaches.

    Without revisions on both sides this is the version comparison it always
    was, which is what an ordinary installed copy gets. An unparseable version
    on either side is treated as serviceable: both come from installed package
    metadata, so a value neither ``packaging`` nor this understands is a local
    build or an editable install rather than a skew, and turning that into a
    forced restart on every single launch would make the daemon useless exactly
    where it is being worked on.
    """
    if owner_revision and frontend_revision:
        if source_revision.is_descendant(
            candidate=frontend_revision, ancestor=owner_revision
        ):
            logger.debug(
                "Frontend commit %s builds on the owner's %s; owner is stale",
                frontend_revision,
                owner_revision,
            )
            return Skew.OWNER_IS_STALE
        return Skew.SERVICEABLE

    try:
        published, ours = Version(owner), Version(frontend)
    except InvalidVersion:
        logger.debug(
            "Cannot compare daemon versions (%s against %s); attaching", owner, frontend
        )
        return Skew.SERVICEABLE

    if ours > published:
        return Skew.OWNER_IS_STALE
    return Skew.SERVICEABLE
