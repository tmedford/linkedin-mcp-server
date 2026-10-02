"""``reply_to_thread`` through the messaging API.

The page is faked at ``page.evaluate``, which is the only thing the sender
touches: each call is one HTTP request issued from the logged-in page, so what
is asserted here is which requests are made, with what body, and what each
possible answer is reported as.
"""

from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    InvalidReferenceError,
    LinkedInScraperException,
    RateLimitError,
)
from linkedin_mcp_server.voyager import overlay
from linkedin_mcp_server.voyager.thread_reply import (
    VoyagerThreadReply,
    refuse_an_invalid_reply,
)

THREAD_ID = "2-ZDBkMjZiY2Ut_XzEwMA=="
THREAD_URL = f"https://www.linkedin.com/messaging/thread/{THREAD_ID}/"
ME = "urn:li:fsd_profile:ACoAA-me"
CONVERSATION = f"urn:li:msg_conversation:({ME},{THREAD_ID})"
CREATED = f"urn:li:msg_message:({ME},2-new)"

ME_ANSWER = {"body": json.dumps({"included": [{"dashEntityUrn": ME}]})}


def _created(conversation: str = CONVERSATION) -> dict[str, Any]:
    value = {
        "entityUrn": CREATED,
        "conversationUrn": conversation,
        "deliveredAt": 1_790_000_000_000,
    }
    return {"status": 200, "body": json.dumps({"value": value})}


def _thread(*texts: str) -> dict[str, Any]:
    included: list[dict[str, Any]] = [
        {
            "$type": "com.linkedin.messenger.MessagingParticipant",
            "participantType": {
                "member": {"firstName": {"text": "Ada"}, "lastName": {"text": "L"}}
            },
        }
    ]
    for index, text in enumerate(texts):
        included.append(
            {
                "$type": "com.linkedin.messenger.Message",
                "deliveredAt": 1_790_000_000_000 + index,
                "body": {"text": text},
            }
        )
    return {"body": json.dumps({"included": included})}


class _Page:
    """Answers each request in order and remembers what was asked."""

    def __init__(self, *answers: Any):
        self._answers = list(answers)
        self.requests: list[Any] = []

    async def evaluate(self, _program: str, argument: Any) -> Any:
        self.requests.append(argument)
        answer = self._answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @property
    def writes(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if isinstance(r, dict)]


def _replier(*answers: Any) -> tuple[VoyagerThreadReply, _Page]:
    page = _Page(*answers)
    session = MagicMock()
    session.page = page
    return VoyagerThreadReply(session, MagicMock()), page


@pytest.mark.parametrize(
    "reference",
    [
        THREAD_ID,
        THREAD_URL,
        f"/messaging/thread/{THREAD_ID}/",
        f"{THREAD_URL}?source=inbox",
    ],
)
async def test_an_id_and_every_reference_to_it_address_the_same_conversation(
    reference,
):
    replier, page = _replier(ME_ANSWER, _created())

    result = await replier.reply_to_thread(reference, "hello", confirm_send=True)

    assert result["status"] == "sent"
    assert page.writes[0]["body"]["message"]["conversationUrn"] == CONVERSATION


@pytest.mark.parametrize(
    "reference",
    [
        "https://www.linkedin.com/messaging/compose/?recipient=ACoAAB",
        "https://evil.example/messaging/thread/2-abc/",
        "../../feed",
    ],
)
def test_a_reference_to_anything_but_a_thread_raises(reference):
    with pytest.raises(InvalidReferenceError, match="thread_id"):
        refuse_an_invalid_reply(reference, "hello")


def test_a_thread_urn_is_refused_rather_than_wrapped_in_another_urn():
    refusal = refuse_an_invalid_reply(CONVERSATION, "hello")

    assert refusal is not None
    assert refusal["status"] == "invalid_thread"
    assert refusal["sent"] is False


@pytest.mark.parametrize(
    "message", ["", "  \t ", " \n ", "cr\r\nlf", "tab\there", "bell\x07", "del\x7f"]
)
async def test_an_unsendable_message_makes_no_request_at_all(message):
    replier, page = _replier()

    result = await replier.reply_to_thread(THREAD_ID, message, confirm_send=True)

    assert result["status"] == "invalid_message"
    assert result["url"] == THREAD_URL
    assert result["retry_safe"] is True
    assert page.requests == []


async def test_a_dry_run_reads_the_thread_and_writes_nothing():
    replier, page = _replier(ME_ANSWER, _thread("first", "latest"))

    result = await replier.reply_to_thread(THREAD_URL, "hello", confirm_send=False)

    assert result["status"] == "confirmation_required"
    assert result["sent"] is False
    assert result["retry_safe"] is True
    assert result["thread_readable"] is True
    assert result["participants"] == ["Ada L"]
    assert result["last_message_text"] == "latest"
    assert page.writes == []
    assert len(page.requests) == 2


async def test_a_dry_run_on_a_thread_holding_nothing_says_so():
    replier, page = _replier(ME_ANSWER, {"body": json.dumps({"included": []})})

    result = await replier.reply_to_thread(THREAD_ID, "hello", confirm_send=False)

    assert result["status"] == "thread_not_found"
    assert result["thread_readable"] is False
    assert page.writes == []


async def test_a_stale_preview_query_does_not_block_or_vouch_for_the_thread():
    # The persisted query id rotates. That is a failed preview, not a missing
    # thread, and it must be reported as neither readable nor unreadable.
    replier, page = _replier(ME_ANSWER, {"error": "HTTP 400", "status": 400})

    result = await replier.reply_to_thread(THREAD_ID, "hello", confirm_send=False)

    assert result["status"] == "confirmation_required"
    assert result["thread_readable"] is None
    assert result["recipient_selected"] is False
    assert page.writes == []


async def test_a_confirmed_reply_is_one_write_addressed_to_the_named_thread():
    replier, page = _replier(ME_ANSWER, _created())

    result = await replier.reply_to_thread(THREAD_ID, "hello there", confirm_send=True)

    assert result["status"] == "sent"
    assert result["sent"] is True
    assert result["retry_safe"] is False
    assert result["message_urn"] == CREATED
    assert result["delivered_at"] == "2026-09-21T14:13:20+00:00"
    assert len(page.writes) == 1
    write = page.writes[0]
    assert write["url"].endswith(
        "/voyagerMessagingDashMessengerMessages?action=createMessage"
    )
    assert write["body"]["mailboxUrn"] == ME
    assert write["body"]["message"]["body"]["text"] == "hello there"
    assert write["body"]["message"]["conversationUrn"] == CONVERSATION


async def test_line_breaks_reach_the_server_as_written():
    replier, page = _replier(ME_ANSWER, _created())

    result = await replier.reply_to_thread(
        THREAD_ID, "Hey Roy,\nthanks for the note.", confirm_send=True
    )

    assert result["status"] == "sent"
    assert (
        page.writes[0]["body"]["message"]["body"]["text"]
        == "Hey Roy,\nthanks for the note."
    )


async def test_each_send_carries_its_own_token():
    first, page_one = _replier(ME_ANSWER, _created())
    second, page_two = _replier(ME_ANSWER, _created())

    await first.reply_to_thread(THREAD_ID, "same text", confirm_send=True)
    await second.reply_to_thread(THREAD_ID, "same text", confirm_send=True)

    tokens = {
        page.writes[0]["body"]["message"]["originToken"]
        for page in (page_one, page_two)
    }
    assert len(tokens) == 2


async def test_a_refused_write_sent_nothing_and_may_be_retried():
    replier, _ = _replier(ME_ANSWER, {"status": 400, "body": '{"code":"BAD"}'})

    result = await replier.reply_to_thread(THREAD_ID, "hello", confirm_send=True)

    assert result["status"] == "send_rejected"
    assert result["sent"] is False
    assert result["retry_safe"] is True
    assert result["http_status"] == 400


@pytest.mark.parametrize(
    "answer",
    [
        {"status": 500, "body": ""},
        {"status": 200, "body": "not json"},
        {"status": 200, "body": '{"value": {}}'},
        RuntimeError("round trip interrupted"),
    ],
)
async def test_an_answer_that_does_not_name_the_message_is_never_called_sent(answer):
    replier, _ = _replier(ME_ANSWER, answer)

    result = await replier.reply_to_thread(THREAD_ID, "hello", confirm_send=True)

    assert result["status"] == "send_unconfirmed"
    assert result["sent"] is False
    assert result["retry_safe"] is False


async def test_a_reply_delivered_elsewhere_is_not_reported_as_a_plain_success():
    other = f"urn:li:msg_conversation:({ME},2-other)"
    replier, _ = _replier(ME_ANSWER, _created(other))

    result = await replier.reply_to_thread(THREAD_ID, "hello", confirm_send=True)

    assert result["status"] == "sent_to_other_thread"
    assert result["delivered_to"] == other
    assert result["retry_safe"] is False


@pytest.mark.parametrize(
    ("status", "error"), [(401, AuthenticationError), (429, RateLimitError)]
)
async def test_auth_and_rate_limit_refusals_keep_their_own_types(status, error):
    replier, _ = _replier(ME_ANSWER, {"status": status, "body": ""})

    with pytest.raises(error):
        await replier.reply_to_thread(THREAD_ID, "hello", confirm_send=True)


@pytest.mark.parametrize(
    "included",
    [
        [],
        [{"dashEntityUrn": ME}, {"dashEntityUrn": "urn:li:fsd_profile:ACoAA-x"}],
        [{"dashEntityUrn": "urn:li:member:1"}],
    ],
)
async def test_an_unidentified_sender_stops_before_any_write(included):
    replier, page = _replier({"body": json.dumps({"included": included})})

    with pytest.raises(LinkedInScraperException, match="signed-in member"):
        await replier.reply_to_thread(THREAD_ID, "hello", confirm_send=True)

    assert page.writes == []


async def _tool(mcp: FastMCP, name: str) -> Any:
    tool = await mcp.get_tool(name)
    assert tool is not None
    return cast(FunctionTool, tool).fn


def _served(monkeypatch) -> FastMCP:
    mcp = FastMCP("test")
    monkeypatch.setattr(overlay, "SUPERSEDED", {})
    overlay.install_voyager_overlay(mcp)
    return mcp


async def test_the_tool_forwards_the_confirmation_gate(monkeypatch, mock_context):
    expected = {"status": "confirmation_required", "sent": False}
    extractor = MagicMock()
    extractor.reply_to_thread = AsyncMock(return_value=expected)
    reply = await _tool(_served(monkeypatch), "reply_to_thread")

    result = await reply(THREAD_URL, "Draft only", False, mock_context, extractor)

    assert result is expected
    extractor.reply_to_thread.assert_awaited_once_with(
        THREAD_URL, "Draft only", confirm_send=False
    )


async def test_the_tool_refuses_an_unsendable_reply_before_acquiring_a_session(
    monkeypatch, mock_context
):
    acquire = AsyncMock(side_effect=AssertionError("a session was acquired"))
    monkeypatch.setattr(overlay, "get_ready_extractor", acquire)
    reply = await _tool(_served(monkeypatch), "reply_to_thread")

    result = await reply(THREAD_ID, "bell\x07", True, mock_context)

    assert result["status"] == "invalid_message"
    acquire.assert_not_awaited()


async def test_the_tool_names_an_unusable_thread_instead_of_masking_it(
    monkeypatch, mock_context
):
    reply = await _tool(_served(monkeypatch), "reply_to_thread")

    with pytest.raises(ToolError, match="thread_id"):
        await reply("../../feed", "hello", True, mock_context)
