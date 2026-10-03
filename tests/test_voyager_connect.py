"""``connect_with_person`` without a note: LinkedIn's own Connect action."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.voyager.connect import (
    VoyagerConnect,
    connect_body,
    connect_payload,
    parse_relationship,
)

URN = "urn:li:fsd_profile:ACoAA-ada"
ME = "urn:li:fsd_profile:ACoAA-me"


def _top_card(union: dict[str, Any]) -> dict[str, Any]:
    return {
        "included": [
            {
                "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
                "entityUrn": URN,
                "objectUrn": "urn:li:member:4242",
                "firstName": "Ada",
                "lastName": "Lovelace",
                "publicIdentifier": "ada-lovelace",
            },
            {
                "$type": "com.linkedin.voyager.dash.relationships.invitation.Invitation",
                "entityUrn": INVITATION,
                "inviteeMember": URN,
                "invitationState": "PENDING",
                "invitationType": "SENT",
            },
            {
                "$type": "com.linkedin.voyager.dash.relationships.MemberRelationship",
                "entityUrn": "urn:li:fsd_memberRelationship:ACoAA-ada",
                "memberRelationshipUnion": union,
            },
        ]
    }


NOT_INVITED = {"noConnection": {"invitationUnion": {"noInvitation": {"inviter": ME}}}}
# As measured after a real send: the union names an Invitation entity, and that
# entity, in ``included``, says who was invited.
INVITATION = "urn:li:fsd_invitation:7511933106086473728"
INVITED_BY_ME = {"noConnection": {"invitationUnion": {"*invitation": INVITATION}}}
INVITED_BY_THEM = {
    "noConnection": {"invitationUnion": {"invitation": {"inviteeMember": ME}}}
}


@pytest.mark.parametrize(
    ("union", "state"),
    [
        (NOT_INVITED, "not_invited"),
        (INVITED_BY_ME, "invited_by_me"),
        (INVITED_BY_THEM, "invited_by_them"),
        ({"*connection": "urn:li:fsd_connection:1"}, "connected"),
        ({"self": {}}, "self"),
        ({}, "unknown"),
    ],
)
def test_the_state_comes_from_which_union_member_is_present(union, state):
    assert parse_relationship(_top_card(union), URN)["state"] == state


def test_the_request_is_built_as_the_viewer_list_renders_it():
    person = parse_relationship(_top_card(NOT_INVITED), URN)

    payload = connect_payload(person)

    assert payload["inviteeUrn"] == {"memberId": "4242"}
    assert payload["nonIterableProfileId"] == URN
    assert payload["connectionState"]["key"] == "state:invitation:urn:li:member:4242"
    assert payload["isDisabled"]["key"] == "connect-button-disabled-ada-lovelace"
    assert payload["profileCanonicalUrl"] == "https://www.linkedin.com/in/ada-lovelace"
    body = json.loads(connect_body(payload))
    assert body["requestId"] == "com.linkedin.sdui.requests.mynetwork.addaAddConnection"
    assert body["serverRequest"]["requestedArguments"]["payload"] == payload
    assert body["requestedArguments"]["payload"] == payload
    assert body["requestedArguments"]["screenId"].endswith("wvmp.WVMP")


class _Page:
    """Profile reads by URL; every POST recorded, answered with ``status``."""

    def __init__(self, *top_cards: dict[str, Any], status: int = 200):
        self.top_cards = list(top_cards)
        self.status = status
        self.posts: list[dict[str, Any]] = []

    async def evaluate(self, _program: str, argument: Any = None) -> Any:
        if isinstance(argument, dict):
            self.posts.append(argument)
            return {"status": self.status, "text": "0:[]"}
        if "decorationId" in argument:
            card = (
                self.top_cards[0] if len(self.top_cards) == 1 else self.top_cards.pop(0)
            )
            return {"body": json.dumps(card)}
        return {
            "body": json.dumps(
                {
                    "data": {"*elements": [URN]},
                    "included": _top_card({})["included"][:1],
                }
            )
        }


def _reader(page: _Page) -> VoyagerConnect:
    session = MagicMock()
    session.page = page
    session.delay = AsyncMock()
    reader = VoyagerConnect(session, MagicMock())
    setattr(reader, "_page_headers", AsyncMock(return_value={"x-li-track": "{}"}))
    return reader


async def test_a_send_is_confirmed_by_reading_the_relationship_back():
    page = _Page(_top_card(NOT_INVITED), _top_card(INVITED_BY_ME))

    result = await _reader(page).connect_with_person("ada-lovelace")

    assert result["status"] == "pending"
    assert (result["relationship_before"], result["relationship_after"]) == (
        "not_invited",
        "invited_by_me",
    )
    assert len(page.posts) == 1
    assert page.posts[0]["url"].endswith(
        "server-request?sduiid=com.linkedin.sdui.requests.mynetwork.addaAddConnection"
    )
    assert json.loads(page.posts[0]["body"])["requestedArguments"]["payload"][
        "inviteeUrn"
    ] == {"memberId": "4242"}


async def test_a_200_that_changed_nothing_is_not_reported_as_sent():
    page = _Page(_top_card(NOT_INVITED), _top_card(NOT_INVITED))

    result = await _reader(page).connect_with_person("ada-lovelace")

    assert result["status"] == "send_unconfirmed"
    assert len(page.posts) == 1


async def test_a_refused_request_is_a_failed_send():
    page = _Page(_top_card(NOT_INVITED), status=500)

    result = await _reader(page).connect_with_person("ada-lovelace")

    assert result["status"] == "send_failed"


async def test_a_dry_run_sends_nothing():
    page = _Page(_top_card(NOT_INVITED))

    result = await _reader(page).connect_with_person("ada-lovelace", dry_run=True)

    assert result["status"] == "dry_run"
    assert result["request"]["inviteeUrn"] == {"memberId": "4242"}
    assert page.posts == []


@pytest.mark.parametrize(
    ("union", "status"),
    [
        ({"*connection": "urn:li:fsd_connection:1"}, "already_connected"),
        (INVITED_BY_ME, "pending"),
        (INVITED_BY_THEM, "connect_unavailable"),
        ({"self": {}}, "connect_unavailable"),
        ({}, "connect_unavailable"),
    ],
)
async def test_nothing_is_sent_unless_the_member_was_never_invited(union, status):
    page = _Page(_top_card(union))

    result = await _reader(page).connect_with_person("ada-lovelace")

    assert result["status"] == status
    assert page.posts == []


async def test_a_read_back_that_fails_after_a_send_is_not_an_error():
    class AfterSendFails(_Page):
        async def evaluate(self, _program: str, argument: Any = None) -> Any:
            if self.posts and isinstance(argument, str) and "decorationId" in argument:
                return {"body": "not json"}
            return await super().evaluate(_program, argument)

    page = AfterSendFails(_top_card(NOT_INVITED))

    result = await _reader(page).connect_with_person("ada-lovelace")

    assert result["status"] == "send_unconfirmed"
    assert "Do not resend" in result["message"]
    assert len(page.posts) == 1


def _with_note(union: dict[str, Any], message: Any) -> dict[str, Any]:
    card = _top_card(union)
    for entity in card["included"]:
        if entity.get("entityUrn") == INVITATION:
            entity["message"] = message
    return card


class _NotePage(_Page):
    """The note call goes through the shared Voyager POST, not the stream one."""

    async def evaluate(self, _program: str, argument: Any = None) -> Any:
        if isinstance(argument, dict) and "invitee" in json.dumps(argument.get("body")):
            self.posts.append(argument)
            return {"status": self.status, "body": "{}"}
        return await super().evaluate(_program, argument)


async def test_a_note_is_sent_with_the_dialogs_call_and_read_back():
    page = _NotePage(
        _top_card(NOT_INVITED), _with_note(INVITED_BY_ME, "Hey Ada, great talk")
    )

    result = await _reader(page).connect_with_person(
        "ada-lovelace", note="  Hey Ada, great talk  "
    )

    assert (result["status"], result["note_sent"]) == ("pending", True)
    sent = page.posts[0]
    assert "action=verifyQuotaAndCreateV2" in sent["url"]
    assert "InvitationCreationResultWithInvitee-3" in sent["url"]
    assert sent["body"]["invitee"] == {"inviteeUnion": {"memberProfile": URN}}
    assert sent["body"]["customMessage"] == "Hey Ada, great talk"


async def test_a_note_that_did_not_come_back_is_not_claimed_as_sent():
    page = _NotePage(_top_card(NOT_INVITED), _with_note(INVITED_BY_ME, None))

    result = await _reader(page).connect_with_person("ada-lovelace", note="Hi")

    assert (result["status"], result["note_sent"]) == ("pending", False)


async def test_a_refused_note_passes_linkedins_answer_on():
    page = _NotePage(_top_card(NOT_INVITED), status=400)

    result = await _reader(page).connect_with_person("ada-lovelace", note="Hi")

    assert result["status"] == "send_failed"
    assert "response_excerpt" in result


async def test_a_note_over_the_limit_is_refused_before_any_request():
    page = _NotePage(_top_card(NOT_INVITED))

    with pytest.raises(Exception, match="301 characters"):
        await _reader(page).connect_with_person("ada-lovelace", note="x" * 301)

    assert page.posts == []


async def test_a_note_dry_run_sends_nothing():
    page = _NotePage(_top_card(NOT_INVITED))

    result = await _reader(page).connect_with_person(
        "ada-lovelace", note="Hi", dry_run=True
    )

    assert result["request"]["customMessage"] == "Hi"
    assert page.posts == []
