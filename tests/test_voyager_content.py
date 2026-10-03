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


def _post(n: int, *, member: bool = True, mention: str | None = None) -> str:
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
            "text": "Post 1 about agents. a friendSecond line.",
            "url": "https://www.linkedin.com/posts/author-1_title-ugcPost-7510730674383835137-abcd",
        }
    ]


def test_an_author_without_a_degree_badge_is_not_claimed_as_a_member():
    post = parse_content_posts(_post(2, member=False))[0]

    assert "author_public_identifier" not in post
    assert post["author_slug"] == "author-2"


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

    assert "feed/updatesV2?q=feed&count=5" in page.sent[0]["url"]
    assert [p["author"] for p in result["posts"]] == ["Ada"]
    assert result["references"]["feed"][0]["kind"] == "feed_post"
    assert result["references"]["feed"][0]["url"].startswith("/feed/update/")
