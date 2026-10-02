"""Search messages by keyword through the query the messaging page issues.

Upstream's ``search_conversations`` types into the messaging page's search box
and reads what it renders. The page opens the first match as it goes, which
marks that thread read, and its text reports "No messages…yet!" above results
that are plainly there, so the rendered answer contradicts itself.

The page gets its results from a ``messengerConversations`` query carrying a
``keywords`` variable. Issuing that request returns the matching conversations
as the same entities the inbox walk returns, so they are normalized by the
same code and come back in the same shape as ``get_conversations`` rows.

**Measured on 2026-10-02, one account.** ``/messaging/?searchTerm=<kw>`` issued
``messengerConversations.<hash>`` with ``categories:List(INBOX,SPAM,ARCHIVE)``,
``count:20``, ``firstDegreeConnections:false``, the mailbox URN and
``keywords:<kw>``, then the same query again with ``nextCursor``. Rows are at
``data.data.messengerConversationsBySearchCriteria['*elements']`` and the
cursor at its ``metadata.nextCursor``. A search with no matches answers with
``elements: []`` in place of ``*elements``. A search with two matches still handed
back a cursor, so a cursor is not evidence of more; ``at_end`` is measured from
the row count.

**Not measured:** whether the message included with each conversation is the
one that matched or simply its most recent. It is reported under the same
``last_message_*`` keys as the inbox walk and should be read as "a message from
this conversation", not as the match.

The query id rotates like every persisted GraphQL id here. The observed one is
tried first; when it fails it is taken off the search page's own request.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    LinkedInScraperException,
    RateLimitError,
)
from linkedin_mcp_server.voyager.messaging import MESSAGING_URL, VoyagerMessagingReader

logger = logging.getLogger(__name__)

_GRAPHQL = "https://www.linkedin.com/voyager/api/voyagerMessagingGraphQL/graphql"

#: Observed on 2026-10-02.
PINNED_QUERY_ID = "messengerConversations.737b27144cf922499202658a5345016f"

#: What the page asks for, and what `at_end` is measured against.
PAGE_SIZE = 20

_QUERY_ID_RE = re.compile(r"[?&]queryId=(messengerConversations\.[0-9a-f]+)")
_ELEMENTS_PATH = "data.data.messengerConversationsBySearchCriteria['*elements']"

# Per page, for the reason given beside `_QUERY_CACHE` in `messaging.py`.
_QUERY_ID_CACHE: tuple[Any, str] | None = None


def forget_cached_query_id() -> None:
    """Drop a discovered query id so the next search starts from the pinned one."""
    global _QUERY_ID_CACHE
    _QUERY_ID_CACHE = None


def _known_query_id(page: Any) -> str:
    if _QUERY_ID_CACHE is not None and _QUERY_ID_CACHE[0] is page:
        return _QUERY_ID_CACHE[1]
    return PINNED_QUERY_ID


class VoyagerMessageSearch(VoyagerMessagingReader):
    """Find conversations by keyword without typing into the search box."""

    surface = "message-search"

    def _search_url(self, mailbox_urn: str, keywords: str, cursor: str | None) -> str:
        # `safe=""` is the point: these values sit inside a Rest.li variables
        # block, where a bare comma, colon or parenthesis is syntax.
        paging = f"nextCursor:{quote(cursor, safe='')}," if cursor else ""
        return (
            f"{_GRAPHQL}?queryId={_known_query_id(self._session.page)}"
            "&variables=(categories:List(INBOX,SPAM,ARCHIVE),"
            f"count:{PAGE_SIZE},firstDegreeConnections:false,"
            f"mailboxUrn:{quote(mailbox_urn, safe='')},"
            f"{paging}keywords:{quote(keywords, safe='')})"
        )

    async def _discover_query_id(self, keywords: str) -> str | None:
        """Load the search page and take the query id off its own request."""
        global _QUERY_ID_CACHE
        page = self._session.page
        seen: list[str] = []

        def _capture(request: Any) -> None:
            # Only the search variant carries `keywords`; the inbox query on
            # the same page has a different id and must not be taken for it.
            if "keywords" not in request.url:
                return
            match = _QUERY_ID_RE.search(request.url)
            if match:
                seen.append(match.group(1))

        page.on("request", _capture)
        try:
            await self._navigator._navigate_to_page(
                f"{MESSAGING_URL}?searchTerm={quote(keywords, safe='')}"
            )
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

    async def search_messages(
        self, keywords: str, cursor: str | None = None
    ) -> dict[str, Any]:
        """Read ONE page of conversations matching ``keywords``."""
        keywords = keywords.strip()
        if not keywords:
            raise LinkedInScraperException(
                "keywords was blank. Pass the word or phrase to search for."
            )
        if cursor is not None and not cursor.strip():
            raise LinkedInScraperException(
                "cursor was blank. OMIT the argument for the first page, or "
                "pass a next_cursor from a previous call."
            )
        cursor = cursor.strip() if cursor else None

        mailbox_urn = await self._mailbox_urn()
        renewed = False
        try:
            payload = await self._fetch(self._search_url(mailbox_urn, keywords, cursor))
        except (AuthenticationError, RateLimitError):
            raise
        except LinkedInScraperException as exc:
            logger.info("Search query failed (%s); observing the query id again", exc)
            renewed = True
            if await self._discover_query_id(keywords) is None:
                raise LinkedInScraperException(
                    "Voyager message-search request failed and no keyword "
                    "query was observed on the search page, so the query id "
                    "could not be renewed."
                ) from exc
            payload = await self._fetch(self._search_url(mailbox_urn, keywords, cursor))

        inner = (payload.get("data") or {}).get("data") or {}
        container = inner.get("messengerConversationsBySearchCriteria")
        found = self._has_rows_key(container)
        rows = self._conversations(payload)
        self._refuse_unexplained_zero(
            rows=rows, payload=payload, path=_ELEMENTS_PATH, container_found=found
        )

        me = mailbox_urn.rsplit(":", 1)[-1]
        people = self._participants(payload)
        messages = self._messages_by_conversation(payload)
        conversations = [
            self._normalize(row, people, messages.get(row.get("entityUrn", "")), me)
            for row in rows
        ]

        zero_reason: str | None = None
        if not rows:
            zero_reason = "after-cursor" if cursor else "no-matches"
        return {
            "url": f"{MESSAGING_URL}?searchTerm={quote(keywords, safe='')}",
            "sections": {"search_results": self.render_page_text(conversations)},
            "keywords": keywords,
            "conversations": conversations,
            "count": len(conversations),
            "page_size": PAGE_SIZE,
            "next_cursor": self._forward_cursor(payload, cursor),
            # Measured from the rows, never from the cursor: a two-match search
            # was observed handing back a cursor anyway.
            "at_end": None if not rows else len(rows) < PAGE_SIZE,
            "zero_reason": zero_reason,
            "query_id_renewed": renewed,
        }
