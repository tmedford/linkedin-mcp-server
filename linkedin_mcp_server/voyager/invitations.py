"""Read the invitation manager from LinkedIn's API instead of its page.

The received board has no upstream tool at all, so the routine that owns it
drives a browser, waits for a lazy-loading list to settle, and reads rows out
of the DOM. That is slow, it fights every markup change, and it cannot answer
"how many are pending" without rendering all of them. This asks the endpoint
the page itself calls.

**Every guard here is a defect that actually happened**, on this surface, and
each is named at the point it applies:

``data.data`` not ``data``
    The response is wrapped. Reading ``data['*elements']`` returns a clean,
    well-formed zero rather than raising, and on this surface a zero means "the
    board is empty, nothing to do". Six live invitations read as none.

``paging.total`` is a lie
    Observed reading 0 against six real rows across four separate runs. The
    only honest count is the length of what parsed.

``customMessage`` is a flag, not the text
    It is a boolean saying whether a note exists; ``message`` carries the note.
    Reproduced cleanly twice: true on exactly the rows with note text, false on
    the bare ones, with message lengths agreeing both ways.

``start``/``count`` really do page
    Controlled: ``start=2&count=2`` returned exactly rows three and four, and
    ``start`` past the end returned empty. So paging is the caller's loop here
    for the same reason it is in :mod:`~linkedin_mcp_server.voyager.messaging`
    -- only the caller knows when to stop.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.voyager.client import VoyagerReader, person_identifier

logger = logging.getLogger(__name__)

#: LinkedIn caps a page well below this, but asking for more than will be
#: returned is how ``at_end`` stays measured rather than inferred.
PAGE_SIZE = 50

#: ``q`` selects the board. Received and sent are different endpoints, not two
#: values of one parameter, which is why they are separate methods here.
_RECEIVED = "https://www.linkedin.com/voyager/api/relationships/invitationViews"
_SENT = "https://www.linkedin.com/voyager/api/relationships/sentInvitationViewsV2"

#: Where the rows live. Named as a constant because getting it wrong is the
#: documented failure and a constant is greppable in a way a literal is not.
_ELEMENTS_PATH = "data.data['*elements']"


def _iso(milliseconds: Any) -> str | None:
    """LinkedIn epoch milliseconds to ISO 8601 UTC, or None if unusable."""
    if not isinstance(milliseconds, (int, float)) or milliseconds <= 0:
        return None
    return (
        datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


class VoyagerInvitationsReader(VoyagerReader):
    """Read pending invitations without rendering the invitation manager."""

    surface = "invitations"

    async def get_invitations(
        self,
        direction: str = "received",
        start: int = 0,
        count: int = PAGE_SIZE,
    ) -> dict[str, Any]:
        """Read one page of the invitation board.

        Returns the same shape as the other readers in this package: ``url``
        and ``sections`` for generic consumers, plus ``invitations``, ``count``,
        ``start``, ``at_end`` and ``zero_reason`` as the structured answer.

        ``at_end`` is measured by comparing what came back against what was
        asked for, never inferred: fewer than ``count`` means the server had no
        more, exactly ``count`` means there may be more, and an empty page
        proves nothing either way and is reported as ``None``.
        """
        if direction not in ("received", "sent"):
            raise LinkedInScraperException(
                f"direction was {direction!r}. Pass 'received' or 'sent'."
            )
        if start < 0 or count < 1:
            raise LinkedInScraperException(
                f"start must be >= 0 and count >= 1, got start={start}, count={count}."
            )

        if direction == "received":
            url = f"{_RECEIVED}?q=receivedInvitation&start={start}&count={count}"
        else:
            url = (
                f"{_SENT}?q=invitationType&invitationType=CONNECTION"
                f"&start={start}&count={count}"
            )

        payload = await self._fetch(url)
        rows, container_found = self._elements(payload)
        self._refuse_unexplained_zero(
            rows=rows,
            payload=payload,
            path=_ELEMENTS_PATH,
            container_found=container_found,
        )

        by_urn = self._by_urn(payload)
        invitations = [self._normalize(row, by_urn) for row in rows]

        # Measured against what was asked for. paging.total is deliberately not
        # consulted: it has read 0 against a full board on every run it was
        # checked, so believing it would turn a populated surface into an empty
        # one, which is the exact failure this module is built around.
        at_end: bool | None
        zero_reason: str | None = None
        if not invitations:
            at_end = None
            zero_reason = "after-start" if start else "empty-page"
        else:
            at_end = len(invitations) < count

        return {
            "url": url,
            "sections": {"invitations": self._as_text(invitations, direction)},
            "invitations": invitations,
            "count": len(invitations),
            "page_size": count,
            "start": start,
            "direction": direction,
            "at_end": at_end,
            "zero_reason": zero_reason,
        }

    @staticmethod
    def _elements(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        """Pull invitation rows out of the WRAPPED payload.

        The wrapping is the whole point: ``payload['data']['data']``, not
        ``payload['data']``. Both are dicts, so the wrong one does not raise --
        it just has no ``*elements`` and yields a clean empty list.

        Returns the rows and **whether the container was found at all**, which
        is what lets an empty board be reported as empty while a missed path is
        reported as a shape change. Without that second value the two are the
        same answer.
        """
        data = payload.get("data")
        if not isinstance(data, dict):
            return [], False
        inner = data.get("data")
        if not isinstance(inner, dict):
            return [], False
        if "*elements" not in inner:
            return [], False
        elements = inner.get("*elements") or []
        return [e for e in elements if isinstance(e, dict)], True

    def _normalize(
        self, row: dict[str, Any], by_urn: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """One invitation, flattened to what a decision actually needs."""
        invitation = row.get("invitation") if isinstance(row, dict) else None
        invitation = invitation if isinstance(invitation, dict) else row

        message = invitation.get("message")
        note = message if isinstance(message, str) else ""
        # customMessage is a boolean has-note FLAG, never the text. Both are
        # reported: the flag is LinkedIn's own answer and the text is ours, and
        # when they disagree that is worth seeing rather than silently
        # preferring one.
        has_note_flag = invitation.get("customMessage")

        profile = self._profile_of(row, by_urn)
        return {
            "invitation_urn": invitation.get("entityUrn"),
            "shared_secret": invitation.get("sharedSecret"),
            "invitation_type": invitation.get("invitationType"),
            "state": invitation.get("invitationState") or invitation.get("state"),
            "sent_at_iso": _iso(invitation.get("sentTime")),
            "has_note": bool(has_note_flag) or bool(note),
            "has_note_flag": has_note_flag,
            "note": note,
            "note_length": len(note),
            "name": profile.get("name"),
            "headline": profile.get("headline"),
            "profile_slug": profile.get("slug"),
            "profile_urn": profile.get("urn"),
            # The name every other tool uses for what to pass as
            # linkedin_username: the slug, or the id when there is none.
            "public_identifier": profile.get("slug")
            or person_identifier(None, profile.get("urn")),
        }

    @staticmethod
    def _profile_of(
        row: dict[str, Any], by_urn: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """Resolve the other party, whichever shape this board used.

        Received and sent rows name the counterparty differently, and the
        normalized representation may hand back either an inline object or a
        URN pointing into ``included``. Both are handled rather than assuming
        the shape of whichever board was read first.
        """
        candidate: Any = None
        for key in ("fromMember", "toMember", "*fromMember", "*toMember"):
            value = row.get(key) if isinstance(row, dict) else None
            if value:
                candidate = value
                break
        if isinstance(candidate, str):
            candidate = by_urn.get(candidate)
        if not isinstance(candidate, dict):
            return {}

        first = (candidate.get("firstName") or "").strip()
        last = (candidate.get("lastName") or "").strip()
        return {
            "name": " ".join(p for p in (first, last) if p) or None,
            "headline": candidate.get("occupation") or candidate.get("headline"),
            "slug": candidate.get("publicIdentifier"),
            "urn": candidate.get("entityUrn"),
        }

    @staticmethod
    def _as_text(invitations: list[dict[str, Any]], direction: str) -> str:
        """The same page as readable text, for consumers that want prose."""
        if not invitations:
            return f"No {direction} invitations on this page."
        lines = [f"{direction.title()} invitations ({len(invitations)})"]
        for item in invitations:
            note = f" — note ({item['note_length']} chars)" if item["has_note"] else ""
            lines.append(
                f"{item.get('name') or 'Unknown'} ({item.get('profile_slug') or '?'})"
                f" — {item.get('headline') or 'no headline'}"
                f" — {item.get('state') or 'unknown state'}"
                f" — sent {item.get('sent_at_iso') or 'unknown'}{note}"
            )
        return "\n".join(lines)
