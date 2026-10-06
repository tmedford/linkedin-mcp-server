"""Company profile, posts, people and search through LinkedIn's API."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.voyager.company import (
    VoyagerCompany,
    parse_companies,
    parse_company,
    parse_demographics,
)

URN = "urn:li:fs_normalized_company:1001"


def _company() -> dict[str, Any]:
    return {
        "data": {"*elements": [URN]},
        "included": [
            # Other companies ride along (affiliates); only the listed one is it.
            {"entityUrn": "urn:li:fs_normalized_company:9", "name": "Other"},
            {
                "entityUrn": URN,
                "name": "Acme",
                "universalName": "acme",
                "staffCount": 7868,
                "staffCountRange": {"start": 1001, "end": 5000},
                "headquarter": {"city": "San Francisco", "country": "US"},
                "foundedOn": {"year": 2016},
                "companyType": {"localizedName": "Privately Held"},
                "*companyIndustries": ["urn:li:fs_industry:4"],
                "*followingInfo": "urn:li:fs_followingInfo:1",
            },
            {"entityUrn": "urn:li:fs_industry:4", "localizedName": "Software"},
            {"entityUrn": "urn:li:fs_followingInfo:1", "followerCount": 492652},
        ],
    }


def _people(names: list[str], *, total: int = 7469) -> dict[str, Any]:
    entities = [
        {
            "entityUrn": f"urn:li:fsd_entityResultViewModel:(urn:li:fsd_profile:ACoAA-{n},X,Y)",
            "title": {"text": n.title()},
            "navigationUrl": f"https://www.linkedin.com/in/{n}",
        }
        for n in names
    ]
    return {
        "data": {
            "metadata": {
                "totalResultCount": total,
                "primaryFilterCluster": {
                    "filters": [
                        {
                            "parameterName": "currentFunction",
                            "primaryFilterValues": [
                                {"displayName": "Sales", "count": 2294, "value": "25"}
                            ],
                        },
                        # The same filter again, without counts: not used.
                        {
                            "parameterName": "currentFunction",
                            "primaryFilterValues": [{"displayName": "Sales"}],
                        },
                        {
                            "parameterName": "resultType",
                            "primaryFilterValues": [
                                {"displayName": "People", "count": 1, "value": "P"}
                            ],
                        },
                    ]
                },
            },
            "elements": [
                {
                    "items": [
                        {"itemUnion": {"*entityResult": e["entityUrn"]}}
                        for e in entities
                    ]
                }
            ],
        },
        "included": entities,
    }


class _Page:
    def __init__(self, *answers: Any):
        self.answers = list(answers)
        self.requests: list[str] = []

    async def evaluate(self, _program: str, argument: Any = None) -> Any:
        self.requests.append(argument)
        return {"body": json.dumps(self.answers.pop(0))}


def _reader(*answers: Any) -> tuple[VoyagerCompany, _Page]:
    page = _Page(*answers)
    session = MagicMock()
    session.page = page
    session.delay = AsyncMock()
    return VoyagerCompany(session, MagicMock()), page


def test_the_listed_company_is_read_with_its_numeric_id():
    company = parse_company(_company())

    assert company is not None
    assert (company["name"], company["company_id"]) == ("Acme", "1001")
    assert company["industries"] == ["Software"]
    assert company["followers"] == 492652
    assert company["headquarters"] == "San Francisco, US"
    assert company["staff_range"] == [1001, 5000]


def test_demographics_are_the_filter_values_that_carry_counts():
    assert parse_demographics(_people(["ada"])) == {
        "functions": [{"name": "Sales", "count": 2294, "id": "25"}]
    }


async def test_company_people_looks_the_company_up_then_searches_by_its_id():
    reader, page = _reader(_company(), _people(["ada", "grace"]))

    result = await reader.get_company_people("acme", keywords="product lead")

    assert "universalName=acme" in page.requests[0]
    assert "currentCompany:List(1001)" in page.requests[1]
    assert "ORGANIZATIONS_PEOPLE_ALUMNI" in page.requests[1]
    assert "keywords:product%20lead" in page.requests[1]
    assert [p["public_identifier"] for p in result["people"]] == ["ada", "grace"]
    assert result["total"] == 7469 and result["company_id"] == "1001"
    assert result["demographics"]["functions"][0]["count"] == 2294


async def test_company_people_can_be_narrowed_to_schools_by_id():
    reader, page = _reader(_company(), _people(["ada"]))

    await reader.get_company_people("acme", schools=["3558", "3881"])

    assert (
        "(currentCompany:List(1001),schoolFilter:List(3558,3881),"
        "resultType:List(ORGANIZATION_ALUMNI))" in page.requests[1]
    )


async def test_company_people_without_schools_sends_no_school_filter():
    reader, page = _reader(_company(), _people(["ada"]))

    await reader.get_company_people("acme")

    assert "schoolFilter" not in page.requests[1]


async def test_a_school_name_is_refused_before_any_request():
    reader, page = _reader(_company(), _people(["ada"]))

    with pytest.raises(LinkedInScraperException, match="school ids"):
        await reader.get_company_people("acme", schools=["Georgia Tech"])

    assert page.requests == []


@pytest.mark.parametrize("school", ["٣٥٥٨", "３５５８", "²"])
async def test_a_school_id_in_non_ascii_digits_is_refused(school):
    reader, page = _reader(_company(), _people(["ada"]))

    with pytest.raises(LinkedInScraperException, match="school ids"):
        await reader.get_company_people("acme", schools=[school])

    assert page.requests == []


async def test_an_unknown_company_is_refused_by_name():
    reader, _ = _reader({"data": {"*elements": []}, "included": []})

    with pytest.raises(LinkedInScraperException, match="no company named 'nope'"):
        await reader.get_company("nope")


def test_company_search_keeps_companies_and_counts_every_item():
    result = {
        "entityUrn": "urn:li:fsd_entityResultViewModel:(urn:li:fsd_company:1001,X,Y)",
        "trackingUrn": "urn:li:company:1001",
        "title": {"text": "Acme"},
        "primarySubtitle": {"text": "Software • San Francisco"},
        "navigationUrl": "https://www.linkedin.com/company/acme/",
    }
    payload = {
        "data": {
            "elements": [
                {
                    "items": [
                        {"itemUnion": {"*entityResult": result["entityUrn"]}},
                        {"itemUnion": {"*feedbackCard": "urn:li:fsd_card:1"}},
                    ]
                }
            ]
        },
        "included": [result],
    }

    companies, found, seen = parse_companies(payload)

    assert (found, seen) == (True, 2)
    assert companies == [
        {
            "name": "Acme",
            "company_id": "1001",
            "universal_name": "acme",
            "detail": "Software • San Francisco",
            "url": "/company/acme/",
        }
    ]


async def test_company_posts_drop_promotions_and_keep_the_page_end_honest():
    update = "urn:li:fs_updateV2:(urn:li:activity:7511799000000000000,X)"
    payload = {
        "data": {
            "*elements": [update, "urn:li:fs_updateV2:promo"],
            "paging": {"total": 501},
        },
        "included": [
            {
                "$type": "com.linkedin.voyager.feed.render.UpdateV2",
                "entityUrn": update,
                "updateMetadata": {"urn": "urn:li:activity:7511799000000000000"},
                "actor": {"name": {"text": "Acme"}},
                "commentary": {"text": {"text": "We shipped"}},
            },
            {
                "$type": "com.linkedin.voyager.feed.render.UpdateV2",
                "entityUrn": "urn:li:fs_updateV2:promo",
                "updateMetadata": {"urn": "urn:li:inAppPromotion:9"},
            },
        ],
    }
    reader, page = _reader(payload)

    result = await reader.get_company_posts("acme", count=2)

    assert "q=companyRelevanceFeed" in page.requests[0]
    assert "companyIdOrUniversalName=acme" in page.requests[0]
    assert [p["text"] for p in result["posts"]] == ["We shipped"]
    # Two entries came back for two asked: a full page, though one was a promo.
    assert result["at_end"] is False and result["total"] == 501


async def test_company_search_reports_how_many_the_search_matched():
    result = {
        "entityUrn": "urn:li:fsd_entityResultViewModel:(urn:li:fsd_company:1001,X,Y)",
        "trackingUrn": "urn:li:company:1001",
        "title": {"text": "Acme"},
        "navigationUrl": "https://www.linkedin.com/company/acme/",
    }
    reader, _ = _reader(
        {
            "data": {
                # paging.total is the cap on what can be paged, not the match.
                "paging": {"total": 1000},
                "metadata": {"totalResultCount": 5534},
                "elements": [
                    {"items": [{"itemUnion": {"*entityResult": result["entityUrn"]}}]}
                ],
            },
            "included": [result],
        }
    )

    assert (await reader.find_companies("fintech"))["total"] == 5534


def test_a_company_lists_its_offices_and_showcase_pages():
    payload = _company()
    company = next(e for e in payload["included"] if e["entityUrn"] == URN)
    company["confirmedLocations"] = [
        {
            "description": "HQ",
            "city": "San Francisco",
            "country": "US",
            "headquarter": True,
        },
        {"city": "London", "country": "GB"},
    ]
    company["showcasePages"] = ["urn:li:fs_normalized_company:55"]
    payload["included"].append(
        {"entityUrn": "urn:li:fs_normalized_company:55", "name": "Acme IT"}
    )

    parsed = parse_company(payload)

    assert parsed is not None
    assert parsed["locations"] == [
        {
            "description": "HQ",
            "city": "San Francisco",
            "country": "US",
            "headquarter": True,
        },
        {"city": "London", "country": "GB", "headquarter": False},
    ]
    assert parsed["showcase_pages"] == [{"name": "Acme IT", "company_id": "55"}]


def test_a_company_result_carries_linkedins_line_about_your_tie_to_it():
    result = {
        "entityUrn": "urn:li:fsd_entityResultViewModel:(urn:li:fsd_company:1001,X,Y)",
        "trackingUrn": "urn:li:company:1001",
        "title": {"text": "Acme"},
        "navigationUrl": "https://www.linkedin.com/company/acme/",
        "insights": [
            {
                "simpleInsight": {
                    "title": {"text": "1 person from your school was hired here"}
                }
            }
        ],
    }
    payload = {
        "data": {
            "elements": [
                {"items": [{"itemUnion": {"*entityResult": result["entityUrn"]}}]}
            ]
        },
        "included": [result],
    }

    assert (
        parse_companies(payload)[0][0]["insight"]
        == "1 person from your school was hired here"
    )
