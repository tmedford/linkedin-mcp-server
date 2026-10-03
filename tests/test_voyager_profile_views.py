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
    """Answers the JSON read, and plays LinkedIn's paging endpoint for the list."""

    def __init__(
        self, payload: dict[str, Any], rows: list[str] | None, status: int = 200
    ):
        self.payload = payload
        self.rows = list(rows or [])
        self.status = status
        self.requests: list[Any] = []
        self.windows: list[dict[str, Any]] = []
        self.listeners: list[Any] = []

    def on(self, _event: str, callback: Any) -> None:
        self.listeners.append(callback)

    def remove_listener(self, _event: str, callback: Any) -> None:
        self.listeners.remove(callback)

    async def evaluate(self, _program: str, argument: Any = None) -> Any:
        if isinstance(argument, dict) and "body" in argument:
            body = json.loads(argument["body"])
            window = body["clientArguments"]["payload"]
            self.windows.append(
                {
                    "url": argument["url"],
                    "headers": argument["headers"],
                    "start": window["start"],
                    "count": window["count"],
                    "period": body["clientArguments"]["states"][0]["value"],
                    "body": body,
                }
            )
            chunk = self.rows[window["start"] : window["start"] + window["count"]]
            return {"status": self.status, "text": "\n".join(chunk)}
        self.requests.append(argument)
        return {"body": json.dumps(self.payload)}


PAGE_HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "csrf-token": "ajax:1",
    "x-li-track": "{}",
    "x-li-page-instance": "urn:li:page:d_flagship3_leia_wvmp;abc",
    # Browser-owned: a script may not set these, so they must not be copied.
    "cookie": "li_at=secret",
    "user-agent": "Chrome",
    "sec-fetch-mode": "cors",
    "referer": "https://www.linkedin.com/analytics/profile-views/",
}


def _reader(
    payload: dict[str, Any],
    rows: list[str] | None = None,
    status: int = 200,
    emits: bool = True,
) -> tuple[VoyagerProfileViews, Any]:
    page = _Page(payload, rows, status)
    session = MagicMock()
    session.page = page
    session.check_rate_limit = AsyncMock()
    session.delay = AsyncMock()
    navigator = MagicMock()

    async def navigate(_url: str) -> None:
        if emits:
            request = MagicMock(
                url="https://www.linkedin.com/flagship-web/rsc-action/actions/x",
                method="POST",
                headers=dict(PAGE_HEADERS),
            )
            for listener in list(page.listeners):
                listener(request)

    navigator._navigate_to_page = AsyncMock(side_effect=navigate)
    reader = VoyagerProfileViews(session, navigator)
    setattr(reader, "test_page", page)
    setattr(reader, "test_navigator", navigator)
    return reader, page.requests


@pytest.fixture(autouse=True)
def _no_cached_headers():
    views_module.forget_cached_headers()
    yield
    views_module.forget_cached_headers()


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
        "title": "Salesperson",
        "company": "Example Co",
        "company_id": "1",
        "search_url": (
            "https://www.linkedin.com/search/results/people/"
            "?keywords=Salesperson&origin=WHO_VIEWED_ME&currentCompany=1"
        ),
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


async def test_the_full_list_is_paged_through_the_api_with_the_pages_own_headers():
    rows = [_row(f"p{i}", f"Person {i}", "Viewed 1w ago") for i in range(45)]
    reader, requests = _reader(_payload(), rows)

    result = await reader.get_profile_views()

    page = getattr(reader, "test_page")
    assert len(result["viewers"]) == 45 + 2  # the list, plus the two it lacks
    assert result["complete"] is True
    # Two windows: a full one of 40, then a short one that is the end.
    assert [(w["start"], w["count"]) for w in page.windows] == [(0, 40), (40, 40)]
    assert all("/rsc-action/actions/pagination" in w["url"] for w in page.windows)
    # The default period is sent explicitly, as the page sends it.
    assert page.windows[0]["period"] == ["WvmpSearchFilterTimeRange_LAST_90_DAYS"]
    sent = page.windows[0]["headers"]
    assert sent["x-li-track"] == "{}" and sent["csrf-token"] == "ajax:1"
    assert not {"cookie", "user-agent", "sec-fetch-mode", "referer"} & set(sent)
    assert requests == ["https://www.linkedin.com/voyager/api/identity/wvmpCards"]


async def test_the_page_is_loaded_once_and_its_headers_reused():
    rows = [_row("p1", "Person 1", "Viewed 1w ago")]
    reader, _ = _reader(_payload(), rows)

    await reader.get_profile_views()
    await reader.get_profile_views(days=7)

    getattr(reader, "test_navigator")._navigate_to_page.assert_awaited_once_with(
        "https://www.linkedin.com/analytics/profile-views/"
    )
    assert getattr(reader, "test_page").listeners == []


async def test_the_body_matches_the_request_the_page_itself_sends():
    # Start and count sit in two places and the period in one. A body that
    # sets only one of the two is a request the page never makes.
    reader, _ = _reader(_payload(), [_row("p1", "Person 1", "Viewed 2d ago")])

    await reader.get_profile_views(days=365)

    body = getattr(reader, "test_page").windows[0]["body"]
    assert body["pagerId"] == "com.linkedin.sdui.premium.wvmp.entityList"
    inner = body["paginationRequest"]["requestedArguments"]["payload"]
    outer = body["clientArguments"]["payload"]
    assert (
        (inner["start"], inner["count"]) == (outer["start"], outer["count"]) == (0, 40)
    )
    states = body["clientArguments"]["states"]
    assert [s["value"] for s in states] == [
        ["WvmpSearchFilterTimeRange_LAST_365_DAYS"],
        [],
        [],
        [],
        [],
    ]
    assert len(outer["filterTypeList"]) == 5


async def test_a_viewer_in_both_sources_keeps_the_exact_time_and_gains_the_row():
    reader, _ = _reader(_payload(), [_row("ilan", "Ilan Rado", "Viewed 1w ago")])

    viewers = (await reader.get_profile_views())["viewers"]

    ilan = next(v for v in viewers if v.get("public_identifier") == "ilan")
    assert ilan["viewed_at"] == 7_000
    assert ilan["viewed_text"] == "Viewed 1w ago"
    assert ilan["notable_reason"] == "a senior leader"
    assert "viewed_at_approximate" not in ilan

    views_module.forget_cached_headers()
    reader, _ = _reader(_payload(), [_row("newcomer", "New Comer", "Viewed 2w ago")])
    viewers = (await reader.get_profile_views())["viewers"]
    newcomer = next(v for v in viewers if v.get("public_identifier") == "newcomer")
    assert newcomer["viewed_at_approximate"] is True


async def test_a_list_longer_than_the_page_limit_is_reported_as_cut_short(monkeypatch):
    monkeypatch.setattr(views_module, "MAX_PAGES", 2)
    rows = [_row(f"p{i}", f"Person {i}", "Viewed 1d ago") for i in range(200)]
    reader, _ = _reader(_payload(), rows)

    result = await reader.get_profile_views()

    assert result["complete"] is False
    assert len(getattr(reader, "test_page").windows) == 2


async def test_a_row_older_than_the_period_means_the_period_was_ignored():
    reader, _ = _reader(_payload(), [_row("old", "Old Viewer", "Viewed 5mo ago")])
    assert (await reader.get_profile_views(days=7))["period_applied"] is False

    views_module.forget_cached_headers()
    reader, _ = _reader(_payload(), [_row("new", "New Viewer", "Viewed 3d ago")])
    result = await reader.get_profile_views(days=7)
    assert result["period_applied"] is True
    assert result["days"] == 7

    views_module.forget_cached_headers()
    reader, _ = _reader(_payload(), [_row("new", "New Viewer", "Viewed 3d ago")])
    assert (await reader.get_profile_views())["period_applied"] is None


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, "AuthenticationError"),
        (429, "RateLimitError"),
        (500, "LinkedInScraperException"),
    ],
)
async def test_a_refused_list_request_raises_as_what_it_is(status, error):
    from linkedin_mcp_server.core import exceptions

    reader, _ = _reader(_payload(), [_row("p", "P Q", "Viewed 1d ago")], status=status)

    with pytest.raises(getattr(exceptions, error)):
        await reader.get_profile_views()


async def test_a_page_that_sends_nothing_to_copy_headers_from_is_an_error():
    reader, _ = _reader(_payload(), [_row("p", "P Q", "Viewed 1d ago")], emits=False)

    with pytest.raises(LinkedInScraperException, match="no component request"):
        await reader.get_profile_views()

    assert getattr(reader, "test_page").windows == []


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"days": 30}, "7, 14, 28, 90, 365"),
        ({"days": 90, "full": False}, "needs the full list"),
    ],
)
async def test_an_unusable_period_is_refused_before_any_request(arguments, message):
    reader, requests = _reader(_payload())

    with pytest.raises(LinkedInScraperException, match=message):
        await reader.get_profile_views(**arguments)

    assert requests == []


def _private_row(description: str, query: str) -> str:
    """A private viewer's row: a description and LinkedIn's search for them."""
    return (
        '1:["$","div",null,{"viewTrackingSpecs":{"viewName":"viewer-list-item"},'
        f'"url":"https://www.linkedin.com/search/results/people/?{query}",'
        f'"a":{{"children":["{description}"]}},'
        '"c":{"children":["Viewed 2d ago"]}}]'
    )


def test_a_private_viewer_without_an_employer_keeps_their_industry_and_place():
    row = parse_stream_rows(
        _private_row(
            "Founder in the Staffing and Recruiting industry from Greater Boston",
            "keywords=Founder&origin=WHO_VIEWED_ME&industry=104&geoUrn=90000512",
        )
    )[0]

    assert (row["title"], row["industry_id"], row["geo_id"]) == (
        "Founder",
        "104",
        "90000512",
    )
    # No employer id means the words after the title are not an employer.
    assert "company" not in row and "company_id" not in row


async def test_filters_are_sent_as_the_values_linkedin_accepts():
    reader, _ = _reader(_payload(), [_row("p1", "Person 1", "Viewed 2d ago")])

    result = await reader.get_profile_views(
        interesting="senior_leader_in_your_industry",
        company_id="229978",
        industry_id="4",
        geo_id="90000070",
    )

    body = getattr(reader, "test_page").windows[0]["body"]
    assert [s["value"] for s in body["clientArguments"]["states"]] == [
        ["WvmpSearchFilterTimeRange_LAST_90_DAYS"],
        ["InterestingViewerType_SENIOR_LEADER_IN_YOUR_INDUSTRY"],
        ["229978"],
        ["4"],
        ["90000070"],
    ]
    assert result["filters"]["company_id"] == "229978"
    # The unfiltered highlights are not put back into a filtered answer.
    assert [v["public_identifier"] for v in result["viewers"]] == ["p1"]


async def test_a_filter_that_matches_nobody_is_an_empty_list_not_the_highlights():
    reader, _ = _reader(_payload(), [])

    result = await reader.get_profile_views(company_id="229978")

    assert result["viewers"] == []


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"company_id": "urn:li:fsd_company:229978"}, "numeric id"),
        ({"geo_id": "New York"}, "numeric id"),
        ({"interesting": "recruiters"}, "senior_leader_in_your_industry"),
        ({"company_id": "229978", "full": False}, "Filters need the full list"),
    ],
)
async def test_an_unusable_filter_is_refused_before_any_request(arguments, message):
    reader, requests = _reader(_payload())

    with pytest.raises(LinkedInScraperException, match=message):
        await reader.get_profile_views(**arguments)

    assert requests == []


def test_a_private_viewer_known_only_by_school_keeps_the_school():
    row = parse_stream_rows(
        _private_row(
            "Someone at Example University",
            "keywords=&origin=WHO_VIEWED_ME&school=Example+University",
        )
    )[0]

    assert row["school"] == "Example University"
    assert "title" not in row and "company_id" not in row


async def test_the_relevant_order_is_asked_for_and_kept_as_linkedin_gave_it():
    rows = [
        _row("old", "Old But Relevant", "Viewed 3w ago"),
        _row("new", "New Viewer", "Viewed 1h ago"),
    ]
    reader, _ = _reader(_payload(), rows)

    result = await reader.get_profile_views(sort="relevant")

    body = getattr(reader, "test_page").windows[0]["body"]
    assert body["clientArguments"]["payload"]["sortType"] == (
        "ProfileViewSortType_RELEVANCE_DESCENDING"
    )
    # Not re-sorted by time: LinkedIn's first stays first.
    assert [v["public_identifier"] for v in result["viewers"]][:2] == ["old", "new"]
    assert result["sort"] == "relevant"


async def test_the_default_order_is_newest_first():
    rows = [
        _row("old", "Old Viewer", "Viewed 3w ago"),
        _row("new", "New Viewer", "Viewed 1h ago"),
    ]
    reader, _ = _reader(_payload(), rows)

    result = await reader.get_profile_views()

    body = getattr(reader, "test_page").windows[0]["body"]
    assert body["clientArguments"]["payload"]["sortType"] == (
        "ProfileViewSortType_TIME_DESCENDING"
    )
    named = [v["public_identifier"] for v in result["viewers"]]
    assert named.index("new") < named.index("old")


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"sort": "popular"}, "recent, relevant"),
        ({"sort": "relevant", "full": False}, "sort needs the full list"),
    ],
)
async def test_an_unusable_sort_is_refused_before_any_request(arguments, message):
    reader, requests = _reader(_payload())

    with pytest.raises(LinkedInScraperException, match=message):
        await reader.get_profile_views(**arguments)

    assert requests == []


def test_a_rollup_whose_text_comes_before_its_marker_is_kept():
    # The private-mode rollup closes the relevance-sorted list: its texts sit
    # ahead of the marker on the same line, and after it only a button.
    stream = (
        '1:["$","div",null,{"children":[["$","p",null,'
        '{"children":["86 LinkedIn members"]}],["$","$La",null,'
        '{"children":["These people viewed your profile in Private mode"]}],'
        '["$","$L4",null,{"viewTrackingSpecs":{"viewName":"viewer-list-item"},'
        '"children":"$Lb"}]]}]\n'
        '2:["$","$Ld",null,{"text":["Learn more"]}]'
    )

    assert parse_stream_rows(stream) == [
        {
            "aggregate": (
                "86 LinkedIn members - These people viewed your profile in Private mode"
            )
        }
    ]


async def test_two_private_viewers_with_the_same_description_and_time_are_two():
    same = _private_row(
        "Recruiter at Example Co", "keywords=Recruiter&currentCompany=1"
    )
    reader, _ = _reader(_payload(), [same, same])

    result = await reader.get_profile_views()

    described = [
        v
        for v in result["anonymous_viewers"]
        if v["description"] == "Recruiter at Example Co"
    ]
    assert len(described) == 2
