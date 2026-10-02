"""``get_profile_views``: who viewed the signed-in member's profile."""

from __future__ import annotations

import json
from typing import Any
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.voyager import profile_views as views_module
from linkedin_mcp_server.voyager.profile_views import (
    VoyagerProfileViews,
    parse_stream_rows,
)

STREAM = (
    Path(__file__).parent / "fixtures" / "voyager" / "wvmp-entity-list.rsc.txt"
).read_text()


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


class _Page:
    """Answers the JSON read, then plays the part of a page that loads rows."""

    def __init__(self, payload: dict[str, Any], batches: list[list[str]] | None):
        self.payload = payload
        self.batches = list(batches or [])
        self.captured: list[dict[str, str]] = []
        self.requests: list[Any] = []
        self.init_scripts: list[str] = []
        self.main_world_reads = 0
        self.scrolls = 0

    async def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)

    async def evaluate(
        self, program: str, argument: Any = None, *, isolated_context: bool = True
    ) -> Any:
        if "__liMcpRsc" in program:
            # What the page's world holds is invisible from the isolated one.
            if isolated_context:
                return []
            self.main_world_reads += 1
            return list(self.captured)
        if "scrollTo" in program:
            self.scrolls += 1
            if self.batches:
                self.captured += [
                    {"url": "/rsc-action/x", "text": text}
                    for text in self.batches.pop(0)
                ]
            return 0
        self.requests.append(argument)
        return {"body": json.dumps(self.payload)}


def _reader(
    payload: dict[str, Any], batches: list[list[str]] | None = None
) -> tuple[VoyagerProfileViews, Any]:
    page = _Page(payload, batches)
    session = MagicMock()
    session.page = page
    session.check_rate_limit = AsyncMock()
    session.delay = AsyncMock()
    navigator = MagicMock()
    navigator._navigate_to_page = AsyncMock()
    reader = VoyagerProfileViews(session, navigator)
    setattr(reader, "test_page", page)
    setattr(reader, "test_navigator", navigator)
    return reader, page.requests


async def test_identified_viewers_come_back_newest_first_with_exact_times():
    reader, requests = _reader(_payload())

    result = await reader.get_profile_views(full=False)

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

    viewers = (await reader.get_profile_views(full=False))["viewers"]

    sean = [v for v in viewers if v["name"] == "Sean Foreman"]
    assert len(sean) == 1
    assert sean[0]["viewed_at"] == 9_000
    assert sean[0]["referrer"] == "Homepage"
    assert sean[0]["seen_in"] == ["Most recent viewers", "Zuora"]


async def test_private_viewers_and_rollups_are_kept_apart_from_people():
    reader, _ = _reader(_payload())

    result = await reader.get_profile_views(full=False)

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

    groups = (await reader.get_profile_views(full=False))["groups"]

    assert [(g["kind"], g["label"], g["views"]) for g in groups] == [
        ("recent", "Most recent viewers", 529),
        ("notable", "Notable viewers", 9),
        ("company", "Zuora", 14),
        # A group's referrer is attributed text, not a string.
        ("source", "Homepage", 29),
    ]


async def test_it_says_it_is_not_every_viewer():
    reader, _ = _reader(_payload())

    result = await reader.get_profile_views(full=False)

    assert result["count"] == 2
    assert result["returned"] == 3
    # The list was not read at all, which is neither complete nor cut short.
    assert result["complete"] is None


async def test_an_answer_without_the_card_is_refused_rather_than_read_as_no_viewers():
    reader, _ = _reader(_payload(card=False))

    with pytest.raises(LinkedInScraperException, match="changed shape"):
        await reader.get_profile_views(full=False)


def _row(slug: str, name: str, viewed: str) -> str:
    """One rendered row as the stream carries it, on its own line."""
    return (
        '1:["$","div",null,{"viewTrackingSpecs":{"viewName":"viewer-list-item"},'
        f'"url":"https://www.linkedin.com/in/{slug}",'
        f'"children":[[null,"{name}",["$","span",null,{{}}]]],'
        '"a":{"children":["\u2022 2nd"]},"b":{"children":["Engineer"]},'
        f'"c":{{"children":["{viewed}"]}}}}]'
    )


def test_real_stream_rows_parse_into_people_private_viewers_and_rollups():
    now = datetime(2026, 10, 2, 21, 0, tzinfo=timezone.utc)

    rows = parse_stream_rows(STREAM, now)

    assert rows[0] == {
        "description": "Salesperson at Example Co",
        "viewed_text": "Viewed 3h ago",
        "viewed_at_iso": "2026-10-02T18:00+00:00",
        "viewed_at_approximate": True,
    }
    assert rows[1]["public_identifier"] == "ada-lovelace"
    assert rows[1]["name"] == "Ada Lovelace"
    assert rows[1]["degree"] == "1st"
    assert rows[1]["headline"] == "Engineer at Analytical Engine"
    assert rows[2] == {
        "aggregate": "133 recruiters viewed your profile - From Example Corp and other companies"
    }
    # The mutual-connection count is a REFERENCE to another line of the stream.
    assert rows[3]["extra"] == ["15 mutual connections"]
    assert rows[3]["viewed_at_iso"] == "2026-10-02T12:00+00:00"


def test_the_degree_badge_is_not_mistaken_for_a_time():
    # "\u2022 1st" contains a digit and an "s". Read as a relative time it put
    # the badge where the time belongs and the time where the headline does.
    rows = parse_stream_rows(STREAM, datetime(2026, 10, 2, tzinfo=timezone.utc))

    assert rows[1]["viewed_text"] == "Viewed 3h ago"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Viewed 3h ago", "2026-10-02T18:00+00:00"),
        ("Viewed 2d ago", "2026-09-30T21:00+00:00"),
        ("Viewed 1w ago", "2026-09-25T21:00+00:00"),
        ("Viewed 2mo ago", "2026-08-03T21:00+00:00"),
        ("no time here", None),
    ],
)
def test_a_relative_time_becomes_an_approximate_instant(text, expected):
    now = datetime(2026, 10, 2, 21, 0, tzinfo=timezone.utc)

    assert views_module._approximate(text, now) == expected


async def test_the_full_list_is_read_from_the_pages_own_answers_and_nothing_is_sent():
    reader, requests = _reader(
        _payload(),
        batches=[
            [_row("ilan", "Ilan Rado", "Viewed 1w ago")],
            [_row("newcomer", "New Comer", "Viewed 2w ago")],
        ],
    )

    result = await reader.get_profile_views()

    page = getattr(reader, "test_page")
    names = {v.get("name") for v in result["viewers"]}
    assert {"Ilan Rado", "New Comer", "Sean Foreman"} <= names
    assert result["complete"] is True
    # One request of our own: the JSON endpoint. The list is the page's doing.
    assert requests == ["https://www.linkedin.com/voyager/api/identity/wvmpCards"]
    getattr(reader, "test_navigator")._navigate_to_page.assert_awaited_once_with(
        "https://www.linkedin.com/analytics/profile-views/"
    )
    assert page.init_scripts and "window.fetch" in page.init_scripts[0]
    # Read from the page's own world; the isolated one holds nothing.
    assert page.main_world_reads > 0


async def test_a_viewer_in_both_sources_keeps_the_exact_time_and_gains_the_row():
    reader, _ = _reader(
        _payload(), batches=[[_row("ilan", "Ilan Rado", "Viewed 1w ago")]]
    )

    viewers = (await reader.get_profile_views())["viewers"]

    ilan = next(v for v in viewers if v.get("public_identifier") == "ilan")
    assert ilan["viewed_at"] == 7_000
    assert ilan["viewed_text"] == "Viewed 1w ago"
    assert ilan["notable_reason"] == "a senior leader"
    assert "viewed_at_approximate" not in ilan

    reader, _ = _reader(
        _payload(), batches=[[_row("newcomer", "New Comer", "Viewed 2w ago")]]
    )
    viewers = (await reader.get_profile_views())["viewers"]
    newcomer = next(v for v in viewers if v.get("public_identifier") == "newcomer")
    assert newcomer["viewed_at_approximate"] is True


async def test_a_list_still_growing_at_the_round_limit_is_reported_as_cut_short(
    monkeypatch,
):
    monkeypatch.setattr(views_module, "MAX_ROUNDS", 3)
    endless = [[_row(f"p{i}", f"Person {i}", "Viewed 1d ago")] for i in range(10)]
    reader, _ = _reader(_payload(), batches=endless)

    result = await reader.get_profile_views()

    assert result["complete"] is False


async def test_the_same_row_delivered_twice_is_one_viewer():
    row = _row("newcomer", "New Comer", "Viewed 2w ago")
    reader, _ = _reader(_payload(), batches=[[row], [row]])

    viewers = (await reader.get_profile_views())["viewers"]

    assert sum(v.get("public_identifier") == "newcomer" for v in viewers) == 1
