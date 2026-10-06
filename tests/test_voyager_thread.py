"""``get_thread``: one thread's messages from the messaging API.

The page is faked at ``page.evaluate``, where each call is one request issued
from the logged-in page.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    InvalidReferenceError,
    LinkedInOperationError,
)
from linkedin_mcp_server.voyager import thread as thread_module
from linkedin_mcp_server.voyager.thread import PINNED_QUERY_ID, VoyagerThreadReader

THREAD_ID = "2-ZDBkMjZiY2Ut_XzEwMA=="
THREAD_URL = f"https://www.linkedin.com/messaging/thread/{THREAD_ID}/"
ME = "urn:li:fsd_profile:ACoAA-me"
ME_PARTICIPANT = f"urn:li:msg_messagingParticipant:{ME}"
ADA_PARTICIPANT = "urn:li:msg_messagingParticipant:urn:li:fsd_profile:ACoAA-ada"
ME_ANSWER = {"body": json.dumps({"included": [{"dashEntityUrn": ME}]})}
ROTATED = "messengerMessages.0123456789abcdef"


def _participant(urn: str, first: str, last: str) -> dict[str, Any]:
    return {
        "$type": "com.linkedin.messenger.MessagingParticipant",
        "entityUrn": urn,
        "hostIdentityUrn": urn.split("Participant:")[1],
        "participantType": {
            "member": {"firstName": {"text": first}, "lastName": {"text": last}}
        },
    }


def _message(sender: str | None, at: int, text: str) -> dict[str, Any]:
    entity: dict[str, Any] = {
        "$type": "com.linkedin.messenger.Message",
        "entityUrn": f"urn:li:msg_message:{at}",
        "deliveredAt": at,
        "body": {"text": text},
    }
    if sender:
        entity["*sender"] = sender
    return entity


def _thread(*messages: dict[str, Any], container: bool = True) -> dict[str, Any]:
    # The WRAPPED shape LinkedIn actually returns: data.data, not data.
    data: dict[str, Any] = {"data": {"other": {"k": 1}}}
    if container:
        data = {"data": {"messengerMessagesBySyncToken": {"*elements": []}}}
    included = [
        _participant(ME_PARTICIPANT, "Taylor", "M"),
        _participant(ADA_PARTICIPANT, "Ada", "Lovelace"),
        *messages,
    ]
    return {"body": json.dumps({"data": data, "included": included})}


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


def _reader(*answers: Any, emits: str | None = None):
    page = _Page(*answers)
    session = MagicMock()
    session.page = page
    session.check_rate_limit = AsyncMock()
    session.delay = AsyncMock()
    navigator = MagicMock()

    async def navigate(_url: str) -> None:
        if emits:
            for listener in list(page.listeners):
                listener(MagicMock(url=f"https://x/graphql?queryId={emits}&v=1"))

    navigator._navigate_to_page = AsyncMock(side_effect=navigate)
    return VoyagerThreadReader(session, navigator), page, navigator


@pytest.fixture(autouse=True)
def _no_cached_query_id():
    thread_module.forget_cached_query_id()
    yield
    thread_module.forget_cached_query_id()


async def test_messages_come_back_oldest_first_with_who_wrote_each():
    reader, page, navigator = _reader(
        ME_ANSWER,
        _thread(
            _message(ME_PARTICIPANT, 2_000, "my answer"),
            _message(ADA_PARTICIPANT, 1_000, "her question"),
        ),
    )

    result = await reader.get_thread(THREAD_URL)

    assert [m["text"] for m in result["messages"]] == ["her question", "my answer"]
    assert [m["from_me"] for m in result["messages"]] == [False, True]
    assert result["messages"][0]["sender_name"] == "Ada Lovelace"
    assert result["count"] == 2
    assert result["url"] == THREAD_URL
    assert result["thread_urn"] == f"urn:li:msg_conversation:({ME},{THREAD_ID})"
    assert [p["name"] for p in result["participants"]] == ["Ada Lovelace"]
    assert "You - " in result["sections"]["conversation"]
    # The whole point: no page is opened.
    assert result["query_id_renewed"] is False
    navigator._navigate_to_page.assert_not_awaited()
    assert PINNED_QUERY_ID in page.requests[1]


async def test_a_sender_the_payload_does_not_name_is_unknown_not_theirs():
    reader, _, _ = _reader(ME_ANSWER, _thread(_message(None, 1_000, "orphan")))

    result = await reader.get_thread(THREAD_ID)

    assert result["messages"][0]["from_me"] is None
    assert result["messages"][0]["sender_name"] is None


async def test_an_empty_thread_is_a_real_zero():
    reader, _, _ = _reader(ME_ANSWER, _thread())

    result = await reader.get_thread(THREAD_ID)

    assert result["count"] == 0
    assert result["messages"] == []


async def test_a_moved_container_is_refused_rather_than_read_as_empty():
    reader, _, _ = _reader(ME_ANSWER, _thread(container=False))

    with pytest.raises(LinkedInOperationError, match="changed shape"):
        await reader.get_thread(THREAD_ID)


async def test_a_rotated_query_id_is_renewed_without_opening_the_thread():
    reader, page, navigator = _reader(
        ME_ANSWER,
        {"error": "HTTP 400", "status": 400},
        _thread(_message(ADA_PARTICIPANT, 1_000, "hello")),
        emits=ROTATED,
    )

    result = await reader.get_thread(THREAD_ID)

    assert result["count"] == 1
    assert result["query_id_renewed"] is True
    navigator._navigate_to_page.assert_awaited_once_with(
        "https://www.linkedin.com/messaging/"
    )
    assert PINNED_QUERY_ID in page.requests[1]
    assert ROTATED in page.requests[2]
    assert page.listeners == []


async def test_a_renewed_query_id_is_reused_without_opening_another_thread():
    reader, page, navigator = _reader(
        ME_ANSWER,
        {"error": "HTTP 400", "status": 400},
        _thread(_message(ADA_PARTICIPANT, 1_000, "hello")),
        ME_ANSWER,
        _thread(_message(ADA_PARTICIPANT, 1_000, "hello")),
        emits=ROTATED,
    )
    await reader.get_thread(THREAD_ID)

    second = await reader.get_thread(THREAD_ID)

    assert second["query_id_renewed"] is False
    assert ROTATED in page.requests[4]
    navigator._navigate_to_page.assert_awaited_once()


async def test_a_query_id_that_cannot_be_renewed_raises_rather_than_reading_zero():
    reader, _, _ = _reader(ME_ANSWER, {"error": "HTTP 400", "status": 400})

    with pytest.raises(LinkedInOperationError, match="could not be renewed"):
        await reader.get_thread(THREAD_ID)


async def test_a_rejected_session_is_not_mistaken_for_a_rotated_query():
    reader, _, navigator = _reader(ME_ANSWER, {"error": "HTTP 403", "status": 403})

    with pytest.raises(AuthenticationError):
        await reader.get_thread(THREAD_ID)

    navigator._navigate_to_page.assert_not_awaited()


async def test_anything_but_a_thread_id_is_refused_before_any_request():
    reader, page, _ = _reader()

    with pytest.raises(InvalidReferenceError):
        await reader.get_thread("../../feed")
    with pytest.raises(LinkedInOperationError, match="not a messaging thread id"):
        await reader.get_thread(f"urn:li:msg_conversation:({ME},{THREAD_ID})")

    assert page.requests == []


def _search_page(*rows: tuple[str, str, bool, int]) -> dict[str, Any]:
    """A message-search answer: (thread id, participant urn, group, activity)."""
    included: list[dict[str, Any]] = []
    for thread, participant, group, activity in rows:
        included.append(
            {
                "$type": "com.linkedin.messenger.Conversation",
                "entityUrn": f"urn:li:msg_conversation:({ME},{thread})",
                "conversationUrl": f"https://www.linkedin.com/messaging/thread/{thread}/",
                "*conversationParticipants": [participant],
                "lastActivityAt": activity,
                "groupChat": group,
            }
        )
    for urn in {row[1] for row in rows}:
        included.append(_participant(urn, "Ada", "Lovelace"))
    container = {"*elements": [], "metadata": {"nextCursor": None}}
    payload = {
        "data": {"data": {"messengerConversationsBySearchCriteria": container}},
        "included": included,
    }
    return {"body": json.dumps(payload)}


ADA_URN = "urn:li:fsd_profile:ACoAA-ada"
ADA_FOUND = {
    "body": json.dumps(
        {
            "data": {"*elements": [ADA_URN]},
            "included": [
                {"entityUrn": ADA_URN, "firstName": "Ada", "lastName": "Lovelace"}
            ],
        }
    )
}
OTHER = "urn:li:msg_messagingParticipant:urn:li:fsd_profile:ACoAA-namesake"


async def test_a_person_leads_to_their_one_to_one_thread_before_any_group():
    reader, page, _ = _reader(
        ADA_FOUND,
        ME_ANSWER,
        _search_page(
            ("2-group", ADA_PARTICIPANT, True, 9_000),
            ("2-direct", ADA_PARTICIPANT, False, 1_000),
            # Same name, different member: found by the search, not hers.
            ("2-namesake", OTHER, False, 8_000),
        ),
        ME_ANSWER,
        _thread(_message(ADA_PARTICIPANT, 1_000, "hello")),
    )

    result = await reader.get_thread(linkedin_username="ada-lovelace")

    assert result["thread_id"] == "2-direct"
    assert "keywords:Ada%20Lovelace)" in page.requests[2]


async def test_index_selects_among_a_persons_threads_and_past_the_end_says_so():
    answers = (
        ADA_FOUND,
        ME_ANSWER,
        _search_page(
            ("2-group", ADA_PARTICIPANT, True, 9_000),
            ("2-direct", ADA_PARTICIPANT, False, 1_000),
        ),
    )
    reader, _, _ = _reader(
        *answers, ME_ANSWER, _thread(_message(ADA_PARTICIPANT, 1_000, "hello"))
    )
    second = await reader.get_thread(linkedin_username="ada-lovelace", index=1)
    assert second["thread_id"] == "2-group"

    past, _, _ = _reader(*answers)
    with pytest.raises(LinkedInOperationError, match="found 2 conversation"):
        await past.get_thread(linkedin_username="ada-lovelace", index=2)


async def test_a_thread_id_wins_over_a_username_and_no_lookup_is_made():
    reader, page, _ = _reader(ME_ANSWER, _thread(_message(ADA_PARTICIPANT, 1, "x")))

    result = await reader.get_thread(THREAD_ID, linkedin_username="ada-lovelace")

    assert result["thread_id"] == THREAD_ID
    assert len(page.requests) == 2


async def test_neither_a_thread_nor_a_person_is_refused_before_any_request():
    reader, page, _ = _reader()

    with pytest.raises(LinkedInOperationError, match="at least one of"):
        await reader.get_thread()

    assert page.requests == []
