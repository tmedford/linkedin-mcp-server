"""Reply inside an existing messaging thread through LinkedIn's own API.

``send_message`` reaches a composer through the recipient's profile. That path
is absent for a member who can only be reached through an existing InMail or
Open Profile thread, and where it does exist it may open a separate DM rather
than continue the conversation the caller is looking at. This names the thread
instead: the caller hands back the id (or ``thread_url``) a read returned, and
the reply goes there or nowhere.

**This is a write through the API, and that is this fork's decision.**
Upstream's ``message_sender.py`` says sending must not be moved to a private
endpoint, and upstream's sender is left exactly as written. The fork's tools
all read the API the web client calls rather than the page it paints, and on
2026-10-02 the page-driven version of this tool showed why that extends to
writes: over three live sends the composer lost focus twice after the text was
in, which refused the send, and the one that went through was reported as
unconfirmed although it had been delivered.

**What is measured and what is not** (all on 2026-10-02, one account):

``/voyager/api/me``
    Carries the signed-in member as ``included[].dashEntityUrn``, which is the
    mailbox every conversation URN is built on. Measured.

``messengerMessages`` GraphQL query
    What the thread page itself issues to load a thread, keyed by conversation
    URN. Measured, including that reading it does not mark the thread read. Its
    ``queryId`` is a persisted hash LinkedIn rotates, so it is used only to
    show a dry run what it is about to reply to, and a failure there is
    reported rather than raised.

``createMessage``
    The write. Its body here was built from the endpoint's known shape rather
    than from a recording, and the first live send was the measurement: HTTP
    200, answering with the created message's URN and delivery time, and the
    message then read back once in the thread with its line breaks intact.

**Sent means the server said so.** The response to the write carries the
message it created, with its URN and delivery time. That is first-hand, which
no reading of the rendered thread ever was.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    LinkedInOperationError,
    RateLimitError,
)
from linkedin_mcp_server.voyager.client import VoyagerReader
from linkedin_mcp_server.voyager.thread import (
    conversation_urn as build_conversation_urn,
    is_thread_id,
    iso,
    messages_query_url,
    normalize_thread_reference,
    parse_thread,
    thread_url,
)

logger = logging.getLogger(__name__)

#: The write. A plain REST action, so unlike a GraphQL query there is no hash
#: to rotate.
_CREATE_MESSAGE = (
    "https://www.linkedin.com/voyager/api/"
    "voyagerMessagingDashMessengerMessages?action=createMessage"
)

_RETRY_WARNING = (
    "Check the conversation before retrying; retrying may deliver the reply twice."
)


def _origin_token() -> str:
    """The client-generated handle for one message, fresh per send."""
    return str(uuid.uuid4())


def _tracking_id() -> str:
    """Sixteen random bytes as the web client sends them: one character each."""
    return os.urandom(16).decode("latin-1")


def _thread_reply_result(
    url: str,
    thread_id: str,
    status: str,
    message: str,
    *,
    thread_verified: bool = False,
    sent: bool = False,
    retry_safe: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    """``send_message``'s result shape, plus the thread it was pinned to."""
    # The keys of upstream's `contracts.message_action_result`, spelled out
    # rather than called: `scraping` imports this package to build the facade,
    # so importing it back from here is a cycle.
    return {
        "url": url,
        "status": status,
        "message": message,
        "recipient_selected": thread_verified,
        "sent": sent,
        "retry_safe": retry_safe,
        "thread_id": thread_id,
        **extra,
    }


def refuse_an_invalid_reply(thread_id: str, message: str) -> dict[str, Any] | None:
    """The browser-free refusal for a reply that could never be sent.

    Raises ``InvalidReferenceError`` for a ``thread_id`` that is not an id at
    all, the way every other id argument does.
    """
    thread_id = normalize_thread_reference(thread_id)
    status = "invalid_message"
    reason = None
    if not is_thread_id(thread_id):
        # A thread URN passes the generic id check and is not an id. Built into
        # a conversation URN it would name a conversation that does not exist.
        status = "invalid_thread"
        reason = (
            "thread_id is not a messaging thread id. Pass the thread id or "
            "thread_url exactly as a previous result returned it."
        )
    elif not message.strip():
        reason = "Message must contain non-whitespace characters."
    elif any(
        (ord(character) < 32 and character != "\n") or ord(character) == 127
        for character in message
    ):
        # Line breaks are text here. send_message rejects them because it
        # types into a composer where Enter submits; an API body has no such
        # hazard. Every other control character is still refused.
        reason = "Message must not contain control characters other than line breaks."
    if reason is None:
        return None
    return _thread_reply_result(thread_url(thread_id), thread_id, status, reason)


class VoyagerThreadReply(VoyagerReader):
    """Send a reply into one named thread through the messaging API."""

    surface = "thread-reply"

    async def _thread_preview(self, conversation_urn: str) -> dict[str, Any]:
        """What the thread currently holds, so a dry run shows its target.

        Best effort by design: the query id rotates, and a dry run that raised
        on a stale hash would block replies the write itself can still make.
        """
        page = self._session.page
        try:
            payload = await self._fetch(messages_query_url(page, conversation_urn))
        except (AuthenticationError, RateLimitError):
            raise
        except LinkedInOperationError as exc:
            logger.info("Thread preview unavailable: %s", exc)
            return {"thread_readable": None}

        mailbox_urn = conversation_urn[len("urn:li:msg_conversation:(") :].split(",")[0]
        messages, participants, _found = parse_thread(payload, mailbox_urn)
        if not messages:
            # A conversation URN that names nothing still answers 200. That is
            # the thread not existing for this mailbox, and it is said so.
            return {"thread_readable": False}
        last = messages[-1]
        return {
            "thread_readable": True,
            "participants": [person["name"] for person in participants],
            "last_message_text": last["text"],
            "last_message_at": last["delivered_at_iso"],
        }

    async def reply_to_thread(
        self, thread_id: str, message: str, *, confirm_send: bool
    ) -> dict[str, Any]:
        """Reply in an existing thread with explicit confirmation gating.

        Args:
            thread_id: The thread id, or the ``/messaging/thread/{id}/`` URL a
                previous result returned.
            message: The reply text.
            confirm_send: Must be True to actually send. False reads who the
                signed-in member is and what the thread holds, and writes
                nothing.
        """
        refusal = refuse_an_invalid_reply(thread_id, message)
        if refusal is not None:
            return refusal
        thread_id = normalize_thread_reference(thread_id)
        url = thread_url(thread_id)

        mailbox_urn = await self._mailbox_urn()
        conversation_urn = build_conversation_urn(mailbox_urn, thread_id)

        if not confirm_send:
            preview = await self._thread_preview(conversation_urn)
            if preview.get("thread_readable") is False:
                return _thread_reply_result(
                    url,
                    thread_id,
                    "thread_not_found",
                    "No messages were found in that thread for the signed-in "
                    "member. Nothing was sent.",
                    **preview,
                )
            return _thread_reply_result(
                url,
                thread_id,
                "confirmation_required",
                "Set confirm_send=true to send the reply.",
                thread_verified=preview.get("thread_readable") is True,
                **preview,
            )

        body = {
            "message": {
                "body": {"attributes": [], "text": message},
                "renderContentUnions": [],
                "conversationUrn": conversation_urn,
                # A fresh token per send, as the web client issues. It is the
                # server's handle on this one message, so reusing one would
                # tie two different sends together.
                "originToken": _origin_token(),
            },
            "mailboxUrn": mailbox_urn,
            "trackingId": _tracking_id(),
            "dedupeByClientGeneratedToken": False,
        }
        outcome = await self._create_message(body)
        landed_in = outcome.pop("conversation_urn", None)
        if outcome["status"] == "sent" and landed_in not in (None, conversation_urn):
            # Delivered, and not where it was asked to go. Never reported as a
            # plain success: the caller has to know where it went.
            return _thread_reply_result(
                url,
                thread_id,
                "sent_to_other_thread",
                "LinkedIn delivered the reply to a different conversation than "
                "the one requested.",
                sent=True,
                retry_safe=False,
                message_urn=outcome["message_urn"],
                delivered_to=landed_in,
            )
        return _thread_reply_result(
            url,
            thread_id,
            outcome.pop("status"),
            outcome.pop("message"),
            thread_verified=outcome["sent"],
            **outcome,
        )

    async def _create_message(self, body: dict[str, Any]) -> dict[str, Any]:
        """Issue the write and say what the server's answer means.

        Returns ``status``, ``message``, ``sent`` and ``retry_safe``, plus
        whatever the answer carried. Shared by every sender so a status is
        read one way: a refusal is retryable, an answer that does not name the
        created message is unconfirmed, and only a named message is sent.
        """
        try:
            status, text = await self._post(_CREATE_MESSAGE, body)
        except AuthenticationError:
            # Raised only when the request provably never left the page.
            raise
        except Exception:
            # The request may have left before the round trip failed.
            logger.debug("Message request did not complete", exc_info=True)
            return {
                "status": "send_unconfirmed",
                "message": "The request was interrupted and LinkedIn's answer "
                f"was lost. {_RETRY_WARNING}",
                "sent": False,
                "retry_safe": False,
            }

        if status in (401, 403):
            raise AuthenticationError(
                f"Voyager {self.surface} request rejected: HTTP {status}"
            )
        if status == 429:
            raise RateLimitError(
                f"Voyager {self.surface} request rate limited: HTTP {status}"
            )
        if 400 <= status < 500:
            # The server understood the request and refused it, so nothing was
            # delivered and the same call can be made again once corrected.
            return {
                "status": "send_rejected",
                "message": f"LinkedIn refused the message (HTTP {status}). "
                "Nothing was sent.",
                "sent": False,
                "retry_safe": True,
                "http_status": status,
                "response_excerpt": text[:300],
            }
        if not 200 <= status < 300:
            return {
                "status": "send_unconfirmed",
                "message": f"LinkedIn answered HTTP {status}, which does not say "
                f"whether the message was delivered. {_RETRY_WARNING}",
                "sent": False,
                "retry_safe": False,
                "http_status": status,
            }

        try:
            created = (json.loads(text) or {}).get("value") or {}
        except ValueError:
            created = {}
        message_urn = created.get("entityUrn")
        if not isinstance(message_urn, str) or not message_urn:
            return {
                "status": "send_unconfirmed",
                "message": f"LinkedIn accepted the request (HTTP {status}) but "
                f"did not return the message it created. {_RETRY_WARNING}",
                "sent": False,
                "retry_safe": False,
                "http_status": status,
            }
        landed_in = created.get("conversationUrn")
        return {
            "status": "sent",
            "message": "Delivered; LinkedIn returned the message it created.",
            "sent": True,
            "retry_safe": False,
            "message_urn": message_urn,
            "delivered_at": iso(created.get("deliveredAt")),
            "conversation_urn": landed_in if isinstance(landed_in, str) else None,
        }
