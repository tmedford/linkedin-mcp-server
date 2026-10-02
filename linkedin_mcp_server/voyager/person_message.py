"""Message a person through the messaging API, by their profile.

Upstream's ``send_message`` opens the recipient's profile, follows its Message
action to a composer and types. On 2026-10-02 that failed outright for a
first-degree connection whose profile did not expose one unambiguous Message
action (``recipient_resolution_failed``), which leaves no way to start that
conversation at all.

This resolves the member from their public identifier and posts to the same
``createMessage`` action ``reply_to_thread`` uses, addressed to a person
instead of a conversation.

**Measured on 2026-10-02:** ``/voyager/api/identity/dash/profiles`` with
``q=memberIdentity&memberIdentity=<public id>`` answers with exactly one
profile URN at ``data['*elements']`` and the profile entity in ``included``.
It is a plain REST finder, so there is no query id to rotate.

**Not yet measured: the write addressed to a person.** The body is the
``createMessage`` body with ``hostRecipientUrns`` in place of
``conversationUrn``, built from the endpoint's known shape. Until one live send
proves it, upstream's ``send_message`` stays served beside this tool. Where
the message lands is not assumed either: the server's answer names the
conversation, and that is what is returned as ``thread_id``.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.voyager.thread import thread_url
from linkedin_mcp_server.voyager import thread_reply
from linkedin_mcp_server.voyager.thread_reply import VoyagerThreadReply

logger = logging.getLogger(__name__)

_PROFILES = "https://www.linkedin.com/voyager/api/identity/dash/profiles"
_PROFILE_URN_PREFIX = "urn:li:fsd_profile:"
_ELEMENTS_PATH = "data['*elements']"
_THREAD_IN_URN = re.compile(r",([A-Za-z0-9_=-]+)\)$")


def _result(url: str, status: str, message: str, **extra: Any) -> dict[str, Any]:
    """``send_message``'s result keys, in that order, plus what this tool knows."""
    base: dict[str, Any] = {
        "url": url,
        "status": status,
        "message": message,
        "recipient_selected": False,
        "sent": False,
        "retry_safe": True,
    }
    base.update(extra)
    return base


def refuse_an_invalid_person_message(
    linkedin_username: str, message: str
) -> dict[str, Any] | None:
    """The browser-free refusal for a message that could never be sent.

    Raises ``InvalidReferenceError`` for a username that is not one.
    """
    from linkedin_mcp_server.scraping.identifiers import (
        normalize_person_identifier,
        person_profile_url,
    )

    url = person_profile_url(normalize_person_identifier(linkedin_username), "/")
    if not message.strip():
        reason = "Message must contain non-whitespace characters."
    elif any(
        (ord(character) < 32 and character != "\n") or ord(character) == 127
        for character in message
    ):
        reason = "Message must not contain control characters other than line breaks."
    else:
        return None
    return _result(url, "invalid_message", reason)


class VoyagerPersonMessage(VoyagerThreadReply):
    """Send a message to a member named by their public identifier."""

    surface = "person-message"

    async def _recipient(self, username: str) -> dict[str, Any]:
        """Resolve one member, or raise. Never a best guess among several."""
        payload = await self._fetch(
            f"{_PROFILES}?q=memberIdentity&memberIdentity={quote(username, safe='')}"
        )
        data = payload.get("data") or {}
        found = self._has_rows_key(data)
        urns = [
            urn
            for urn in (data.get("*elements") or [] if found else [])
            if isinstance(urn, str) and urn.startswith(_PROFILE_URN_PREFIX)
        ]
        self._refuse_unexplained_zero(
            rows=urns, payload=payload, path=_ELEMENTS_PATH, container_found=found
        )
        if len(urns) != 1:
            raise LinkedInScraperException(
                f"Voyager {self.surface} found {len(urns)} members for "
                f"{username!r}, not exactly one. Pass the /in/ public "
                "identifier exactly as a profile URL shows it."
            )
        entity = self._by_urn(payload).get(urns[0]) or {}
        name = " ".join(
            part for part in (entity.get("firstName"), entity.get("lastName")) if part
        )
        return {"urn": urns[0], "name": name or None}

    async def message_person(
        self, linkedin_username: str, message: str, *, confirm_send: bool
    ) -> dict[str, Any]:
        """Send a message to a person with explicit confirmation gating."""
        from linkedin_mcp_server.scraping.identifiers import (
            normalize_person_identifier,
            person_profile_url,
        )

        refusal = refuse_an_invalid_person_message(linkedin_username, message)
        if refusal is not None:
            return refusal
        username = normalize_person_identifier(linkedin_username)
        url = person_profile_url(username, "/")

        recipient = await self._recipient(username)
        mailbox_urn = await self._mailbox_urn()
        if recipient["urn"] == mailbox_urn:
            return _result(
                url,
                "recipient_is_sender",
                "That identifier is the signed-in member. Nothing was sent.",
            )
        who = {"recipient_urn": recipient["urn"], "recipient_name": recipient["name"]}

        if not confirm_send:
            return _result(
                url,
                "confirmation_required",
                "Set confirm_send=true to send the message.",
                recipient_selected=True,
                **who,
            )

        body = {
            "message": {
                "body": {"attributes": [], "text": message},
                "renderContentUnions": [],
                "originToken": thread_reply._origin_token(),
            },
            "mailboxUrn": mailbox_urn,
            "trackingId": thread_reply._tracking_id(),
            "dedupeByClientGeneratedToken": False,
            "hostRecipientUrns": [recipient["urn"]],
        }
        outcome = await self._create_message(body)
        landed_in = outcome.pop("conversation_urn", None)
        extra: dict[str, Any] = dict(who)
        if outcome["sent"] and isinstance(landed_in, str):
            match = _THREAD_IN_URN.search(landed_in)
            extra["thread_urn"] = landed_in
            if match:
                # Hand back what reply_to_thread and get_thread take, so the
                # conversation can be continued without looking it up again.
                extra["thread_id"] = match.group(1)
                extra["thread_url"] = thread_url(match.group(1))
        return _result(
            url,
            outcome.pop("status"),
            outcome.pop("message"),
            recipient_selected=True,
            **{**outcome, **extra},
        )
