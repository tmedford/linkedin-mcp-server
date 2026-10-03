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

#: Where the rows live. The board was first measured wrapped (``data.data``,
#: rows inline). Re-measured 2026-10-02 both boards answered NORMALIZED: rows
#: at ``data['*elements']`` as ids of views in ``included``, each view
#: pointing (``*invitation``) at an Invitation that points at MiniProfiles.
#: Reading only the old depth refused every call as a shape change, and
#: reading the new depth while keeping only inline rows returned zero against
#: 15 sent and 31 received. Both shapes are read; nothing else is.
_ELEMENTS_PATHS = (("data", "data"), ("data",))
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
            path="data['*elements'] (or data.data['*elements'])",
            container_found=container_found,
        )

        by_urn = self._by_urn(payload)
        invitations = [self._normalize(row, by_urn, direction) for row in rows]

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
        by_urn = {
            entity.get("entityUrn"): entity
            for entity in payload.get("included") or []
            if isinstance(entity, dict)
        }
        for path in _ELEMENTS_PATHS:
            inner: Any = payload
            for key in path:
                inner = inner.get(key) if isinstance(inner, dict) else None
            if not isinstance(inner, dict):
                continue
            if "*elements" not in inner and "elements" not in inner:
                continue
            # An empty board answers with ``elements: []`` rather than a
            # pointer list, as on every other collection read here.
            elements = inner.get("*elements") or inner.get("elements") or []
            rows = [
                by_urn.get(element) if isinstance(element, str) else element
                for element in elements
            ]
            return [row for row in rows if isinstance(row, dict)], True
        return [], False

    def _normalize(
        self,
        row: dict[str, Any],
        by_urn: dict[str, dict[str, Any]],
        direction: str = "received",
    ) -> dict[str, Any]:
        """One invitation, flattened to what a decision actually needs."""
        invitation = row.get("invitation") if isinstance(row, dict) else None
        if not isinstance(invitation, dict):
            invitation = by_urn.get(row.get("*invitation") or "")
        invitation = invitation if isinstance(invitation, dict) else row

        message = invitation.get("message")
        note = message if isinstance(message, str) else ""
        # customMessage is a boolean has-note FLAG, never the text. Both are
        # reported: the flag is LinkedIn's own answer and the text is ours, and
        # when they disagree that is worth seeing rather than silently
        # preferring one.
        has_note_flag = invitation.get("customMessage")

        # The member sits inside the Invitation in the normalized shape and
        # beside it in the inline one.
        profile = self._profile_of(invitation, by_urn, direction) or (
            self._profile_of(row, by_urn, direction)
        )
        mutual = next(
            (
                (insight.get("sharedInsight") or {}).get("totalCount")
                for insight in row.get("insights") or []
                if isinstance(insight, dict)
            ),
            None,
        )
        return {
            "invitation_urn": invitation.get("entityUrn"),
            "shared_secret": invitation.get("sharedSecret"),
            "invitation_type": invitation.get("invitationType"),
            # The normalized Invitation has no state field; its type (SENT,
            # PENDING) is the state LinkedIn shows.
            "state": invitation.get("invitationState")
            or invitation.get("state")
            or invitation.get("invitationType"),
            "sent_at_iso": _iso(invitation.get("sentTime")),
            "has_note": bool(has_note_flag) or bool(note),
            "has_note_flag": has_note_flag,
            "note": note,
            "note_length": len(note),
            "name": profile.get("name"),
            "headline": profile.get("headline"),
            "profile_urn": profile.get("urn"),
            # The one id to pass as linkedin_username to any person tool:
            # the slug, or the profile id when there is none.
            "public_identifier": profile.get("slug")
            or person_identifier(None, profile.get("urn")),
            # LinkedIn's count of connections in common, on received rows.
            "mutual_connections": mutual,
        }

    @staticmethod
    def _profile_of(
        row: dict[str, Any],
        by_urn: dict[str, dict[str, Any]],
        direction: str = "received",
    ) -> dict[str, Any]:
        """Resolve the other party, whichever shape this board used.

        Received and sent rows name the counterparty differently, and the
        normalized representation may hand back either an inline object or a
        URN pointing into ``included``. Both are handled rather than assuming
        the shape of whichever board was read first.
        """
        # The OTHER party: who sent it on the received board, who it went to
        # on the sent board. Both members are present on every invitation, so
        # taking the first one found named the signed-in member on every
        # sent row.
        keys = (
            ("toMember", "*toMember")
            if direction == "sent"
            else ("fromMember", "*fromMember")
        )
        candidate: Any = None
        for key in keys:
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
            # The profile URN every other tool returns, not the mini one.
            "urn": candidate.get("dashEntityUrn") or candidate.get("entityUrn"),
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
                f"{item.get('name') or 'Unknown'} ({item.get('public_identifier') or '?'})"
                f" — {item.get('headline') or 'no headline'}"
                f" — {item.get('state') or 'unknown state'}"
                f" — sent {item.get('sent_at_iso') or 'unknown'}{note}"
            )
        return "\n".join(lines)
