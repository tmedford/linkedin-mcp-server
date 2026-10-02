"""``message_person``: a message addressed to a member through the API."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInScraperException,
)
from linkedin_mcp_server.voyager.person_message import (
    VoyagerPersonMessage,
    refuse_an_invalid_person_message,
)

ME = "urn:li:fsd_profile:ACoAA-me"
ADA = "urn:li:fsd_profile:ACoAA-ada"
ME_ANSWER = {"body": json.dumps({"included": [{"dashEntityUrn": ME}]})}
THREAD = f"urn:li:msg_conversation:({ME},2-new_Thread==)"


def _profiles(*urns: str, key: str = "*elements") -> dict[str, Any]:
    data: dict[str, Any] = {"paging": {"count": 10}}
    if key:
        data[key] = list(urns)
    included = [
        {"entityUrn": urn, "firstName": "Ada", "lastName": "Lovelace"} for urn in urns
    ]
    return {"body": json.dumps({"data": data, "included": included})}


def _created(conversation: str | None = THREAD) -> dict[str, Any]:
    value: dict[str, Any] = {
        "entityUrn": f"urn:li:msg_message:({ME},2-msg)",
        "deliveredAt": 1_790_000_000_000,
    }
    if conversation:
        value["conversationUrn"] = conversation
    return {"status": 200, "body": json.dumps({"value": value})}


class _Page:
    def __init__(self, *answers: Any):
        self._answers = list(answers)
        self.requests: list[Any] = []

    async def evaluate(self, _program: str, argument: Any) -> Any:
        self.requests.append(argument)
        return self._answers.pop(0)

    @property
    def writes(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if isinstance(r, dict)]


def _sender(*answers: Any) -> tuple[VoyagerPersonMessage, _Page]:
    page = _Page(*answers)
    session = MagicMock()
    session.page = page
    return VoyagerPersonMessage(session, MagicMock()), page


async def test_a_dry_run_names_the_recipient_and_writes_nothing():
    sender, page = _sender(ME_ANSWER, _profiles(ADA))

    result = await sender.message_person("ada-lovelace", "hello", confirm_send=False)

    assert result["status"] == "confirmation_required"
    assert result["recipient_selected"] is True
    assert result["recipient_urn"] == ADA
    assert result["recipient_name"] == "Ada Lovelace"
    assert result["sent"] is False
    assert page.writes == []
    assert "memberIdentity=ada-lovelace" in page.requests[1]


async def test_a_confirmed_message_is_one_write_addressed_to_that_member():
    sender, page = _sender(ME_ANSWER, _profiles(ADA), _created())

    result = await sender.message_person(
        "https://www.linkedin.com/in/ada-lovelace/",
        "Hey Ada,\n\nhello",
        confirm_send=True,
    )

    assert result["status"] == "sent"
    assert result["sent"] is True
    assert result["retry_safe"] is False
    # Where the server says it landed, in the form the thread tools take.
    assert result["thread_urn"] == THREAD
    assert result["thread_id"] == "2-new_Thread=="
    assert result["thread_url"].endswith("/messaging/thread/2-new_Thread==/")
    assert len(page.writes) == 1
    body = page.writes[0]["body"]
    assert body["hostRecipientUrns"] == [ADA]
    assert body["mailboxUrn"] == ME
    assert body["message"]["body"]["text"] == "Hey Ada,\n\nhello"
    assert "conversationUrn" not in body["message"]


async def test_a_sent_message_with_no_conversation_named_invents_no_thread():
    sender, _ = _sender(ME_ANSWER, _profiles(ADA), _created(conversation=None))

    result = await sender.message_person("ada-lovelace", "hello", confirm_send=True)

    assert result["status"] == "sent"
    assert "thread_id" not in result
    assert "thread_url" not in result


async def test_a_refused_write_sent_nothing_and_may_be_retried():
    sender, _ = _sender(ME_ANSWER, _profiles(ADA), {"status": 422, "body": "{}"})

    result = await sender.message_person("ada-lovelace", "hello", confirm_send=True)

    assert result["status"] == "send_rejected"
    assert result["sent"] is False
    assert result["retry_safe"] is True
    assert result["recipient_urn"] == ADA


@pytest.mark.parametrize("urns", [(), (ADA, "urn:li:fsd_profile:ACoAA-other")])
async def test_anything_but_exactly_one_member_stops_before_any_write(urns):
    key = "*elements" if urns else "elements"
    sender, page = _sender(ME_ANSWER, _profiles(*urns, key=key))

    with pytest.raises(LinkedInScraperException, match="not exactly one"):
        await sender.message_person("ada-lovelace", "hello", confirm_send=True)

    assert page.writes == []


async def test_a_lookup_whose_shape_moved_is_not_read_as_nobody_found():
    sender, _ = _sender(ME_ANSWER, _profiles(key=""))

    with pytest.raises(LinkedInScraperException, match="changed shape"):
        await sender.message_person("ada-lovelace", "hello", confirm_send=True)


async def test_messaging_yourself_is_refused_before_any_write():
    sender, page = _sender(ME_ANSWER, _profiles(ME))

    result = await sender.message_person("taylor", "hello", confirm_send=True)

    assert result["status"] == "recipient_is_sender"
    assert page.writes == []


@pytest.mark.parametrize("message", ["", "  ", "tab\there", "cr\r\n", "del\x7f"])
def test_an_unsendable_message_is_refused_without_a_browser(message):
    refusal = refuse_an_invalid_person_message("ada-lovelace", message)

    assert refusal is not None
    assert refusal["status"] == "invalid_message"
    assert refusal["url"] == "https://www.linkedin.com/in/ada-lovelace/"


def test_line_breaks_are_text_and_a_bad_username_raises():
    assert refuse_an_invalid_person_message("ada-lovelace", "Hey Ada,\n\nhi") is None
    with pytest.raises(InvalidReferenceError):
        refuse_an_invalid_person_message("../../feed", "hi")


GRACE = "urn:li:fsd_profile:ACoAA-grace"


async def test_several_identifiers_address_one_group_conversation():
    sender, page = _sender(ME_ANSWER, _profiles(ADA), _profiles(GRACE), _created())

    result = await sender.message_person(
        "ada-lovelace, grace-hopper", "hello both", confirm_send=True
    )

    assert result["status"] == "sent"
    assert [r["urn"] for r in result["recipients"]] == [ADA, GRACE]
    assert "recipient_urn" not in result
    assert len(page.writes) == 1
    assert page.writes[0]["body"]["hostRecipientUrns"] == [ADA, GRACE]


async def test_one_member_named_twice_is_one_recipient_not_a_group():
    sender, page = _sender(ME_ANSWER, _profiles(ADA), _profiles(ADA), _created())

    result = await sender.message_person(
        "ada-lovelace,ada-lovelace-alias", "hello", confirm_send=True
    )

    assert page.writes[0]["body"]["hostRecipientUrns"] == [ADA]
    assert result["recipient_urn"] == ADA


async def test_a_group_that_includes_the_sender_is_refused_before_any_write():
    sender, page = _sender(ME_ANSWER, _profiles(ADA), _profiles(ME))

    result = await sender.message_person("ada-lovelace,taylor", "hi", confirm_send=True)

    assert result["status"] == "recipient_is_sender"
    assert page.writes == []


def test_one_bad_identifier_in_a_group_raises_for_the_whole_call():
    with pytest.raises(InvalidReferenceError):
        refuse_an_invalid_person_message("ada-lovelace, ../../feed", "hi")


@pytest.mark.parametrize("urn", ["ACoAA-ada", "urn:li:fsd_profile:ACoAA-ada"])
async def test_a_matching_profile_urn_lets_the_send_through(urn):
    sender, page = _sender(ME_ANSWER, _profiles(ADA), _created())

    result = await sender.message_person(
        "ada-lovelace", "hello", confirm_send=True, profile_urn=urn
    )

    assert result["status"] == "sent"
    assert len(page.writes) == 1


async def test_a_profile_urn_for_someone_else_stops_the_send():
    sender, page = _sender(ME_ANSWER, _profiles(ADA))

    result = await sender.message_person(
        "ada-lovelace", "hello", confirm_send=True, profile_urn="ACoAA-other"
    )

    assert result["status"] == "recipient_resolution_failed"
    assert result["sent"] is False
    assert page.writes == []
