"""``search_jobs`` and ``get_job_details`` through LinkedIn's job API."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInOperationError
from linkedin_mcp_server.voyager.jobs import (
    VoyagerJobs,
    parse_job_cards,
    parse_posting,
    selected_filters,
)


def _card(job_id: int, *, footers: tuple[str, ...] = ("LISTED_DATE",)) -> dict:
    return {
        "$type": "com.linkedin.voyager.dash.jobs.JobPostingCard",
        "entityUrn": f"urn:li:fsd_jobPostingCard:({job_id},JOBS_SEARCH)",
        "jobPostingUrn": f"urn:li:fsd_jobPosting:{job_id}",
        "jobPostingTitle": f"VP Product {job_id}",
        "primaryDescription": {"text": "Acme"},
        "secondaryDescription": {"text": "New York, NY (Remote)"},
        "relevanceInsight": {"text": {"text": "You'd be a top applicant"}},
        "logo": {
            "attributes": [
                {"detailDataUnion": {"companyLogo": "urn:li:fsd_company:77"}}
            ]
        },
        "footerItems": [
            {"type": kind, "timeAt": 1_700_000_000_000} for kind in footers
        ],
    }


def _page(
    ids: list[int], *, total: int = 2076, easy: frozenset[int] = frozenset()
) -> dict:
    cards = [
        _card(
            i,
            footers=("LISTED_DATE", "EASY_APPLY_TEXT")
            if i in easy
            else ("LISTED_DATE",),
        )
        for i in ids
    ]
    return {
        "body": json.dumps(
            {
                "data": {
                    "paging": {"total": total},
                    "elements": [
                        {"jobCardUnion": {"*jobPostingCard": c["entityUrn"]}}
                        for c in cards
                    ],
                },
                # Deliberately reversed: the order is the elements list's.
                "included": list(reversed(cards)),
            }
        )
    }


class _Page:
    def __init__(self, *answers: Any):
        self.answers = list(answers)
        self.requests: list[Any] = []

    async def evaluate(self, _program: str, argument: Any = None) -> Any:
        self.requests.append(argument)
        return self.answers.pop(0)


def _jobs(*answers: Any) -> tuple[VoyagerJobs, _Page]:
    page = _Page(*answers)
    session = MagicMock()
    session.page = page
    session.delay = AsyncMock()
    return VoyagerJobs(session, MagicMock()), page


def test_cards_come_back_in_linkedins_order_with_typed_footers():
    payload = json.loads(_page([3, 1, 2], easy=frozenset({1}))["body"])

    jobs, found, seen = parse_job_cards(payload)

    assert (found, seen) == (True, 3)
    assert [j["job_id"] for j in jobs] == ["3", "1", "2"]
    first = jobs[1]
    assert first["company"] == "Acme" and first["company_id"] == "77"
    assert first["insight"] == "You'd be a top applicant"
    assert first["easy_apply"] is True and jobs[0]["easy_apply"] is False
    assert first["listed_at_iso"] == "2023-11-14T22:13+00:00"


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"date_posted": "past_week"}, "timePostedRange:List(r604800)"),
        ({"experience_level": "director,executive"}, "experience:List(5,6)"),
        ({"job_type": "F"}, "jobType:List(F)"),
        ({"work_type": "remote"}, "workplaceType:List(2)"),
        ({"sort_by": "date"}, "sortBy:List(DD)"),
        ({"easy_apply": True}, "applyWithLinkedin:List(true)"),
    ],
)
def test_filters_are_sent_as_linkedins_codes(arguments, expected):
    assert selected_filters(**arguments) == expected


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"work_type": "remote,anywhere"}, "on_site, remote, hybrid"),
        ({"date_posted": "yesterday"}, "past_hour"),
        ({"sort_by": "date,relevance"}, "one value"),
    ],
)
def test_a_value_linkedin_would_ignore_is_refused(arguments, message):
    with pytest.raises(LinkedInOperationError, match=message):
        selected_filters(**arguments)


async def test_pages_are_read_until_a_short_one():
    full = list(range(1, 26))
    jobs, page = _jobs(_page(full), _page([100, 101]))

    result = await jobs.find_jobs("vp product", max_pages=3)

    assert result["count"] == 27 and result["complete"] is True
    assert result["total"] == 2076
    assert ["&start=0" in r for r in page.requests] == [True, False]
    assert "&start=25" in page.requests[1]
    assert result["job_ids"][:2] == ["1", "2"]


async def test_max_pages_stops_the_walk_and_says_so():
    jobs, page = _jobs(_page(list(range(1, 26))))

    result = await jobs.find_jobs("vp product", max_pages=1)

    assert len(page.requests) == 1
    assert result["complete"] is False


async def test_a_place_becomes_a_geo_filter_and_is_reported():
    geo = {
        "body": json.dumps(
            {
                "data": {
                    "elements": [
                        {
                            "trackingUrn": "urn:li:geo:105080838",
                            "title": {"text": "New York"},
                        }
                    ]
                }
            }
        )
    }
    jobs, page = _jobs(geo, _page([1]))

    result = await jobs.find_jobs("vp product", "New York", max_pages=1)

    assert "locationUnion:(geoId:105080838)" in page.requests[1]
    assert result["location_resolved"]["geo_id"] == "105080838"


async def test_nothing_is_written():
    # The page posts every search to the member's search history. Every
    # request here is a read through the shared fetch: a URL, never a body.
    jobs, page = _jobs(_page([1]))

    await jobs.find_jobs("vp product", max_pages=1)

    assert all(isinstance(r, str) for r in page.requests)
    assert not any("action=" in r for r in page.requests)


def _posting(**overrides: Any) -> dict:
    data = {
        "jobPostingId": 4468054306,
        "title": "VP, Product",
        "companyDetails": {"company": "urn:li:fs_normalized_company:1441"},
        "formattedLocation": "New York, NY",
        "workplaceTypes": ["urn:li:fs_workplaceType:3"],
        "listedAt": 1_700_000_000_000,
        "jobState": "LISTED",
        "applyMethod": {
            "$type": "com.linkedin.voyager.jobs.OffsiteApply",
            "companyApplyUrl": "https://careers.example.com/1",
        },
        "description": {
            "text": "About the roleWhat you will do",
            "attributes": [
                {"start": 14, "attributeKindUnion": {"lineBreak": {}}},
            ],
        },
        **overrides,
    }
    return {
        "data": data,
        "included": [
            {
                "entityUrn": "urn:li:fs_normalized_company:1441",
                "name": "Google",
                "url": "https://www.linkedin.com/company/google",
            }
        ],
    }


def test_a_posting_reads_whole_with_its_line_breaks_restored():
    job = parse_posting(_posting())

    assert job is not None
    assert (job["company"], job["company_id"]) == ("Google", "1441")
    assert job["workplace"] == ["hybrid"]
    assert job["description"] == "About the role\nWhat you will do"
    assert job["easy_apply"] is False
    assert job["apply_url"] == "https://careers.example.com/1"


def test_an_onsite_apply_method_is_easy_apply():
    job = parse_posting(
        _posting(applyMethod={"$type": "com.linkedin.voyager.jobs.ComplexOnsiteApply"})
    )

    assert job is not None and job["easy_apply"] is True


async def test_get_job_reads_the_rest_posting():
    jobs, page = _jobs({"body": json.dumps(_posting())})

    result = await jobs.get_job("4468054306")

    assert "jobs/jobPostings/4468054306?decorationId=" in page.requests[0]
    assert "WebFullJobPosting-65" in page.requests[0]
    assert result["job"]["title"] == "VP, Product"
    assert "About the role" in result["sections"]["job_posting"]


@pytest.mark.parametrize("bad", ["abc", "https://www.linkedin.com/jobs/view/1/", ""])
async def test_a_job_id_that_is_not_a_number_is_refused_before_any_request(bad):
    jobs, page = _jobs()

    with pytest.raises(LinkedInOperationError, match="numeric id"):
        await jobs.get_job(bad)

    assert page.requests == []


def test_a_company_filter_takes_numeric_ids_only():
    assert selected_filters(company_id="17988315,1441") == "company:List(17988315,1441)"
    with pytest.raises(LinkedInOperationError, match="numeric company id"):
        selected_filters(company_id="Rippling")


async def test_a_page_that_adds_nothing_new_ends_the_walk():
    same = list(range(1, 26))
    jobs, page = _jobs(_page(same), _page(same), _page(same))

    result = await jobs.find_jobs("vp product", max_pages=3)

    assert len(page.requests) == 2
    assert result["count"] == 25 and result["complete"] is True
