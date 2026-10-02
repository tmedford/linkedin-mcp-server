"""``get_person``: a whole profile from the API, and what it shares with yours."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from unittest.mock import MagicMock

import pytest

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    LinkedInScraperException,
)
from linkedin_mcp_server.voyager import person as person_module
from linkedin_mcp_server.voyager.person import (
    VoyagerPersonReader,
    common_ground,
    overlap,
    parse_profile,
)

ME = "urn:li:fsd_profile:ACoAA-me"
ADA = "urn:li:fsd_profile:ACoAA-ada"
ZUORA = "urn:li:fsd_company:229978"
GATECH = "urn:li:fsd_school:18158"
ME_ANSWER = {"body": json.dumps({"included": [{"dashEntityUrn": ME}]})}


def _date(text: str | None) -> dict[str, int] | None:
    if text is None:
        return None
    parts = [int(part) for part in text.split("-")]
    return dict(zip(("year", "month", "day"), parts))


def _span(start: str | None, end: str | None) -> dict[str, Any]:
    return {"start": _date(start), "end": _date(end)}


def _profile(
    urn: str,
    name: str,
    *,
    jobs: Sequence[tuple[str, str | None, str, str | None, str | None]] = (),
    schools: Sequence[tuple[str, str | None, str | None, str | None]] = (),
    skills: Sequence[str] = (),
    skill_total: int | None = None,
    location: str | None = None,
) -> dict[str, Any]:
    """A FullProfileWithEntities answer. One position group per job."""
    included: list[dict[str, Any]] = []
    groups = []
    for index, (company, company_urn, title, start, end) in enumerate(jobs):
        group = f"{urn}:group{index}"
        groups.append(group)
        row: dict[str, Any] = {
            "entityUrn": f"{group}:job",
            "title": title,
            "companyName": company,
            "dateRange": _span(start, end),
        }
        if company_urn:
            row["*company"] = company_urn
        included += [
            {"entityUrn": group, "*profilePositionInPositionGroup": f"{group}:rows"},
            {"entityUrn": f"{group}:rows", "*elements": [f"{group}:job"]},
            row,
        ]
    school_urns = []
    for index, (school, school_urn, start, end) in enumerate(schools):
        entity: dict[str, Any] = {
            "entityUrn": f"{urn}:school{index}",
            "schoolName": school,
            "dateRange": _span(start, end),
        }
        if school_urn:
            entity["*school"] = school_urn
        school_urns.append(entity["entityUrn"])
        included.append(entity)
    skill_urns = []
    for index, skill in enumerate(skills):
        skill_urns.append(f"{urn}:skill{index}")
        included.append({"entityUrn": skill_urns[-1], "name": skill})
    first, last = name.split(" ", 1)
    profile: dict[str, Any] = {
        "entityUrn": urn,
        "firstName": first,
        "lastName": last,
        "publicIdentifier": name.lower().replace(" ", "-"),
        "headline": "Engineer",
        "*profilePositionGroups": f"{urn}:groups",
        "*profileEducations": f"{urn}:schools",
        "*profileSkills": f"{urn}:skills",
    }
    if location:
        profile["geoLocation"] = {"*geo": f"{urn}:geo"}
        included.append({"entityUrn": f"{urn}:geo", "defaultLocalizedName": location})
    included += [
        profile,
        {
            "entityUrn": f"{urn}:groups",
            "*elements": groups,
            "paging": {"total": len(groups)},
        },
        {
            "entityUrn": f"{urn}:schools",
            "*elements": school_urns,
            "paging": {"total": len(school_urns)},
        },
        {
            "entityUrn": f"{urn}:skills",
            "*elements": skill_urns,
            "paging": {
                "total": skill_total if skill_total is not None else len(skills)
            },
        },
    ]
    return {"data": {"*elements": [urn]}, "included": included}


def _relationship(key: str) -> dict[str, Any]:
    union: dict[str, Any] = {key: {} if key == "self" else "urn:li:fsd_connection:x"}
    entity = {
        "$type": "com.linkedin.voyager.dash.relationships.MemberRelationship",
        "memberRelationshipUnion": union,
    }
    return {"body": json.dumps({"included": [entity]})}


def _body(payload: dict[str, Any]) -> dict[str, Any]:
    return {"body": json.dumps(payload)}


class _Page:
    def __init__(self, *answers: Any):
        self._answers = list(answers)
        self.requests: list[Any] = []

    async def evaluate(self, _program: str, argument: Any) -> Any:
        self.requests.append(argument)
        return self._answers.pop(0)


def _reader(*answers: Any) -> tuple[VoyagerPersonReader, _Page]:
    page = _Page(*answers)
    session = MagicMock()
    session.page = page
    return VoyagerPersonReader(session, MagicMock()), page


@pytest.fixture(autouse=True)
def _no_cached_profile():
    person_module.forget_my_profile()
    yield
    person_module.forget_my_profile()


MINE = _profile(
    ME,
    "Taylor Medford",
    jobs=[
        ("Zuora", ZUORA, "Director", "2022-02", None),
        ("Zuora", ZUORA, "Engineer", "2013-11", "2016-08"),
    ],
    schools=[("Georgia Tech", GATECH, "2007", "2011")],
    skills=["Python", "Billing"],
    location="New York",
)


def test_every_title_at_an_employer_is_a_position_not_just_the_group():
    # Positions are nested under a group per employer. Reading the groups as
    # positions keeps one row per employer and loses the titles.
    payload = _profile(ADA, "Ada Lovelace")
    group = f"{ADA}:group"
    payload["included"] += [
        {"entityUrn": group, "*profilePositionInPositionGroup": f"{group}:rows"},
        {"entityUrn": f"{group}:rows", "*elements": [f"{group}:a", f"{group}:b"]},
        {"entityUrn": f"{group}:a", "title": "Director", "companyName": "Zuora"},
        {"entityUrn": f"{group}:b", "title": "Engineer", "companyName": "Zuora"},
    ]
    for entity in payload["included"]:
        if entity["entityUrn"] == f"{ADA}:groups":
            entity["*elements"] = [group]
            entity["paging"] = {"total": 1}

    positions = parse_profile(payload)["positions"]

    assert [p["title"] for p in positions["items"]] == ["Director", "Engineer"]
    assert positions["employers"] == 1
    assert positions["complete"] is True


def test_a_capped_section_reports_what_it_is_missing():
    parsed = parse_profile(
        _profile(ADA, "Ada Lovelace", skills=["a", "b"], skill_total=34)
    )

    assert parsed["skills"]["returned"] == 2
    assert parsed["skills"]["total"] == 34
    assert parsed["skills"]["complete"] is False
    assert parsed["education"]["complete"] is True


def test_a_section_with_no_total_is_unknown_not_complete():
    payload = _profile(ADA, "Ada Lovelace", skills=["a"])
    for entity in payload["included"]:
        if entity["entityUrn"] == f"{ADA}:skills":
            del entity["paging"]

    assert parse_profile(payload)["skills"]["complete"] is None


@pytest.mark.parametrize(
    ("mine", "theirs", "expected"),
    [
        (("2013-11", "2016-08"), ("2015-01", "2018-01"), ("2015-01", "2016-08", 20)),
        (("2013-11", "2016-08"), ("2016-08", "2018-01"), ("2016-08", "2016-08", 1)),
        (("2013-11", "2016-08"), ("2016-09", "2018-01"), None),
        (("2022-02", None), ("2020-01", None), ("2022-02", None, None)),
        (("2022-02", None), ("2019-01", "2021-12"), None),
        # A bare year spans the whole year on both ends.
        (("2007", "2011"), ("2011", "2015"), ("2011-01", "2011-12", 12)),
        # No start date cannot be placed, so it overlaps nothing.
        ((None, "2016-08"), ("2015-01", "2018-01"), None),
    ],
)
def test_overlap_is_the_shared_span_and_nothing_more(mine, theirs, expected):
    result = overlap(
        {"start": mine[0], "end": mine[1]}, {"start": theirs[0], "end": theirs[1]}
    )

    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert (result["start"], result["end"], result["months"]) == expected


def test_working_together_needs_the_same_employer_at_the_same_time():
    theirs = parse_profile(
        _profile(
            ADA,
            "Ada Lovelace",
            jobs=[
                ("Zuora", ZUORA, "PM", "2015-01", "2018-01"),
                ("Stripe", "urn:li:fsd_company:9", "PM", "2018-02", None),
            ],
        )
    )

    ground = common_ground(parse_profile(MINE), theirs)

    # Shared employer twice over: once overlapping, once not.
    assert len(ground["companies"]) == 2
    assert len(ground["worked_together"]) == 1
    together = ground["worked_together"][0]
    assert together["matched_by"] == "urn"
    assert together["mine"]["title"] == "Engineer"
    assert together["theirs"]["title"] == "PM"
    assert together["overlap"] == {"start": "2015-01", "end": "2016-08", "months": 20}


def test_the_same_name_under_two_different_ids_is_not_the_same_place():
    theirs = parse_profile(
        _profile(
            ADA,
            "Ada Lovelace",
            jobs=[("Zuora", "urn:li:fsd_company:777", "PM", "2015-01", "2018-01")],
        )
    )

    assert common_ground(parse_profile(MINE), theirs)["companies"] == []


def test_a_free_text_employer_matches_by_name_and_says_so():
    theirs = parse_profile(
        _profile(ADA, "Ada Lovelace", jobs=[(" zuora ", None, "PM", "2015-01", None)])
    )

    companies = common_ground(parse_profile(MINE), theirs)["companies"]

    assert {c["matched_by"] for c in companies} == {"name"}
    assert len(companies) == 2


def test_shared_schools_skills_and_location_are_found():
    theirs = parse_profile(
        _profile(
            ADA,
            "Ada Lovelace",
            schools=[("Georgia Institute of Technology", GATECH, "2010", "2014")],
            skills=["python", "Go"],
            location="New York",
        )
    )

    ground = common_ground(parse_profile(MINE), theirs)

    assert ground["schools"][0]["matched_by"] == "urn"
    assert ground["schools"][0]["overlap"]["start"] == "2010-01"
    assert ground["skills"] == ["python"]
    assert ground["same_location"] is True


async def test_a_profile_comes_back_whole_with_common_ground_and_no_navigation():
    theirs = _profile(
        ADA, "Ada Lovelace", jobs=[("Zuora", ZUORA, "PM", "2015-01", "2018-01")]
    )
    reader, page = _reader(
        _body(theirs), _relationship("*connection"), ME_ANSWER, _body(MINE)
    )

    result = await reader.get_person("ada-lovelace")

    assert result["identity"]["name"] == "Ada Lovelace"
    assert result["relationship"] == "connection"
    assert result["url"] == "https://www.linkedin.com/in/ada-lovelace/"
    assert len(result["common_ground"]["worked_together"]) == 1
    assert "PM at Zuora (2015-01 to 2018-01)" in result["sections"]["profile"]
    assert result["incomplete_sections"] == []
    assert len(page.requests) == 4
    assert "FullProfileWithEntities" in page.requests[0]
    assert "memberIdentity=ada-lovelace" in page.requests[0]


async def test_your_own_profile_is_read_once_across_calls():
    theirs = _body(_profile(ADA, "Ada Lovelace"))
    reader, page = _reader(
        theirs,
        _relationship("*connection"),
        ME_ANSWER,
        _body(MINE),
        theirs,
        _relationship("*connection"),
    )

    await reader.get_person("ada-lovelace")
    second = await reader.get_person("ada-lovelace")

    assert "common_ground" in second
    assert len(page.requests) == 6


async def test_your_own_profile_is_not_compared_with_itself():
    reader, page = _reader(_body(MINE), _relationship("self"))

    result = await reader.get_person("taylor-medford")

    assert result["relationship"] == "self"
    assert "common_ground" not in result
    assert len(page.requests) == 2


async def test_skipping_the_comparison_reads_nothing_of_yours():
    reader, page = _reader(
        _body(_profile(ADA, "Ada Lovelace")), _relationship("*connection")
    )

    result = await reader.get_person("ada-lovelace", compare_to_me=False)

    assert "common_ground" not in result
    assert len(page.requests) == 2


async def test_an_unreadable_relationship_is_unknown_and_the_profile_still_returns():
    reader, _ = _reader(
        _body(_profile(ADA, "Ada Lovelace")),
        {"error": "HTTP 400", "status": 400},
        ME_ANSWER,
        _body(MINE),
    )

    result = await reader.get_person("ada-lovelace")

    assert result["relationship"] is None
    assert result["identity"]["name"] == "Ada Lovelace"


async def test_a_capped_section_is_named_in_incomplete_sections():
    reader, _ = _reader(
        _body(_profile(ADA, "Ada Lovelace", skills=["a"], skill_total=34)),
        _relationship("*connection"),
    )

    result = await reader.get_person("ada-lovelace", compare_to_me=False)

    assert result["incomplete_sections"] == ["skills"]
    assert "Skills (first 1 of 34)" in result["sections"]["profile"]


async def test_nobody_found_and_a_moved_shape_are_different_failures():
    nobody, _ = _reader(
        {"body": json.dumps({"data": {"elements": []}, "included": []})}
    )
    with pytest.raises(LinkedInScraperException, match="not exactly one"):
        await nobody.get_person("ghost")

    moved, _ = _reader({"body": json.dumps({"data": {"other": 1}, "included": []})})
    with pytest.raises(LinkedInScraperException, match="changed shape"):
        await moved.get_person("ghost")


async def test_a_rejected_session_raises_as_authentication():
    reader, _ = _reader({"error": "HTTP 403", "status": 403})

    with pytest.raises(AuthenticationError):
        await reader.get_person("ada-lovelace")


def test_several_title_pairs_at_one_employer_roll_up_to_one_span():
    mine = parse_profile(
        _profile(
            ME,
            "Taylor Medford",
            jobs=[
                ("Zuora", ZUORA, "Senior Manager", "2019-02", "2022-02"),
                ("Zuora", ZUORA, "Manager", "2016-09", "2019-02"),
                ("Zuora", ZUORA, "Engineer", "2013-11", "2016-08"),
            ],
        )
    )
    theirs = parse_profile(
        _profile(
            ADA, "Ada Lovelace", jobs=[("Zuora", ZUORA, "Leader", "2015-03", "2019-05")]
        )
    )

    ground = common_ground(mine, theirs)

    assert len(ground["worked_together"]) == 3
    assert ground["worked_together_by_employer"] == [
        {"company": "Zuora", "start": "2015-03", "end": "2019-05", "months": 51}
    ]


def test_a_gap_between_stints_is_not_counted_and_ongoing_has_no_month_count():
    mine = parse_profile(
        _profile(
            ME,
            "Taylor Medford",
            jobs=[
                ("Zuora", ZUORA, "Second stint", "2020-01", "2020-03"),
                ("Zuora", ZUORA, "First stint", "2015-01", "2015-02"),
                ("Stripe", "urn:li:fsd_company:9", "Now", "2023-01", None),
            ],
        )
    )
    theirs = parse_profile(
        _profile(
            ADA,
            "Ada Lovelace",
            jobs=[
                ("Zuora", ZUORA, "Whole time", "2014-01", "2021-01"),
                ("Stripe", "urn:li:fsd_company:9", "Now", "2022-01", None),
            ],
        )
    )

    rolled = {
        r["company"]: r
        for r in common_ground(mine, theirs)["worked_together_by_employer"]
    }

    assert rolled["Zuora"]["months"] == 5
    assert (rolled["Zuora"]["start"], rolled["Zuora"]["end"]) == ("2015-01", "2020-03")
    assert rolled["Stripe"] == {
        "company": "Stripe",
        "start": "2023-01",
        "end": None,
        "months": None,
    }
