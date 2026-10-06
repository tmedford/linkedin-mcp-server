"""``search_messages``: keyword search through the messaging API."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    LinkedInOperationError,
)
from linkedin_mcp_server.voyager import message_search as search_module
from linkedin_mcp_server.voyager.message_search import (
    PAGE_SIZE,
    PINNED_QUERY_ID,
    VoyagerMessageSearch,
)

ME = "urn:li:fsd_profile:ACoAA-me"
ME_ANSWER = {"body": json.dumps({"included": [{"dashEntityUrn": ME}]})}
ADA = "urn:li:msg_messagingParticipant:urn:li:fsd_profile:ACoAA-ada"
ROTATED = "messengerConversations.0123456789abcdef"


def _conversation(index: int) -> list[dict[str, Any]]:
    urn = f"urn:li:msg_conversation:({ME},2-thread{index})"
    return [
        {
            "$type": "com.linkedin.messenger.Conversation",
            "entityUrn": urn,
            "conversationUrl": f"https://www.linkedin.com/messaging/thread/2-thread{index}/",
            "*conversationParticipants": [ADA],
            "lastActivityAt": 1_790_000_000_000 + index,
        },
        {
            "$type": "com.linkedin.messenger.Message",
            "*conversation": urn,
            "*sender": ADA,
            "deliveredAt": 1_790_000_000_000 + index,
            "body": {"text": f"message {index}"},
        },
    ]


def _page(count: int, *, cursor: str | None = None, key: str = "*elements") -> dict:
    included: list[dict[str, Any]] = [
        {
            "$type": "com.linkedin.messenger.MessagingParticipant",
            "entityUrn": ADA,
            "hostIdentityUrn": "urn:li:fsd_profile:ACoAA-ada",
            "participantType": {
                "member": {"firstName": {"text": "Ada"}, "lastName": {"text": "L"}}
            },
        }
    ]
    for index in range(count):
        included.extend(_conversation(index))
    container: dict[str, Any] = {"metadata": {"nextCursor": cursor}}
    if key:
        container[key] = []
    payload = {
        "data": {"data": {"messengerConversationsBySearchCriteria": container}},
        "included": included if count else [],
    }
    return {"body": json.dumps(payload)}


class _Page:
    def __init__(self, *answers: Any):
        self._answers = list(answers)
        self.requests: list[Any] = []
        self.listeners: list[Any] = []

    async def evaluate(self, _program: str, argument: Any) -> Any:
        self.requests.append(argument)
        return self._answers.pop(0)

    def on(self, _event: str, callback: Any) -> None:
        self.listeners.append(callback)

    def remove_listener(self, _event: str, callback: Any) -> None:
        self.listeners.remove(callback)


def _search(*answers: Any, emits: tuple[str, ...] = ()):
    page = _Page(*answers)
    session = MagicMock()
    session.page = page
    session.check_rate_limit = AsyncMock()
    session.delay = AsyncMock()
    navigator = MagicMock()

    async def navigate(_url: str) -> None:
        for url in emits:
            for listener in list(page.listeners):
                listener(MagicMock(url=url))

    navigator._navigate_to_page = AsyncMock(side_effect=navigate)
    return VoyagerMessageSearch(session, navigator), page, navigator


@pytest.fixture(autouse=True)
def _no_cached_query_id():
    search_module.forget_cached_query_id()
    yield
    search_module.forget_cached_query_id()


async def test_matches_come_back_as_conversation_rows_without_any_navigation():
    search, page, navigator = _search(ME_ANSWER, _page(2, cursor="MCYyMA=="))

    result = await search.search_messages("  Temporal ")

    assert result["count"] == 2
    assert result["keywords"] == "Temporal"
    assert result["conversations"][0]["participants"] == ["Ada L"]
    assert result["conversations"][0]["thread_url"].endswith("/2-thread0/")
    assert result["query_id_renewed"] is False
    navigator._navigate_to_page.assert_not_awaited()
    assert PINNED_QUERY_ID in page.requests[1]
    assert "keywords:Temporal)" in page.requests[1]


async def test_a_cursor_on_a_short_page_is_not_read_as_more():
    # Measured: a two-match search handed back a cursor anyway.
    search, _, _ = _search(ME_ANSWER, _page(2, cursor="MCYyMA=="))

    result = await search.search_messages("Temporal")

    assert result["next_cursor"] == "MCYyMA=="
    assert result["at_end"] is True


async def test_a_full_page_is_not_the_end():
    search, _, _ = _search(ME_ANSWER, _page(PAGE_SIZE, cursor="next"))

    result = await search.search_messages("engineering")

    assert result["count"] == PAGE_SIZE
    assert result["at_end"] is False


async def test_no_matches_is_a_real_zero_in_the_shape_linkedin_sends_it():
    # An empty collection carries `elements: []`, not `*elements`.
    search, _, _ = _search(ME_ANSWER, _page(0, key="elements"))

    result = await search.search_messages("zzqxjvkw")

    assert result["count"] == 0
    assert result["at_end"] is None
    assert result["zero_reason"] == "no-matches"


async def test_a_missing_container_is_refused_rather_than_read_as_no_matches():
    search, _, _ = _search(ME_ANSWER, _page(0, key=""))

    with pytest.raises(LinkedInOperationError, match="changed shape"):
        await search.search_messages("Temporal")


async def test_keywords_and_cursor_cannot_inject_query_syntax():
    search, page, _ = _search(ME_ANSWER, _page(1))

    await search.search_messages("a,b:(c) d", cursor="x),y:z")

    url = page.requests[1]
    variables = url.split("variables=", 1)[1]
    assert "keywords:a%2Cb%3A%28c%29%20d)" in variables
    assert "nextCursor:x%29%2Cy%3Az," in variables


@pytest.mark.parametrize("keywords", ["", "   "])
async def test_blank_keywords_are_refused_before_any_request(keywords):
    search, page, _ = _search()

    with pytest.raises(LinkedInOperationError, match="keywords was blank"):
        await search.search_messages(keywords)

    assert page.requests == []


async def test_a_blank_cursor_is_refused_rather_than_read_as_page_one():
    search, page, _ = _search()

    with pytest.raises(LinkedInOperationError, match="cursor was blank"):
        await search.search_messages("Temporal", cursor=" ")

    assert page.requests == []


async def test_a_rotated_query_id_is_renewed_from_the_search_request_only():
    inbox = "https://x/graphql?queryId=messengerConversations.aaaaaaaa&variables=(mailboxUrn:u)"
    keyword = f"https://x/graphql?queryId={ROTATED}&variables=(keywords:Temporal)"
    search, page, navigator = _search(
        ME_ANSWER,
        {"error": "HTTP 400", "status": 400},
        _page(1),
        emits=(inbox, keyword),
    )

    result = await search.search_messages("Temporal")

    assert result["count"] == 1
    assert result["query_id_renewed"] is True
    navigator._navigate_to_page.assert_awaited_once_with(
        "https://www.linkedin.com/messaging/?searchTerm=Temporal"
    )
    # The inbox query on the same page has another id and is not taken for it.
    assert ROTATED in page.requests[2]
    assert page.listeners == []


async def test_a_rejected_session_is_not_mistaken_for_a_rotated_query():
    search, _, navigator = _search(ME_ANSWER, {"error": "HTTP 403", "status": 403})

    with pytest.raises(AuthenticationError):
        await search.search_messages("Temporal")

    navigator._navigate_to_page.assert_not_awaited()
