"""Tests for the invitation board reader.

The dangerous value on this surface is not an exception, it is a zero. An empty
board and a wrong parse path produce the same well-formed, plausible-looking
result, and a zero here is acted on as "there is nothing pending" -- which on
2026-09-18 would have closed a routine's entire surface over six live
invitations. So most of these are about whether a zero comes back labelled, or
comes back bare.
"""

from __future__ import annotations

from typing import Any

import pytest

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    LinkedInScraperException,
    RateLimitError,
)
from linkedin_mcp_server.voyager.invitations import VoyagerInvitationsReader

SLUG = "jane-doe-123"


def _member(urn: str = "urn:li:member:1") -> dict:
    return {
        "entityUrn": urn,
        "firstName": "Jane",
        "lastName": "Doe",
        "occupation": "Director of Engineering",
        "publicIdentifier": SLUG,
    }


def _row(
    *,
    urn: str = "urn:li:invitation:1",
    note: str | None = None,
    custom_message: bool | None = None,
    state: str = "PENDING",
    sent: int = 1_789_000_000_000,
    member: dict | str | None = None,
) -> dict:
    invitation = {
        "entityUrn": urn,
        "invitationType": "CONNECTION",
        "invitationState": state,
        "sentTime": sent,
        "sharedSecret": "s3cret",
    }
    if note is not None:
        invitation["message"] = note
    if custom_message is not None:
        invitation["customMessage"] = custom_message
    return {"invitation": invitation, "fromMember": member or _member()}


def _payload(rows: list[dict], included: list[dict] | None = None) -> dict:
    """The WRAPPED shape LinkedIn actually returns: data.data, not data."""
    return {
        "data": {"data": {"*elements": rows}},
        "included": included or [],
    }


class _Reader(VoyagerInvitationsReader):
    """Reader with the browser replaced by a scripted payload."""

    def __init__(self, payload: dict | Exception):
        super().__init__(session=object(), navigator=None)
        self._payload = payload
        self.fetched: list[str] = []

    async def _fetch(self, url: str) -> dict:  # type: ignore[override]
        self.fetched.append(url)
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class TestTheWrappedPayload:
    """The defect that motivated this module, pinned from both sides."""

    async def test_rows_are_read_from_the_wrapped_path(self):
        reader = _Reader(_payload([_row()]))
        result = await reader.get_invitations()
        assert result["count"] == 1
        assert result["invitations"][0]["public_identifier"] == SLUG

    async def test_a_payload_with_neither_shape_raises_instead_of_reporting_zero(
        self,
    ):
        """Rows under a key neither shape uses must not read as an empty board.

        Both shapes LinkedIn has sent are read (``data.data`` inline, and
        ``data`` normalized). Anything else came back with data and no rows the
        reader knows, and the guard refuses rather than report a clean zero.
        """
        moved = {"data": {"rows": [_row()]}, "included": []}
        reader = _Reader(moved)
        with pytest.raises(LinkedInScraperException, match="changed shape"):
            await reader.get_invitations()

    async def test_a_genuinely_empty_board_is_allowed_to_be_empty(self):
        """The guard must not turn "nothing pending" into an error.

        Without this, the fix for a false zero becomes a false alarm, and a
        routine that legitimately has no invitations fails every run.
        """
        reader = _Reader({"data": {"data": {"*elements": []}}, "included": []})
        result = await reader.get_invitations()
        assert result["count"] == 0
        assert result["invitations"] == []
        assert result["at_end"] is None, "an empty page proves nothing either way"
        assert result["zero_reason"] == "empty-page"


class TestTheNoteFlag:
    """customMessage is a boolean has-note flag; message carries the text."""

    async def test_note_text_and_flag_are_both_reported(self):
        reader = _Reader(_payload([_row(note="hi there", custom_message=True)]))
        item = (await reader.get_invitations())["invitations"][0]
        assert item["note"] == "hi there"
        assert item["note_length"] == 8
        assert item["has_note"] is True
        assert item["has_note_flag"] is True

    async def test_a_bare_invite_reports_no_note(self):
        reader = _Reader(_payload([_row(custom_message=False)]))
        item = (await reader.get_invitations())["invitations"][0]
        assert item["note"] == ""
        assert item["note_length"] == 0
        assert item["has_note"] is False

    async def test_a_disagreement_between_flag_and_text_stays_visible(self):
        """Both are reported rather than one silently winning.

        They have agreed on every observed row, which is exactly why a
        disagreement would be worth seeing rather than resolved away.
        """
        reader = _Reader(_payload([_row(note="text present", custom_message=False)]))
        item = (await reader.get_invitations())["invitations"][0]
        assert item["has_note_flag"] is False
        assert item["note"] == "text present"
        assert item["has_note"] is True, "text present means a note exists"


class TestPagingIsMeasured:
    async def test_a_full_page_does_not_claim_to_be_the_end(self):
        reader = _Reader(_payload([_row(urn=f"urn:{i}") for i in range(5)]))
        result = await reader.get_invitations(count=5)
        assert result["at_end"] is False

    async def test_a_short_page_is_the_end(self):
        reader = _Reader(_payload([_row(urn=f"urn:{i}") for i in range(3)]))
        result = await reader.get_invitations(count=5)
        assert result["at_end"] is True

    async def test_an_empty_page_past_the_start_says_so(self):
        reader = _Reader({"data": {"data": {"*elements": []}}, "included": []})
        result = await reader.get_invitations(start=50)
        assert result["at_end"] is None
        assert result["zero_reason"] == "after-start"

    async def test_start_and_count_reach_the_url(self):
        reader = _Reader(_payload([_row()]))
        await reader.get_invitations(start=2, count=2)
        assert "start=2" in reader.fetched[0]
        assert "count=2" in reader.fetched[0]

    async def test_paging_total_is_never_consulted(self):
        """It has read 0 against a full board on every run it was checked.

        Asserted by handing it a hostile value: a payload claiming zero while
        carrying rows must still report the rows.
        """
        payload = _payload([_row()])
        payload["data"]["data"]["paging"] = {"total": 0}
        reader = _Reader(payload)
        result = await reader.get_invitations()
        assert result["count"] == 1


class TestBoards:
    async def test_received_and_sent_use_different_endpoints(self):
        received = _Reader(_payload([_row()]))
        await received.get_invitations(direction="received")
        sent = _Reader(_payload([_row(member=None)]))
        await sent.get_invitations(direction="sent")
        assert "invitationViews" in received.fetched[0]
        assert "q=receivedInvitation" in received.fetched[0]
        assert "sentInvitationViewsV2" in sent.fetched[0]

    async def test_an_unknown_direction_is_refused_before_any_request(self):
        reader = _Reader(_payload([]))
        with pytest.raises(LinkedInScraperException, match="direction was"):
            await reader.get_invitations(direction="pending")
        assert reader.fetched == [], "a bad argument must not cost a request"

    @pytest.mark.parametrize("kwargs", [{"start": -1}, {"count": 0}])
    async def test_nonsense_paging_is_refused_before_any_request(self, kwargs):
        reader = _Reader(_payload([]))
        with pytest.raises(LinkedInScraperException, match="start must be"):
            await reader.get_invitations(**kwargs)
        assert reader.fetched == []


class TestIdentityResolution:
    async def test_a_urn_pointer_is_resolved_from_included(self):
        """Normalized payloads hand back a pointer, not the member object."""
        member = _member("urn:li:member:42")
        reader = _Reader(_payload([_row(member="urn:li:member:42")], included=[member]))
        item = (await reader.get_invitations())["invitations"][0]
        assert item["name"] == "Jane Doe"
        assert item["public_identifier"] == SLUG
        assert item["headline"] == "Director of Engineering"

    async def test_an_unresolvable_member_does_not_lose_the_invitation(self):
        """A missing identity is a blank field, never a dropped row.

        Dropping it would under-report the board, which is the same class of
        failure as a false zero, just partial and harder to notice.
        """
        reader = _Reader(_payload([_row(member="urn:li:member:missing")]))
        result = await reader.get_invitations()
        assert result["count"] == 1
        assert result["invitations"][0]["name"] is None
        assert result["invitations"][0]["state"] == "PENDING"

    async def test_sent_time_becomes_iso(self):
        reader = _Reader(_payload([_row(sent=1_789_000_000_000)]))
        item = (await reader.get_invitations())["invitations"][0]
        assert item["sent_at_iso"].endswith("Z")
        assert item["sent_at_iso"].startswith("2026-")

    async def test_a_missing_sent_time_is_none_not_epoch_zero(self):
        reader = _Reader(_payload([_row(sent=0)]))
        assert (await reader.get_invitations())["invitations"][0]["sent_at_iso"] is None


class TestTransportFailuresKeepTheirType:
    """A fault must never be mistaken for an empty board."""

    @pytest.mark.parametrize(
        "error",
        [
            AuthenticationError("rejected"),
            RateLimitError("slow down"),
            LinkedInScraperException("broke"),
        ],
    )
    async def test_the_error_propagates_rather_than_becoming_zero(self, error):
        reader = _Reader(error)
        with pytest.raises(type(error)):
            await reader.get_invitations()


ME = "urn:li:fs_miniProfile:ACoAA-me"


def _normalized(direction: str, other_slug: str, *, mutual: int | None = None) -> dict:
    """Both boards as measured 2026-10-02: rows are ids of views in included,
    each view points at an Invitation, which points at both members."""
    other = f"urn:li:fs_miniProfile:ACoAA-{other_slug}"
    view: dict[str, Any] = {
        "entityUrn": "urn:li:fs_relInvitationView:1",
        "*invitation": "urn:li:fs_relInvitation:1",
    }
    if mutual is not None:
        view["insights"] = [{"sharedInsight": {"totalCount": mutual}}]
    sender, recipient = (ME, other) if direction == "sent" else (other, ME)
    return {
        "data": {"*elements": [view["entityUrn"]]},
        "included": [
            view,
            {
                "entityUrn": "urn:li:fs_relInvitation:1",
                "*fromMember": sender,
                "*toMember": recipient,
                "invitationType": "SENT" if direction == "sent" else "PENDING",
                "sentTime": 1_790_000_000_000,
                "customMessage": False,
            },
            {
                "entityUrn": ME,
                "firstName": "Taylor",
                "lastName": "Medford",
                "publicIdentifier": "taylor-lee-medford",
                "dashEntityUrn": "urn:li:fsd_profile:ACoAA-me",
            },
            {
                "entityUrn": other,
                "firstName": "Ilan",
                "lastName": "Rado",
                "publicIdentifier": other_slug,
                "dashEntityUrn": f"urn:li:fsd_profile:ACoAA-{other_slug}",
            },
        ],
    }


class TestTheNormalizedBoards:
    @pytest.mark.parametrize("direction", ["sent", "received"])
    async def test_rows_are_followed_to_the_other_party(self, direction):
        reader = _Reader(_normalized(direction, "ilan-rado"))
        result = await reader.get_invitations(direction=direction)
        row = result["invitations"][0]
        # The OTHER party on both boards; never the signed-in member.
        assert (row["name"], row["public_identifier"]) == ("Ilan Rado", "ilan-rado")
        assert row["profile_urn"] == "urn:li:fsd_profile:ACoAA-ilan-rado"
        assert row["state"] == ("SENT" if direction == "sent" else "PENDING")

    async def test_received_rows_carry_the_mutual_connection_count(self):
        reader = _Reader(_normalized("received", "ilan-rado", mutual=7))
        result = await reader.get_invitations(direction="received")
        assert result["invitations"][0]["mutual_connections"] == 7
