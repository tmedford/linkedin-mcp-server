"""``search_people``: people search through LinkedIn's search API."""

from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.scraping.contracts import FilterValidationError
from linkedin_mcp_server.server import create_mcp_server
from linkedin_mcp_server.voyager import overlay
from linkedin_mcp_server.voyager.people_search import VoyagerPeopleSearch


def _result(index: int, *, distance: str = "DISTANCE_2") -> dict[str, Any]:
    return {
        "entityUrn": (
            "urn:li:fsd_entityResultViewModel:"
            f"(urn:li:fsd_profile:ACoAA-p{index},SEARCH_SRP,DEFAULT)"
        ),
        "title": {"text": f"Person {index}"},
        "primarySubtitle": {"text": "Recruiter"},
        "secondarySubtitle": {"text": "New York"},
        "navigationUrl": f"https://www.linkedin.com/in/person-{index}?miniProfileUrn=x",
        "entityCustomTrackingInfo": {"memberDistance": distance},
        "insights": [{"simpleInsight": {"title": {"text": "3 mutual connections"}}}],
    }


def _page_of(order: list[int], *, total: int = 150, key: str = "elements") -> dict:
    entities = [_result(index) for index in order]
    items = [{"itemUnion": {"*entityResult": e["entityUrn"]}} for e in entities]
    data: dict[str, Any] = {"paging": {"total": total}}
    if key:
        data[key] = [{"items": items}] if items else []
    # `included` deliberately in REVERSE: the order is the items list's.
    return {"body": json.dumps({"data": data, "included": list(reversed(entities))})}


GEO = {
    "body": json.dumps(
        {
            "data": {
                "elements": [
                    {
                        "trackingUrn": "urn:li:geo:105080838",
                        "title": {"text": "New York, United States"},
                    },
                    {
                        "trackingUrn": "urn:li:geo:90000070",
                        "title": {"text": "New York City Metropolitan Area"},
                    },
                ]
            }
        }
    )
}


class _Page:
    def __init__(self, *answers: Any):
        self._answers = list(answers)
        self.requests: list[str] = []

    async def evaluate(self, _program: str, argument: Any) -> Any:
        self.requests.append(argument)
        return self._answers.pop(0)


def _search(*answers: Any) -> tuple[VoyagerPeopleSearch, _Page]:
    page = _Page(*answers)
    session = MagicMock()
    session.page = page
    return VoyagerPeopleSearch(session, MagicMock()), page


async def test_people_come_back_as_records_in_the_services_order():
    search, page = _search(_page_of([3, 1, 2]))

    result = await search.find_people("recruiter", count=3)

    assert [p["name"] for p in result["people"]] == ["Person 3", "Person 1", "Person 2"]
    first = result["people"][0]
    assert first["public_identifier"] == "person-3"
    assert first["profile_url"] == "/in/person-3/"
    assert first["profile_urn"] == "urn:li:fsd_profile:ACoAA-p3"
    assert first["degree"] == "2nd"
    assert first["insight"] == "3 mutual connections"
    assert result["references"]["search_results"][0] == {
        "kind": "person",
        "url": "/in/person-3/",
        "text": "Person 3",
        "context": "search result",
    }
    assert "Person 3 • 2nd" in result["sections"]["search_results"]
    assert len(page.requests) == 1
    assert "keywords:recruiter," in page.requests[0]
    assert "queryParameters:(resultType:List(PEOPLE))" in page.requests[0]


async def test_filters_become_query_parameters():
    search, page = _search(_page_of([1]))

    await search.find_people(
        "engineer", network=["F", "S"], current_company="229978", start=10, count=25
    )

    url = page.requests[0]
    assert "network:List(F,S)" in url
    assert "currentCompany:List(229978)" in url
    assert url.endswith("&start=10&count=25")


async def test_a_place_name_is_resolved_and_the_choice_is_reported():
    search, page = _search(GEO, _page_of([1]))

    result = await search.find_people("recruiter", location="New York")

    assert "geoUrn:List(105080838)" in page.requests[1]
    assert result["location_resolved"] == {
        "geo_id": "105080838",
        "name": "New York, United States",
    }
    # The runner-up is the city; a caller can see the state was chosen.
    assert result["location_candidates"][1]["name"] == "New York City Metropolitan Area"


async def test_a_numeric_location_is_used_as_the_geo_without_a_lookup():
    search, page = _search(_page_of([1]))

    await search.find_people("recruiter", location="90000070")

    assert len(page.requests) == 1
    assert "geoUrn:List(90000070)" in page.requests[0]


async def test_an_unrecognised_place_is_refused_rather_than_searched_unfiltered():
    search, page = _search({"body": json.dumps({"data": {"elements": []}})})

    with pytest.raises(LinkedInScraperException, match="does not recognise"):
        await search.find_people("recruiter", location="Remote")

    assert len(page.requests) == 1


async def test_the_end_is_measured_from_the_page_never_from_the_reported_total():
    # The service reports 150 whatever the query. A short page is the end.
    short, _ = _search(_page_of([1, 2], total=150))
    result = await short.find_people("recruiter", count=10)
    assert result["at_end"] is True
    assert result["total_reported"] == 150

    full, _ = _search(_page_of(list(range(10)), total=150))
    assert (await full.find_people("recruiter", count=10))["at_end"] is False

    empty, _ = _search(_page_of([]))
    assert (await empty.find_people("recruiter"))["at_end"] is None


async def test_a_moved_container_is_refused_rather_than_read_as_no_results():
    search, _ = _search(_page_of([], key=""))

    with pytest.raises(LinkedInScraperException, match="changed shape"):
        await search.find_people("recruiter")


async def test_keywords_cannot_inject_query_syntax():
    search, page = _search(_page_of([1]))

    await search.find_people("vp, eng (platform):x")

    assert "keywords:vp%2C%20eng%20%28platform%29%3Ax," in page.requests[0]


@pytest.mark.parametrize(
    "arguments",
    [{"network": ["X"]}, {"current_company": "Zuora"}],
)
async def test_a_filter_linkedin_would_ignore_is_refused_before_any_request(arguments):
    search, page = _search()

    with pytest.raises(FilterValidationError):
        await search.find_people("recruiter", **arguments)

    assert page.requests == []


@pytest.mark.parametrize(
    "arguments", [{"keywords": " "}, {"start": -1}, {"count": 0}, {"count": 51}]
)
async def test_unusable_paging_or_keywords_are_refused_before_any_request(arguments):
    search, page = _search()

    with pytest.raises(LinkedInScraperException):
        call: dict[str, Any] = {"keywords": "recruiter", **arguments}
        await search.find_people(**call)

    assert page.requests == []


async def _tool() -> Any:
    tool = await create_mcp_server().get_tool("search_people")
    assert tool is not None
    return cast(FunctionTool, tool)


async def test_the_served_search_people_is_this_forks_and_keeps_upstreams_arguments():
    tool = await _tool()

    assert overlay.SUPERSEDED["search_people"] == "search_people"
    assert tool.fn.__module__ == "linkedin_mcp_server.voyager.overlay"
    assert {"keywords", "location", "network", "current_company"} <= set(
        tool.parameters["properties"]
    )
    assert tool.parameters["required"] == ["keywords"]


async def test_the_tool_forwards_upstream_style_arguments(mock_context):
    extractor = MagicMock()
    extractor.find_people = AsyncMock(return_value={"people": []})
    tool = await _tool()

    await tool.fn(
        "recruiter",
        mock_context,
        location="New York",
        network=["F"],
        current_company="1115",
        extractor=extractor,
    )

    extractor.find_people.assert_awaited_once_with(
        "recruiter",
        "New York",
        network=["F"],
        current_company="1115",
        start=0,
        count=10,
    )


async def test_a_refused_filter_reaches_the_caller_with_its_correction(mock_context):
    extractor = MagicMock()
    extractor.find_people = AsyncMock(
        side_effect=FilterValidationError("current_company must be a numeric id")
    )
    tool = await _tool()

    with pytest.raises(ToolError, match="numeric id"):
        await tool.fn("recruiter", mock_context, extractor=extractor)


async def test_a_full_page_with_a_promo_in_it_is_not_the_end():
    page = json.loads(_page_of(list(range(10)))["body"])
    # One item is a promo: no profile id and no /in/ link, so it is dropped.
    # Nine people from ten items is a full page, not the last one.
    promo = page["included"][0]
    promo["entityUrn"] = "urn:li:fsd_entityResultViewModel:promo"
    promo["navigationUrl"] = "https://www.linkedin.com/premium/"
    for item in page["data"]["elements"][0]["items"]:
        if item["itemUnion"]["*entityResult"].endswith("ACoAA-p9,SEARCH_SRP,DEFAULT)"):
            item["itemUnion"]["*entityResult"] = promo["entityUrn"]
    search, _ = _search({"body": json.dumps(page)})

    result = await search.find_people("recruiter", count=10)

    assert result["count"] == 9
    assert result["at_end"] is False
