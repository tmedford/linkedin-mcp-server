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
from linkedin_mcp_server.voyager import jobs as jobs_module
from linkedin_mcp_server.voyager.person import (
    VoyagerPersonReader,
    common_ground,
    overlap,
    parse_interests,
    parse_network,
    parse_page_cards,
    parse_posts,
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


def _mutual(count: int, *, total: int | None = None, start: int = 0) -> dict[str, Any]:
    elements, included = [], []
    for index in range(start, start + count):
        mini = f"urn:li:fs_miniProfile:ACoAA-m{index}"
        elements.append(
            {
                "*miniProfile": mini,
                "distance": {"value": "DISTANCE_1"},
                "introductionBrokerInsight": {
                    "preFilledText": {"text": f"Hi M{index}, could you introduce me?"}
                },
            }
        )
        included.append(
            {
                "entityUrn": mini,
                "firstName": f"M{index}",
                "lastName": "Mutual",
                "occupation": "Engineer",
                "publicIdentifier": f"m{index}",
                "dashEntityUrn": f"urn:li:fsd_profile:ACoAA-m{index}",
            }
        )
    data = {
        "elements": elements,
        "paging": {"total": count if total is None else total},
    }
    return {"body": json.dumps({"data": data, "included": included})}


CONTACT = {
    "body": json.dumps(
        {
            "included": [
                {
                    "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
                    "websites": [{"url": "https://ada.example/"}],
                    "emailAddress": None,
                    "phoneNumbers": None,
                }
            ]
        }
    )
}

RESOLVED = {"body": json.dumps({"data": {"*elements": [ADA]}, "included": []})}


def _body(payload: dict[str, Any]) -> dict[str, Any]:
    return {"body": json.dumps(payload)}


class _Page:
    def __init__(self, *answers: Any):
        self._answers = list(answers)
        self.requests: list[Any] = []

    #: The follower and connection read is answered apart from the queue: it
    #: is best effort and sits between reads the tests below count by position.
    network: Any = {"error": "HTTP 400", "status": 400}

    cards: Any = {"status": 500, "text": ""}

    async def evaluate(self, _program: str, argument: Any) -> Any:
        if isinstance(argument, dict):
            self.card_requests = [*getattr(self, "card_requests", []), argument]
            return self.cards
        if "following?q=followedEntities" in argument:
            return {"error": "HTTP 400", "status": 400}
        if "TopCardSupplementary" in str(argument):
            self.network_requests = [*getattr(self, "network_requests", []), argument]
            return self.network
        self.requests.append(argument)
        return self._answers.pop(0)


def _reader(*answers: Any) -> tuple[VoyagerPersonReader, _Page]:
    page = _Page(*answers)
    # Taking the route headers opens a page once per browser session, which
    # the saved-jobs tests cover; here they are already held.
    setattr(jobs_module, "_PREFETCH_HEADERS", (page, {"x-li-track": "{}"}))
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
    assert together["matched_by"] == "id"
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
        _body(theirs),
        _relationship("*connection"),
        CONTACT,
        _mutual(2, total=33),
        ME_ANSWER,
        _body(MINE),
    )

    result = await reader.get_person("ada-lovelace")

    assert result["identity"]["name"] == "Ada Lovelace"
    assert result["relationship"] == "connection"
    assert result["url"] == "https://www.linkedin.com/in/ada-lovelace/"
    assert len(result["common_ground"]["worked_together"]) == 1
    assert "PM at Zuora (2015-01 to 2018-01)" in result["sections"]["main_profile"]
    # Consumers of the tool this replaces read the degree off the text.
    assert "1st degree connection" in result["sections"]["main_profile"]
    assert result["incomplete_sections"] == []
    assert result["contact"] == {"websites": [{"url": "https://ada.example/"}]}
    assert result["mutual_connections"]["returned"] == 2
    assert result["mutual_connections"]["total"] == 33
    assert result["mutual_connections"]["complete"] is False
    assert len(page.requests) == 6
    assert "memberConnections?q=inCommon" in page.requests[3]
    assert "/ACoAA-ada/" in page.requests[3]
    assert "FullProfileWithEntities" in page.requests[0]
    assert "memberIdentity=ada-lovelace" in page.requests[0]


async def test_your_own_profile_is_read_once_across_calls():
    theirs = _body(_profile(ADA, "Ada Lovelace"))
    reader, page = _reader(
        theirs,
        _relationship("*connection"),
        CONTACT,
        _mutual(0),
        ME_ANSWER,
        _body(MINE),
        theirs,
        _relationship("*connection"),
        CONTACT,
        _mutual(0),
    )

    await reader.get_person("ada-lovelace")
    second = await reader.get_person("ada-lovelace")

    assert "common_ground" in second
    assert len(page.requests) == 10


async def test_your_own_profile_is_not_compared_with_itself():
    reader, page = _reader(_body(MINE), _relationship("self"), CONTACT)

    result = await reader.get_person("taylor-medford")

    assert result["relationship"] == "self"
    assert "common_ground" not in result
    assert "mutual_connections" not in result
    assert len(page.requests) == 3


async def test_skipping_the_comparison_reads_nothing_of_yours():
    reader, page = _reader(
        _body(_profile(ADA, "Ada Lovelace")),
        _relationship("*connection"),
        CONTACT,
        _mutual(1),
    )

    result = await reader.get_person("ada-lovelace", compare_to_me=False)

    assert "common_ground" not in result
    assert len(page.requests) == 4


async def test_an_unreadable_relationship_is_unknown_and_the_profile_still_returns():
    reader, _ = _reader(
        _body(_profile(ADA, "Ada Lovelace")),
        {"error": "HTTP 400", "status": 400},
        {"error": "HTTP 400", "status": 400},
        _mutual(0),
        ME_ANSWER,
        _body(MINE),
    )

    result = await reader.get_person("ada-lovelace")

    assert result["relationship"] is None
    # A failed contact read is None; a successful empty one would be {}.
    assert result["contact"] is None
    assert result["identity"]["name"] == "Ada Lovelace"


async def test_a_capped_section_is_named_in_incomplete_sections():
    reader, _ = _reader(
        _body(_profile(ADA, "Ada Lovelace", skills=["a"], skill_total=34)),
        _relationship("*connection"),
        CONTACT,
        _mutual(0),
    )

    result = await reader.get_person("ada-lovelace", compare_to_me=False)

    assert result["incomplete_sections"] == ["skills"]
    assert "Skills (first 1 of 34)" in result["sections"]["main_profile"]


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


async def test_mutual_connections_page_against_linkedins_own_total():
    reader, page = _reader(
        RESOLVED, _mutual(40, total=45), RESOLVED, _mutual(5, total=45, start=40)
    )

    first = await reader.get_mutual_connections("ada-lovelace")
    second = await reader.get_mutual_connections("ada-lovelace", start=40)

    assert first["count"] == 40
    assert first["total"] == 45
    assert first["at_end"] is False
    assert second["at_end"] is True
    assert first["mutual_connections"][0]["name"] == "M0 Mutual"
    assert first["mutual_connections"][0]["suggested_ask"].startswith("Hi M0")
    assert "start=40&count=40" in page.requests[3]


async def test_no_mutual_connections_is_a_real_zero_and_a_missing_list_is_not():
    none, _ = _reader(RESOLVED, _mutual(0))
    result = await none.get_mutual_connections("ada-lovelace")
    assert result["count"] == 0
    assert result["at_end"] is None

    moved, _ = _reader(
        RESOLVED, {"body": json.dumps({"data": {"x": 1}, "included": []})}
    )
    with pytest.raises(LinkedInScraperException, match="changed shape"):
        await moved.get_mutual_connections("ada-lovelace")


def _update(
    urn: str, activity: str, author: str, text: str, **extra: Any
) -> dict[str, Any]:
    return {
        "$type": "com.linkedin.voyager.feed.render.UpdateV2",
        "entityUrn": urn,
        "updateMetadata": {"urn": f"urn:li:activity:{activity}"},
        "actor": {"name": {"text": author}},
        "commentary": {"text": {"text": text}},
        **extra,
    }


async def test_posts_keep_the_servers_order_and_do_not_double_reshared_originals():
    own, reshare, original, repost = (
        "urn:li:fs_updateV2:own",
        "urn:li:fs_updateV2:reshare",
        "urn:li:fs_updateV2:original",
        "urn:li:fs_updateV2:repost",
    )
    payload = {
        # `included` also holds the original of the reshare. Only the three
        # listed here are the member's activity.
        "data": {"*elements": [reshare, own, repost]},
        "included": [
            _update(original, "7511096693753487361", "Kate Edwards", "the news"),
            _update(own, "7457533446958170112", "Ada Lovelace", "we are hiring"),
            _update(
                reshare,
                "7511130765242540032",
                "Ada Lovelace",
                "my comment",
                **{"*resharedUpdate": original},
            ),
            _update(
                repost,
                "7440549452710449152",
                "Mark Fleming",
                "about agents",
                header={"text": {"text": "Ada Lovelace reposted this"}},
            ),
            {
                "$type": "com.linkedin.voyager.feed.shared.SocialActivityCounts",
                "urn": "urn:li:activity:7457533446958170112",
                "numLikes": 48,
                "numComments": 0,
                "numShares": 3,
            },
        ],
    }
    reader, page = _reader(RESOLVED, {"body": json.dumps(payload)})

    result = await reader.get_person_posts("ada-lovelace", count=3)

    posts = result["posts"]
    assert [p["text"] for p in posts] == ["my comment", "we are hiring", "about agents"]
    assert posts[0]["reshared_author"] == "Kate Edwards"
    assert posts[0]["reshared_text"] == "the news"
    assert posts[1]["likes"] == 48
    # Read from the activity id, checked against LinkedIn's "5 months ago".
    assert posts[1]["posted_at_iso"] == "2026-05-05T20:55+00:00"
    assert posts[2]["repost_header"] == "Ada Lovelace reposted this"
    assert posts[2]["author"] == "Mark Fleming"
    assert result["at_end"] is False
    assert "profileUrn=urn%3Ali%3Afsd_profile%3AACoAA-ada" in page.requests[1]


async def test_a_short_page_of_posts_is_the_end_and_an_empty_one_proves_nothing():
    payload = {
        "data": {"*elements": ["urn:li:fs_updateV2:a"]},
        "included": [
            _update("urn:li:fs_updateV2:a", "7457533446958170112", "Ada", "hi")
        ],
    }
    short, _ = _reader(RESOLVED, {"body": json.dumps(payload)})
    assert (await short.get_person_posts("ada-lovelace", count=10))["at_end"] is True

    empty, _ = _reader(
        RESOLVED, {"body": json.dumps({"data": {"elements": []}, "included": []})}
    )
    assert (await empty.get_person_posts("ada-lovelace"))["at_end"] is None


async def test_a_negative_offset_is_refused_before_any_request():
    reader, page = _reader()

    with pytest.raises(LinkedInScraperException, match="start must be"):
        await reader.get_mutual_connections("ada-lovelace", start=-1)

    assert page.requests == []


async def test_posts_page_by_token_because_the_endpoint_ignores_an_offset():
    def page_of(activity: str, token: str | None) -> dict[str, Any]:
        payload = {
            "data": {
                "*elements": ["urn:li:fs_updateV2:a"],
                "metadata": {"paginationToken": token},
            },
            "included": [_update("urn:li:fs_updateV2:a", activity, "Ada", "hi")],
        }
        return {"body": json.dumps(payload)}

    reader, page = _reader(
        RESOLVED,
        page_of("7457533446958170112", "tok/1=="),
        RESOLVED,
        page_of("7446932199599370240", "tok/1=="),
    )

    first = await reader.get_person_posts("ada-lovelace", count=1)
    second = await reader.get_person_posts(
        "ada-lovelace", count=1, cursor=first["next_cursor"]
    )

    assert first["next_cursor"] == "tok/1=="
    assert "paginationToken" not in page.requests[1]
    assert "start=" not in page.requests[1]
    assert "&paginationToken=tok%2F1%3D%3D" in page.requests[3]
    # The server handed back the token it was given: that is the same page
    # again, so it is withheld rather than offered as a way forward.
    assert second["next_cursor"] is None


async def test_a_blank_posts_cursor_is_refused_rather_than_read_as_page_one():
    reader, page = _reader()

    with pytest.raises(LinkedInScraperException, match="cursor was blank"):
        await reader.get_person_posts("ada-lovelace", cursor="  ")

    assert page.requests == []


def test_contact_fields_the_member_does_not_share_are_absent_not_empty():
    from linkedin_mcp_server.voyager.person import parse_contact

    shared = {
        "included": [
            {
                "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
                "emailAddress": {"emailAddress": "ada@example.com"},
                "phoneNumbers": [
                    {"phoneNumber": {"number": "555-0100"}, "type": "MOBILE"}
                ],
                "twitterHandles": [{"name": "ada"}],
                "websites": None,
                "birthDateOn": {"month": 12, "day": 10},
            }
        ]
    }

    assert parse_contact(shared) == {
        "email": "ada@example.com",
        "phones": [{"number": "555-0100", "type": "MOBILE"}],
        "twitter": ["ada"],
        "birthday": "--12-10",
    }
    assert parse_contact({"included": []}) == {}


def test_a_position_carries_the_company_id_company_filters_take():
    parsed = parse_profile(
        _profile(
            ADA,
            "Ada Lovelace",
            jobs=[("Zuora", ZUORA, "PM", "2015-01", "2018-01")],
        )
    )

    position = parsed["positions"]["items"][0]
    # One field for a company's id, and it is the one the filters take.
    assert position["company_id"] == "229978"
    assert "company_urn" not in position


async def test_my_own_profile_is_get_person_on_the_signed_in_member():
    from unittest.mock import AsyncMock, MagicMock

    reader = VoyagerPersonReader(MagicMock(), MagicMock())
    setattr(
        reader, "_mailbox_urn", AsyncMock(return_value="urn:li:fsd_profile:ACoAA-me")
    )
    setattr(reader, "get_person", AsyncMock(return_value={"relationship": "self"}))

    result = await reader.get_me()

    # No comparison against oneself, and the id is taken from /me, not asked for.
    getattr(reader, "get_person").assert_awaited_once_with(
        "ACoAA-me", compare_to_me=False
    )
    assert result == {"relationship": "self"}


def test_a_relayed_update_takes_the_originals_tally_and_the_actors_headline():
    # "X likes this": the update has its own activity, the counts sit under
    # the original's, and the update's social detail says which.
    update = "urn:li:fs_updateV2:(urn:li:activity:7511806835692302337,X)"
    payload = {
        "data": {"*elements": [update]},
        "included": [
            {
                "$type": "com.linkedin.voyager.feed.render.UpdateV2",
                "entityUrn": update,
                "updateMetadata": {"urn": "urn:li:activity:7511806835692302337"},
                "actor": {
                    "name": {"text": "Product Growth"},
                    "description": {"text": "58,884 followers"},
                },
                "*socialDetail": "urn:li:fs_socialDetail:urn:li:activity:7509658068725612544",
            },
            {
                "$type": "com.linkedin.voyager.feed.SocialDetail",
                "entityUrn": "urn:li:fs_socialDetail:urn:li:activity:7509658068725612544",
                "*totalSocialActivityCounts": "urn:li:fs_socialActivityCounts:urn:li:activity:7509658068725612544",
            },
            {
                "$type": "com.linkedin.voyager.feed.shared.SocialActivityCounts",
                "entityUrn": "urn:li:fs_socialActivityCounts:urn:li:activity:7509658068725612544",
                "urn": "urn:li:activity:7509658068725612544",
                "numLikes": 83,
                "numComments": 0,
                "numShares": 9,
            },
        ],
    }

    post = parse_posts(payload)[0]

    assert (post["likes"], post["comments"], post["shares"]) == (83, 0, 9)
    assert post["author_headline"] == "58,884 followers"


def test_the_summary_is_unescaped_and_a_position_keeps_its_links():
    payload = _profile(
        ADA, "Ada Lovelace", jobs=[("Zuora", ZUORA, "Eng", "2020-01", None)]
    )
    profile = next(e for e in payload["included"] if e.get("entityUrn") == ADA)
    profile["summary"] = "Product &amp; Engineering"
    position = next(e for e in payload["included"] if e.get("title") == "Eng")
    position["*profileTreasuryMediaPosition"] = "urn:media"
    payload["included"] += [
        {"entityUrn": "urn:media", "*elements": ["urn:m1"]},
        {
            "entityUrn": "urn:m1",
            "title": "Press release",
            "data": {"Url": "https://example.com/pr"},
        },
    ]

    parsed = parse_profile(payload)

    assert parsed["identity"]["summary"] == "Product & Engineering"
    assert parsed["positions"]["items"][0]["media"] == [
        {"title": "Press release", "url": "https://example.com/pr"}
    ]


def test_network_counts_are_read_and_hidden_connections_are_absent_not_zero():
    profile = "urn:li:fsd_profile:A"
    payload = {
        "data": {"*elements": [profile]},
        "included": [
            {
                "entityUrn": profile,
                "*followingState": "urn:follow",
                "*connections": "urn:conn",
            },
            {"entityUrn": "urn:follow", "followerCount": 2406},
            {"entityUrn": "urn:conn", "*elements": [], "paging": {"total": 2396}},
        ],
    }

    assert parse_network(payload) == {"followers": 2406, "connections": 2396}

    payload["included"][2] = {"entityUrn": "urn:conn", "*elements": [], "paging": {}}
    assert parse_network(payload) == {"followers": 2406}


async def test_a_profile_carries_its_network_counts_and_none_when_unread():
    reader, page = _reader(_body(MINE), _relationship("self"), CONTACT)
    page.network = _body(
        {
            "data": {"*elements": [ME]},
            "included": [
                {"entityUrn": ME, "*followingState": "urn:follow"},
                {"entityUrn": "urn:follow", "followerCount": 2406},
            ],
        }
    )

    result = await reader.get_person("taylor-medford")

    assert result["network"] == {"followers": 2406}
    assert "memberIdentity=taylor-medford" in page.network_requests[0]

    reader, _ = _reader(_body(MINE), _relationship("self"), CONTACT)
    assert (await reader.get_person("taylor-medford"))["network"] is None


def _card(name: str, *children: Any) -> str:
    return json.dumps(
        [
            "$",
            "div",
            None,
            {"viewTrackingSpecs": {"viewName": name}, "children": list(children)},
        ]
    )


def _cards_stream() -> str:
    rows = {
        "1": _card(
            "profile-card-about",
            ["$", "h2", None, {"children": ["Info"]}],
            "First paragraph.",
            ["$", "br", None, {}],
            ["$", "br", None, {}],
            "$$5M raised & more.",
        ),
        "2": _card("profile-card-highlights", "Highlights", "You both work at Zuora"),
        "3": _card("insights-wvmp", "1,530 profile views", "Discover"),
        "4": _card("insights-search-appearances", "144 search appearances"),
        "5": _card("profile-opento-enrolled-career-interest", "Open to work"),
    }
    return "\n".join(f"{key}:{value}" for key, value in rows.items())


def test_page_cards_are_found_by_view_name_and_keep_paragraph_breaks():
    assert parse_page_cards(_cards_stream()) == {
        # The heading is dropped by position, whatever it says.
        "about": "First paragraph.\n\n$5M raised & more.",
        "highlights": ["You both work at Zuora"],
        "analytics": {"profile_views": 1530, "search_appearances": 144},
        "open_to_work": ["Open to work"],
    }
    assert parse_page_cards('0:["$","div",null,{}]') == {}


async def test_the_pages_about_replaces_the_flattened_summary():
    reader, page = _reader(_body(MINE), _relationship("self"), CONTACT)
    page.cards = {"status": 200, "text": _cards_stream()}
    result = await reader.get_person("taylor-medford")

    assert result["identity"]["summary"] == "First paragraph.\n\n$5M raised & more."
    assert "First paragraph.\n\n$5M" in result["sections"]["main_profile"]
    assert result["analytics"]["profile_views"] == 1530
    assert "about" not in result
    sent = page.card_requests[0]
    assert sent["url"] == "https://www.linkedin.com/flagship-web/in/taylor-medford/"
    assert json.loads(sent["body"])["isPrefetch"] is True


def test_interests_name_each_kind_of_entity_and_carry_the_total():
    payload = {
        "data": {
            "paging": {"total": 35},
            "elements": [
                {"*entity": "urn:li:fs_miniCompany:1001", "*followingInfo": "urn:f1"},
                {"*entity": "urn:li:fs_miniProfile:A", "*followingInfo": "urn:f2"},
                {"*entity": "urn:li:fs_miniGroup:9"},
            ],
        },
        "included": [
            {
                "$type": "com.linkedin.voyager.entities.shared.MiniCompany",
                "entityUrn": "urn:li:fs_miniCompany:1001",
                "name": "Acme",
                "universalName": "acme",
            },
            {"entityUrn": "urn:f1", "followerCount": 12},
            {
                "$type": "com.linkedin.voyager.identity.shared.MiniProfile",
                "entityUrn": "urn:li:fs_miniProfile:A",
                "firstName": "Ada",
                "lastName": "Lovelace",
                "publicIdentifier": "ada",
            },
            {"entityUrn": "urn:li:fs_miniGroup:9", "groupName": "Rails"},
        ],
    }

    section = parse_interests(payload)

    assert section["items"] == [
        {
            "name": "Acme",
            "company_id": "1001",
            "universal_name": "acme",
            "followers": 12,
        },
        {"name": "Ada Lovelace", "public_identifier": "ada"},
        {"name": "Rails"},
    ]
    assert (section["total"], section["complete"]) == (35, False)
