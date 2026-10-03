"""Post search, the home feed and profile sidebars through LinkedIn's API."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.scraping.contracts import FilterValidationError
from linkedin_mcp_server.voyager.content import VoyagerContent, parse_content_posts


def _view(name: str, props: dict[str, Any]) -> list[Any]:
    return ["$", "div", None, {"viewTrackingSpecs": {"viewName": name}, **props}]


def _post(
    n: int,
    *,
    member: bool = True,
    mention: str | None = None,
    own_urn: str | None = None,
    headline: str | None = None,
    job: str | None = None,
) -> str:
    """One search result as the pager renders it: a feed-full-update whose
    body is on another row, mixing plain strings with link elements."""
    slug = f"author-{n}"
    activity = 7510730674383835136 + n
    body = [
        f"Post {n} about agents. ",
        [
            "$",
            "a",
            None,
            {"url": f"https://www.linkedin.com/in/{mention}", "children": ["a friend"]},
        ]
        if mention
        else "",
        ["$", "br", None, {}],
        "Second line.",
    ]
    actor = [f"Author {n}"] + (["• 2nd"] if member else [])
    rows = {
        f"{n}a": _view("feed-commentary", {"children": body}),
    }
    update = _view(
        "feed-full-update",
        {
            "children": [
                _view(
                    "feed-actor",
                    {
                        "children": [
                            ["$", "span", None, {"children": [t]}] for t in actor
                        ]
                    },
                ),
                _view("feed-actor-sub-description", {"children": ["3d"]}),
                f"$L{n}a",
                _view(
                    "view-likers",
                    {
                        "key": {
                            "value": {"$case": "id", "id": f"reactionsCount-{own_urn}"}
                        }
                    },
                )
                if own_urn
                else "",
                {
                    "state": {
                        "key": {
                            "key": {
                                "value": {
                                    "$case": "id",
                                    "id": "profile_headline_loading_state",
                                }
                            },
                            "namespace": "LoadingNamespace",
                        },
                        "value": {"$case": "stringValue", "stringValue": headline},
                    }
                }
                if headline
                else "",
                _view(
                    "feed-job-card-entity",
                    {
                        "url": f"https://www.linkedin.com/jobs/view/{job}/",
                        "children": [
                            ["$", "p", None, {"children": [t]}]
                            for t in (
                                "VP of Product",
                                "Claravine",
                                "United States (Remote)",
                                "$$185K/yr - $245K/yr",
                            )
                        ],
                    },
                )
                if job
                else "",
                {
                    "url": f"https://www.linkedin.com/posts/{slug}_title-ugcPost-{activity}-abcd?utm=x"
                },
            ]
        },
    )
    lines = [f"{key}:{json.dumps(value)}" for key, value in rows.items()]
    lines.append(f"{n}0:{json.dumps(update)}")
    return "\n".join(lines)


def test_a_post_is_read_whole_and_dated_from_its_id():
    posts = parse_content_posts(_post(1, mention="someone-else"))

    assert posts == [
        {
            "author": "Author 1",
            # The author, from the permalink; not the person mentioned inside.
            "author_public_identifier": "author-1",
            "posted_text": "3d",
            "posted_at_iso": "2026-09-29T16:02+00:00",
            # The line break between the two is an element, not a string.
            "text": "Post 1 about agents. a friend\nSecond line.",
            "url": "https://www.linkedin.com/posts/author-1_title-ugcPost-7510730674383835137-abcd",
            "mentioned_people": ["someone-else"],
        }
    ]


def test_an_author_without_a_degree_badge_is_not_claimed_as_a_member():
    post = parse_content_posts(_post(2, member=False))[0]

    assert "author_public_identifier" not in post
    assert post["author_slug"] == "author-2"


def _state(key: str, case: str, value: Any) -> str:
    """One state the stream sets, serialised as the server serialises it."""
    return json.dumps(
        {
            "key": {"key": {"value": {"$case": "id", "id": key}}},
            "value": {"$case": case, case: value},
        },
        separators=(",", ":"),
    )


def test_counts_come_from_state_under_the_updates_own_urn():
    # The urn the counts hang off is not the id in the permalink.
    urn = "urn:li:activity:7510448457208287233"
    other = "urn:li:activity:7510448457208287999"
    stream = (
        _post(1, own_urn=urn)
        + "\n"
        + "9z:["
        + ",".join(
            [
                _state(f"ReactionType_LIKE_{urn}", "intValue", 62),
                _state(f"ReactionType_SUPER_LIKE_{urn}", "intValue", 4),
                _state(f"commentCount-{urn}", "intValue", 5),
                _state(f"repostCount-{urn}", "intValue", 9),
                # Another post's counts in the same answer.
                _state(f"ReactionType_LIKE_{other}", "intValue", 700),
                _state(f"commentCount-{other}", "intValue", 800),
            ]
        )
        + "]"
    )

    post = parse_content_posts(stream)[0]

    assert (post["reactions"], post["comments"], post["reposts"]) == (66, 5, 9)


def test_a_post_without_count_state_claims_no_counts():
    post = parse_content_posts(_post(1, own_urn="urn:li:activity:75104484572082872"))[0]

    assert not {"reactions", "comments", "reposts"} & set(post)


def test_the_authors_headline_and_a_shared_job_are_read():
    post = parse_content_posts(
        _post(1, headline='CRO at "Claravine"', job="4471534204")
    )[0]

    assert post["author_headline"] == 'CRO at "Claravine"'
    assert post["job"] == {
        "title": "VP of Product",
        "company": "Claravine",
        "location": "United States (Remote)",
        # A leading dollar sign arrives escaped as "$$".
        "details": ["$185K/yr - $245K/yr"],
        "job_id": "4471534204",
    }


class _Page:
    def __init__(self, *texts: str, fetch: Any = None):
        self.texts = list(texts)
        self.fetch = fetch
        self.sent: list[dict[str, Any]] = []

    async def evaluate(self, _program: str, argument: Any = None) -> Any:
        if isinstance(argument, dict):
            self.sent.append(argument)
            return {"status": 200, "text": self.texts.pop(0) if self.texts else ""}
        self.sent.append({"url": argument})
        return {"body": json.dumps(self.fetch)}


def _reader(page: _Page) -> VoyagerContent:
    session = MagicMock()
    session.page = page
    session.delay = AsyncMock()
    reader = VoyagerContent(session, MagicMock())
    setattr(reader, "_page_headers", AsyncMock(return_value={"x-li-track": "{}"}))
    return reader


async def test_search_pages_ten_at_a_time_until_a_short_window():
    full = "\n".join(_post(n) for n in range(10))
    page = _Page(full, _post(50))

    result = await _reader(page).search_posts("agentic ai", "past_week", max_pages=3)

    payloads = [json.loads(s["body"])["clientArguments"]["payload"] for s in page.sent]
    assert [(p["startIndex"], p["count"]) for p in payloads] == [(0, 10), (10, 10)]
    assert payloads[0]["keywords"] == "agentic ai"
    assert payloads[0]["datePosted"] == ["past-week"]
    # One search, one id: the page keeps it across its windows.
    assert payloads[0]["searchId"] == payloads[1]["searchId"]
    assert result["count"] == 11 and result["complete"] is True
    assert result["references"]["search_results"][0]["url"].startswith(
        "/posts/author-0_"
    )


async def test_an_unknown_recency_is_refused_before_any_request():
    page = _Page()

    with pytest.raises(FilterValidationError):
        await _reader(page).search_posts("x", "yesterday")

    assert page.sent == []


async def test_posts_marked_but_not_parsed_are_refused():
    broken = '0:["$","div",null,{"viewTrackingSpecs":{"viewName":"feed-full-update"}}]'

    with pytest.raises(LinkedInScraperException, match="changed shape"):
        await _reader(_Page(broken)).search_posts("x", max_pages=1)


async def test_the_sidebar_is_two_component_requests_named_by_component():
    page = _Page(
        '0:{"a":"https://www.linkedin.com/in/grace-hopper","b":"https://www.linkedin.com/in/ada-lovelace","c":"https://www.linkedin.com/in/grace-hopper"}',
        "0:{}",
    )

    result = await _reader(page).get_sidebar_profiles("ada-lovelace")

    assert "browsemapRecommendedEntitySection" in page.sent[0]["url"]
    assert "pymkRecommendedEntitySection" in page.sent[1]["url"]
    sent = json.loads(page.sent[0]["body"])["clientArguments"]["payload"]
    assert sent["vanityName"] == "ada-lovelace"
    # The profile itself and repeats are dropped; an empty section is omitted.
    assert result["sidebar_profiles"] == {
        "more_profiles_for_you": ["/in/grace-hopper/"]
    }


async def test_the_feed_is_read_from_the_api_and_drops_promotions():
    update = "urn:li:fs_updateV2:(urn:li:activity:7511799000000000000,X)"
    feed = {
        "data": {"*elements": [update, "urn:li:fs_updateV2:promo"]},
        "included": [
            {
                "$type": "com.linkedin.voyager.feed.render.UpdateV2",
                "entityUrn": update,
                "updateMetadata": {"urn": "urn:li:activity:7511799000000000000"},
                "actor": {"name": {"text": "Ada"}},
                "commentary": {"text": {"text": "Hello"}},
            },
            {
                "$type": "com.linkedin.voyager.feed.render.UpdateV2",
                "entityUrn": "urn:li:fs_updateV2:promo",
                "updateMetadata": {"urn": "urn:li:inAppPromotion:9"},
            },
        ],
    }
    page = _Page(fetch=feed)

    result = await _reader(page).get_feed(num_posts=5)

    assert "feed/updatesV2?q=feed&count=5&start=0" in page.sent[0]["url"]
    assert [p["author"] for p in result["posts"]] == ["Ada"]
    assert result["references"]["feed"][0]["kind"] == "feed_post"
    assert result["references"]["feed"][0]["url"].startswith("/feed/update/")


def _feed_window(*numbers: int, token: str | None = None) -> dict[str, Any]:
    urns = [
        f"urn:li:fs_updateV2:(urn:li:activity:75117990000000000{n:02d},X)"
        for n in numbers
    ]
    return {
        "data": {"*elements": urns, "metadata": {"paginationToken": token}},
        "included": [
            {
                "$type": "com.linkedin.voyager.feed.render.UpdateV2",
                "entityUrn": urn,
                "updateMetadata": {"urn": urn.split("(")[1].split(",")[0]},
                "actor": {"name": {"text": f"Author {n}"}},
            }
            for n, urn in zip(numbers, urns, strict=True)
        ],
    }


async def test_a_short_feed_window_is_followed_until_the_count_is_met():
    page = _Page()
    windows = [_feed_window(1, 2, token="t 1"), _feed_window(2, 3, 4, 5)]

    async def evaluate(_program: str, argument: Any = None) -> Any:
        page.sent.append({"url": argument})
        return {"body": json.dumps(windows.pop(0))}

    setattr(page, "evaluate", evaluate)

    result = await _reader(page).get_feed(num_posts=4)

    # Asked for four, got two: the next window starts after those two and
    # carries the token, and the repeat of post 2 is not counted twice.
    assert "&start=2&paginationToken=t%201" in page.sent[1]["url"]
    assert [p["author"] for p in result["posts"]] == [
        "Author 1",
        "Author 2",
        "Author 3",
        "Author 4",
    ]


async def test_a_feed_window_with_nothing_new_ends_the_read():
    page = _Page(fetch=_feed_window(1, 2))

    result = await _reader(page).get_feed(num_posts=10)

    assert result["count"] == 2
    assert len(page.sent) == 2
