"""``get_profile_views``: who viewed the signed-in member's profile."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.voyager.profile_views import VoyagerProfileViews


def _mini(slug: str, first: str, last: str) -> dict[str, Any]:
    return {
        "entityUrn": f"urn:li:fs_miniProfile:{slug}",
        "firstName": first,
        "lastName": last,
        "occupation": "VP Product",
        "publicIdentifier": slug,
        "dashEntityUrn": f"urn:li:fsd_profile:{slug}",
    }


def _person_card(urn: str, slug: str, at: int, **extra: Any) -> dict[str, Any]:
    return {
        "entityUrn": urn,
        "value": {
            "viewer": {
                "profile": {
                    "*miniProfile": f"urn:li:fs_miniProfile:{slug}",
                    "distance": {"value": "DISTANCE_2"},
                }
            },
            "viewedAt": at,
            "pendingInvitee": False,
            **extra,
        },
    }


def _payload(*, card: bool = True) -> dict[str, Any]:
    included: list[dict[str, Any]] = [
        _mini("ilan", "Ilan", "Rado"),
        _mini("sean", "Sean", "Foreman"),
        {"entityUrn": "urn:li:fs_miniCompany:1", "name": "Zuora"},
        _person_card("urn:li:fs_card:sean-new", "sean", 9_000, referrer="Homepage"),
        # The same person again in another group, at an OLDER time.
        _person_card("urn:li:fs_card:sean-old", "sean", 5_000),
        _person_card(
            "urn:li:fs_card:ilan",
            "ilan",
            7_000,
            insights=[{"value": {"relevanceReason": {"text": "a senior leader"}}}],
        ),
        {
            "entityUrn": "urn:li:fs_card:anon",
            "value": {
                "viewer": {
                    "obfuscationString": "Recruiter at DualEntry",
                    "occupation": {"entityName": "DualEntry"},
                },
                "viewedAt": 8_000,
            },
        },
        {
            "entityUrn": "urn:li:fs_card:agg",
            "value": {
                "wvmpCardType": "AGGREGATED_RECRUITER",
                "headline": {"text": "133 people with the job title Recruiter"},
                "viewedAt": 9_500,
            },
        },
    ]
    if card:
        included.append(
            {
                "$type": "com.linkedin.voyager.identity.me.WvmpCard",
                "value": {
                    "insightCards": [
                        {
                            "objectUrn": "urn:li:wvmp:summary",
                            "value": {
                                "numViews": 529,
                                "timeFrame": "LAST_90_DAYS",
                                "numViewsChangeInPercentage": 0,
                                "*cards": [
                                    "urn:li:fs_card:agg",
                                    "urn:li:fs_card:sean-new",
                                    "urn:li:fs_card:anon",
                                ],
                            },
                        },
                        {
                            "objectUrn": "urn:li:wvmp:notableViewers",
                            "value": {"numViews": 9, "*cards": ["urn:li:fs_card:ilan"]},
                        },
                        {
                            "objectUrn": "urn:li:company:1",
                            "value": {
                                "numViews": 14,
                                "*miniCompany": "urn:li:fs_miniCompany:1",
                                "*cards": [
                                    "urn:li:fs_card:sean-old",
                                    "urn:li:fs_card:anon",
                                ],
                            },
                        },
                        {
                            "objectUrn": "urn:li:source:LINKEDIN_HOME",
                            "value": {"numViews": 29, "referrer": {"text": "Homepage"}},
                        },
                    ]
                },
            }
        )
    return {"data": {"*elements": ["x"]}, "included": included}


def _reader(payload: dict[str, Any]) -> tuple[VoyagerProfileViews, list[Any]]:
    requests: list[Any] = []

    class _Page:
        async def evaluate(self, _program: str, argument: Any) -> Any:
            requests.append(argument)
            return {"body": json.dumps(payload)}

    session = MagicMock()
    session.page = _Page()
    return VoyagerProfileViews(session, MagicMock()), requests


async def test_identified_viewers_come_back_newest_first_with_exact_times():
    reader, requests = _reader(_payload())

    result = await reader.get_profile_views()

    assert [v["name"] for v in result["viewers"]] == ["Sean Foreman", "Ilan Rado"]
    ilan = result["viewers"][1]
    assert ilan["public_identifier"] == "ilan"
    assert ilan["degree"] == "2nd"
    assert ilan["viewed_at_iso"] == "1970-01-01T00:00+00:00"
    assert ilan["notable_reason"] == "a senior leader"
    assert ilan["pending_invite"] is False
    assert result["total_views"] == 529
    assert result["time_frame"] == "LAST_90_DAYS"
    assert requests == ["https://www.linkedin.com/voyager/api/identity/wvmpCards"]


async def test_a_person_in_two_groups_is_one_viewer_at_their_latest_view():
    reader, _ = _reader(_payload())

    viewers = (await reader.get_profile_views())["viewers"]

    sean = [v for v in viewers if v["name"] == "Sean Foreman"]
    assert len(sean) == 1
    assert sean[0]["viewed_at"] == 9_000
    assert sean[0]["referrer"] == "Homepage"
    assert sean[0]["seen_in"] == ["Most recent viewers", "Zuora"]


async def test_private_viewers_and_rollups_are_kept_apart_from_people():
    reader, _ = _reader(_payload())

    result = await reader.get_profile_views()

    # In two groups, counted once.
    assert result["anonymous_viewers"] == [
        {
            "description": "Recruiter at DualEntry",
            "company": "DualEntry",
            "viewed_at": 8_000,
            "viewed_at_iso": "1970-01-01T00:00+00:00",
            "seen_in": "Most recent viewers",
        }
    ]
    assert result["aggregates"] == [
        {
            "kind": "AGGREGATED_RECRUITER",
            "description": "133 people with the job title Recruiter",
        }
    ]
    assert "133 people" in result["sections"]["profile_views"]


async def test_groups_are_named_as_linkedin_names_them():
    reader, _ = _reader(_payload())

    groups = (await reader.get_profile_views())["groups"]

    assert [(g["kind"], g["label"], g["views"]) for g in groups] == [
        ("recent", "Most recent viewers", 529),
        ("notable", "Notable viewers", 9),
        ("company", "Zuora", 14),
        # A group's referrer is attributed text, not a string.
        ("source", "Homepage", 29),
    ]


async def test_it_says_it_is_not_every_viewer():
    reader, _ = _reader(_payload())

    result = await reader.get_profile_views()

    assert result["count"] == 2
    assert result["returned"] == 3
    assert result["complete"] is False


async def test_an_answer_without_the_card_is_refused_rather_than_read_as_no_viewers():
    reader, _ = _reader(_payload(card=False))

    with pytest.raises(LinkedInScraperException, match="changed shape"):
        await reader.get_profile_views()
