"""Read one messaging thread through the query the thread page itself issues.

Upstream's ``get_conversation`` opens the thread and reads what LinkedIn
painted. Opening a thread marks it read, so asking what somebody wrote costs
the unread state that said they were still owed an answer, and what comes back
is one block of rendered text with the page's chrome in it.

The thread page loads its messages from ``voyagerMessagingGraphQL`` with a
``messengerMessages`` query keyed by the conversation URN. Issuing that same
request returns each message as a record: who sent it, when, and its text.

**Measured on 2026-10-02, one account.** Reading a thread this way left it
unread: a thread with one unread message was read twice and was still unread
in the conversation list afterwards. The rows are at
``data.data.messengerMessagesBySyncToken['*elements']`` with the entities in
``included``, and sender names come from ``MessagingParticipant`` entities in
the same payload.

**The query id is a persisted hash and LinkedIn rotates it.** It cannot be
synthesised. The one observed is tried first because it costs nothing. When it
stops working the id is observed again by loading the messaging page and
watching the request go by: measured the same day, ``/messaging/`` redirects to
the most recent conversation and issues this query for it, so the id can be
taken without opening the thread that was asked about. That is the same page
load ``get_conversations`` already makes to find its own query. The result
says when it happened (``query_id_renewed``).

**``participants`` is who wrote, not who belongs.** The payload carries a
``MessagingParticipant`` only for the senders of the messages it returns. A new
group thread in which only the signed-in member had written came back with no
other participants, while ``get_conversations`` listed both members.

**Measured across 40 threads: the most that came back was 20 messages.**

**Not measured: paging back.** This is the query the page issues on load, which
returns the most recent messages. Older ones arrive through a different query
when the page is scrolled, and that one has not been observed. A long thread
is therefore returned as its recent tail, and ``count`` is how many came back.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    LinkedInOperationError,
    RateLimitError,
)
from linkedin_mcp_server.voyager.client import VoyagerReader, person_identifier

logger = logging.getLogger(__name__)

MESSAGING_URL = "https://www.linkedin.com/messaging/"

_GRAPHQL = "https://www.linkedin.com/voyager/api/voyagerMessagingGraphQL/graphql"

#: Observed on 2026-10-02. Tried first; see the module docstring for what
#: happens when it has rotated.
PINNED_QUERY_ID = "messengerMessages.5846eeb71c981f11e0134cb6626cc314"

#: A thread id is base64url with its padding kept literal, the alphabet
#: upstream's ``_MESSAGE_THREAD_PATH_RE`` admits for the same segment.
_THREAD_ID_RE = re.compile(r"[A-Za-z0-9_=-]+")

_QUERY_ID_RE = re.compile(r"[?&]queryId=(messengerMessages\.[0-9a-f]+)")

#: Where the rows live, named because getting it wrong returns a clean zero.
_ELEMENTS_PATH = "data.data.messengerMessagesBySyncToken['*elements']"

# A discovered id outlives one tool call and not one browser, for the reason
# given beside `_QUERY_CACHE` in `messaging.py`: a reader is built per call, and
# the page the id was observed on is what bounds how long it can be trusted.
_QUERY_ID_CACHE: tuple[Any, str] | None = None


def forget_cached_query_id() -> None:
    """Drop a discovered query id so the next read starts from the pinned one."""
    global _QUERY_ID_CACHE
    _QUERY_ID_CACHE = None


def _known_query_id(page: Any) -> str:
    if _QUERY_ID_CACHE is not None and _QUERY_ID_CACHE[0] is page:
        return _QUERY_ID_CACHE[1]
    return PINNED_QUERY_ID


def normalize_thread_reference(value: str) -> str:
    """The thread id from an id or from a reference to one.

    Imported here rather than at module top: ``scraping`` imports this package
    to compose the facade, so a top-level import the other way is a cycle for
    whoever imports this module first.
    """
    from linkedin_mcp_server.linkedin.identifiers import normalize_thread_id

    return normalize_thread_id(value)


def thread_url(thread_id: str) -> str:
    """The thread's address, with base64 padding kept literal as LinkedIn writes it."""
    return f"https://www.linkedin.com/messaging/thread/{quote(thread_id, safe='=')}/"


def is_thread_id(thread_id: str) -> bool:
    """Whether a normalized id is a thread id and not, say, a thread URN.

    A URN passes the generic id check. Wrapped in a conversation URN it would
    name a conversation that does not exist, and that reads back as empty.
    """
    return bool(_THREAD_ID_RE.fullmatch(thread_id))


def conversation_urn(mailbox_urn: str, thread_id: str) -> str:
    return f"urn:li:msg_conversation:({mailbox_urn},{thread_id})"


def messages_query_url(page: Any, urn: str) -> str:
    """The messages request for one conversation, with the best id known."""
    return (
        f"{_GRAPHQL}?queryId={_known_query_id(page)}"
        f"&variables=(conversationUrn:{quote(urn, safe='')})"
    )


def iso(milliseconds: Any) -> str | None:
    """LinkedIn epoch milliseconds to ISO 8601 UTC, or None if unusable."""
    if not isinstance(milliseconds, (int, float)) or milliseconds <= 0:
        return None
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


def parse_thread(
    payload: dict[str, Any], mailbox_urn: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Messages oldest first, the other participants, and whether rows were found.

    The third value is whether the row container exists at all, which is what
    separates an empty thread from a response whose shape has moved.
    """
    # WRAPPED, like every GraphQL answer here: data.data, not data. Reading one
    # level short finds no container, and on an empty thread that turned a real
    # zero into a "changed shape" error. Caught against a saved live payload.
    inner = (payload.get("data") or {}).get("data") or {}
    container = inner.get("messengerMessagesBySyncToken")
    found = VoyagerReader._has_rows_key(container)
    included = [e for e in payload.get("included") or [] if isinstance(e, dict)]

    people: dict[str, dict[str, Any]] = {}
    for entity in included:
        if not str(entity.get("$type", "")).endswith(".MessagingParticipant"):
            continue
        member = (entity.get("participantType") or {}).get("member") or {}
        first = (member.get("firstName") or {}).get("text") or ""
        last = (member.get("lastName") or {}).get("text") or ""
        people[entity.get("entityUrn", "")] = {
            "name": f"{first} {last}".strip(),
            "profile_urn": entity.get("hostIdentityUrn") or "",
            "profile_url": member.get("profileUrl") or "",
            # Pass this as linkedin_username to any person tool.
            "public_identifier": person_identifier(
                member.get("profileUrl"), entity.get("hostIdentityUrn")
            ),
        }

    messages = []
    for entity in included:
        if not str(entity.get("$type", "")).endswith(".Message"):
            continue
        sender = str(entity.get("*sender") or "")
        messages.append(
            {
                "message_urn": entity.get("entityUrn"),
                "sender_name": (people.get(sender) or {}).get("name") or None,
                # None when the sender is not named in the payload: a wrong
                # "they wrote this" is worse than an admitted unknown.
                "from_me": (mailbox_urn in sender) if sender else None,
                "delivered_at": entity.get("deliveredAt"),
                "delivered_at_iso": iso(entity.get("deliveredAt")),
                "subject": entity.get("subject"),
                "text": (entity.get("body") or {}).get("text") or "",
            }
        )
    messages.sort(key=lambda message: message["delivered_at"] or 0)

    others = [
        person
        for urn, person in people.items()
        if mailbox_urn not in urn and person["name"]
    ]
    return messages, others, found


class VoyagerThreadReader(VoyagerReader):
    """Read one thread's messages without rendering it."""

    surface = "thread"

    async def _discover_query_id(self) -> str | None:
        """Load the messaging page and take the query id off its own request."""
        global _QUERY_ID_CACHE
        page = self._session.page
        seen: list[str] = []

        def _capture(request: Any) -> None:
            match = _QUERY_ID_RE.search(request.url)
            if match:
                seen.append(match.group(1))

        page.on("request", _capture)
        try:
            await self._navigator._navigate_to_page(MESSAGING_URL)
            await self._session.check_rate_limit()
            for _ in range(10):
                if seen:
                    break
                await self._session.delay(1.0)
        finally:
            page.remove_listener("request", _capture)
        if not seen:
            return None
        _QUERY_ID_CACHE = (page, seen[-1])
        return seen[-1]

    async def _threads_with(self, linkedin_username: str) -> list[dict[str, Any]]:
        """The conversations shared with one member, most recent first.

        There is no measured query from a member to their threads, so this
        searches messages for the member's name and keeps the conversations
        that member is actually in, matched by profile URN and not by the name
        that was searched. One-to-one threads sort ahead of group threads,
        because "my conversation with X" means the one with only X in it.
        """
        from linkedin_mcp_server.linkedin.identifiers import (
            normalize_person_identifier,
        )
        from linkedin_mcp_server.voyager.message_search import VoyagerMessageSearch

        member = await self._resolve_member(
            normalize_person_identifier(linkedin_username)
        )
        if not member["name"]:
            return []
        search = VoyagerMessageSearch(self._session, self._navigator)
        found = await search.search_messages(member["name"])
        rows = [
            row
            for row in found["conversations"]
            if member["urn"]
            in [person.get("profile_urn") for person in row.get("people") or []]
        ]
        return sorted(
            rows,
            key=lambda row: (
                bool(row.get("group_chat")),
                -(row.get("last_activity_at") or 0),
            ),
        )

    async def get_thread(
        self,
        thread_id: str | None = None,
        linkedin_username: str | None = None,
        index: int = 0,
    ) -> dict[str, Any]:
        """Read the recent messages of one thread, named by id or by person.

        Returns ``url`` and ``sections`` for generic consumers, plus
        ``thread_id``, ``thread_urn``, ``participants``, ``messages`` (oldest
        first), ``count`` and ``query_id_renewed``.
        """
        if not thread_id and not linkedin_username:
            raise LinkedInOperationError(
                "Provide at least one of linkedin_username or thread_id"
            )
        if not thread_id:
            if index < 0:
                raise LinkedInOperationError(f"index must be >= 0, got {index}.")
            threads = await self._threads_with(linkedin_username or "")
            if index >= len(threads):
                raise LinkedInOperationError(
                    f"Could not find a conversation for {linkedin_username} at "
                    f"index {index}: a message search on their name found "
                    f"{len(threads)} conversation(s) they are in. A thread the "
                    "search does not surface can still exist; find it in "
                    "get_conversations and pass its thread_url as thread_id."
                )
            thread_id = threads[index].get("thread_url") or ""
        thread_id = normalize_thread_reference(thread_id)
        if not is_thread_id(thread_id):
            raise LinkedInOperationError(
                "thread_id is not a messaging thread id. Pass the thread id or "
                "thread_url exactly as a previous result returned it."
            )
        page = self._session.page
        mailbox_urn = await self._mailbox_urn()
        urn = conversation_urn(mailbox_urn, thread_id)

        renewed = False
        try:
            payload = await self._fetch(messages_query_url(page, urn))
        except (AuthenticationError, RateLimitError):
            raise
        except LinkedInOperationError as exc:
            logger.info("Thread query failed (%s); observing the query id again", exc)
            renewed = True
            if await self._discover_query_id() is None:
                raise LinkedInOperationError(
                    "Voyager thread request failed and no messengerMessages "
                    "request was observed on the messaging page, so the query "
                    "id could not be renewed."
                ) from exc
            payload = await self._fetch(messages_query_url(page, urn))

        messages, participants, found = parse_thread(payload, mailbox_urn)
        self._refuse_unexplained_zero(
            rows=messages,
            payload=payload,
            path=_ELEMENTS_PATH,
            container_found=found,
        )

        lines = []
        for message in messages:
            who = "You" if message["from_me"] else (message["sender_name"] or "?")
            lines.append(f"{who} - {message['delivered_at_iso']}\n{message['text']}")
        return {
            "url": thread_url(thread_id),
            # Keyed as upstream keys it, so a consumer reading the
            # section by name keeps working.
            "sections": {"conversation": "\n\n".join(lines)},
            "thread_id": thread_id,
            "thread_urn": urn,
            "participants": participants,
            "messages": messages,
            "count": len(messages),
            "query_id_renewed": renewed,
        }
