"""Search posts, read the home feed and a profile's sidebar from LinkedIn's API.

Upstream's ``search_posts`` and ``get_feed`` load a page and scroll it, and
``get_sidebar_profiles`` loads a profile and follows its "Show all" links.

**Measured on 2026-10-03, one account.**

- **Feed.** ``feed/updatesV2?q=feed`` returns the home feed as ``UpdateV2``
  records, the shape a member's posts come in, so
  :func:`~linkedin_mcp_server.voyager.person.parse_posts` reads them. Promoted
  and in-app promotion entries come back among them with no time and are
  dropped. ``q=chronFeed`` answered too, newest first.
- **Post search.** ``search/dash/clusters`` with ``resultType:List(CONTENT)``
  names each result but leaves it hollow: a ``SearchUpdateWrapper`` with only
  a tracking id. The results page is server-rendered and loads through the
  paging action under ``com.linkedin.sdui.search.contentSearchResults``, whose
  payload takes ``keywords``, ``startIndex``, ``count`` and ``datePosted``.
  Called directly, ten posts came back for ten, a window starting at 10 held
  ten others, and ``datePosted: ["past-week"]`` changed the set. Each post is
  a ``feed-full-update`` component holding ``feed-actor``,
  ``feed-actor-sub-description`` and ``feed-commentary``; its permalink ends
  in the activity id, which dates it.
  A paragraph break is a ``br`` element between the strings. The author's
  headline is the value a ``profile_headline_loading_state`` action sets. The
  reaction, comment and repost counts are not rendered text: they are state
  values keyed ``commentCount-<urn>``, ``repostCount-<urn>`` and one
  ``ReactionType_<KIND>_<urn>`` per kind of reaction, where the urn is the update's own and differs from
  the id in its permalink. A post that shares a job holds a
  ``feed-job-card-entity`` whose link ends in the job id.
- **The page writes; this does not.** Opening the results page posts the query
  to ``updateSearchHistoryRequest``. Nothing here does.
- **Sidebar.** The profile page asks for each sidebar section as a component:
  ``...profile.dsl.impl.browsemapRecommendedEntitySection`` ("More profiles for
  you") and ``...pymkRecommendedEntitySection`` ("People you may know"), with
  the profile's vanity name. The older ``browsemap`` REST endpoints answer 410
  and 404. A section is named here by its component, never by its heading.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any
from urllib.parse import quote

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    LinkedInScraperException,
    RateLimitError,
)
from linkedin_mcp_server.voyager.profile_views import (
    _POST_STREAM_JS,
    VoyagerProfileViews,
    _read_stream,
    _resolve,
    _strings_under,
)

logger = logging.getLogger(__name__)

_API = "https://www.linkedin.com/voyager/api/"
_FEED = f"{_API}feed/updatesV2?q=feed"
_ACTIONS = "https://www.linkedin.com/flagship-web/rsc-action/actions/"
_CONTENT_PAGER = "com.linkedin.sdui.search.contentSearchResults"
_CONTENT_SCREEN = "com.linkedin.sdui.flagshipnav.search.SearchResultsContent"
_PROFILE_SCREEN = "com.linkedin.sdui.flagshipnav.profile.Profile"
_COMPONENT_PREFIX = "com.linkedin.sdui.generated.profile.dsl.impl."

#: Posts per request; the page asks for three at a time.
SEARCH_PAGE_SIZE = 10
FEED_MAX = 50
#: Asking for one or two posts is asking for a window of promotions.
FEED_MIN_WINDOW = 5
FEED_MAX_WINDOWS = 6

#: Sidebar section -> the component that renders it. The keys are the ones
#: upstream's tool returned, which were the headings in snake case.
SIDEBAR_COMPONENTS = {
    "more_profiles_for_you": "browsemapRecommendedEntitySection",
    "people_you_may_know": "pymkRecommendedEntitySection",
}

_POST_LINK = re.compile(
    r"https://www\.linkedin\.com/(?:posts/[^\s\"?]+|feed/update/[^\s\"?]+)"
)
_ACTIVITY_IN_LINK = re.compile(r"(?:activity|ugcPost|share)[-:](\d{18,20})")
_POST_AUTHOR = re.compile(r"https://www\.linkedin\.com/posts/([^_/?#]+)_")
_PROFILE_LINK = re.compile(r"https://www\.linkedin\.com/in/([^/?#\"\s]+)")
_COMPANY_LINK = re.compile(r"https://www\.linkedin\.com/company/([^/?#\"\s]+)")
_JOB_LINK = re.compile(r"https://www\.linkedin\.com/jobs/view/(\d+)")
_COUNT_KEY = re.compile(r'"id":"reactionsCount-(urn:li:[A-Za-z]+:\d+)"')
_COUNT_VALUE = (
    r'"id":"{name}-{urn}"}}}}}},"value":{{"\$case":"intValue","intValue":(\d+)}}'
)
_HEADLINE = re.compile(
    r'"id":"profile_headline_loading_state"}},"namespace":"[^"]*"},'
    r'"value":{"\$case":"stringValue","stringValue":"((?:[^"\\]|\\.)*)"'
)
#: Result key -> the state that holds it. Reactions have no such state:
#: ``reactionsCount`` is an expression summing one state per reaction type
#: (``ReactionType_LIKE_<urn>`` and its siblings), so those are summed here.
#: Measured: 62 + 1 + 2 + 1 where the page showed 66.
_COUNTS = {"comments": "commentCount", "reposts": "repostCount"}
_REACTION_VALUE = (
    r'"id":"ReactionType_[A-Z_]+_{urn}"}}}}}},'
    r'"value":{{"\$case":"intValue","intValue":(\d+)}}'
)


def _search_id() -> str:
    """One id per search, as the page mints one and keeps it across windows."""
    return str(uuid.uuid4())


def _named(node: Any, name: str, out: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every component whose tracking view name is ``name``, outermost only."""
    if isinstance(node, dict):
        specs = node.get("viewTrackingSpecs")
        if isinstance(specs, dict) and specs.get("viewName") == name:
            out.append(node)
        else:
            for value in node.values():
                _named(value, name, out)
    elif isinstance(node, list):
        for value in node:
            _named(value, name, out)
    return out


def _text_under(node: Any, out: list[str], inside: bool = False) -> list[str]:
    """Text a component renders, in order: the strings among ``children``.

    A post's body is a list mixing plain strings with elements (links,
    hashtags, line breaks), so strings are taken wherever they sit under
    ``children``, and an element array contributes only through its props.
    """
    if isinstance(node, str):
        if inside and not node.startswith("$"):
            out.append(node)
    elif isinstance(node, list):
        if len(node) >= 4 and node[0] == "$" and isinstance(node[1], str):
            if node[1] == "br":
                out.append("\n")
            _text_under(node[3], out, False)
        else:
            for value in node:
                _text_under(value, out, inside)
    elif isinstance(node, dict):
        for key, value in node.items():
            _text_under(value, out, key == "children")
    return out


def parse_content_posts(text: str) -> list[dict[str, Any]]:
    """Posts from one answer of the content-search pager, in order."""
    from linkedin_mcp_server.voyager.person import _activity_time

    rows = _read_stream(text)
    updates: list[dict[str, Any]] = []
    for value in rows.values():
        if isinstance(value, (list, dict)):
            _named(value, "feed-full-update", updates)
    posts = []
    for update in updates:
        resolved = _resolve(update, rows)
        actor = _named(resolved, "feed-actor", [])
        when = _named(resolved, "feed-actor-sub-description", [])
        commentary = _named(resolved, "feed-commentary", [])
        actor_texts = (
            [t.strip() for t in _text_under(actor[0], []) if t.strip()] if actor else []
        )
        urls = _strings_under(resolved, "url", [])
        link = next((u for u in urls if _POST_LINK.match(u)), None)
        link = link.split("?", 1)[0] if link else None
        activity = _ACTIVITY_IN_LINK.search(link or "")
        # The permalink opens with its author's own identifier
        # (/posts/<identifier>_<slug>). Links inside the post are not used:
        # they can be anyone it mentions. A degree badge beside the name
        # marks a member; without one the author may be a company page.
        slug = _POST_AUTHOR.match(link or "")
        is_member = any(t.startswith("\u2022") for t in actor_texts[1:])
        body = "".join(_text_under(commentary[0], [])).strip() if commentary else ""
        flat = json.dumps(resolved, separators=(",", ":"), ensure_ascii=False)
        headline = _HEADLINE.search(flat)
        own = _COUNT_KEY.search(flat)
        tally = {}
        if own:
            urn = re.escape(own.group(1))
            each = re.findall(_REACTION_VALUE.format(urn=urn), text)
            if each:
                tally["reactions"] = sum(int(value) for value in each)
            for key, name in _COUNTS.items():
                value = re.search(_COUNT_VALUE.format(name=name, urn=urn), text)
                if value:
                    tally[key] = int(value.group(1))
        card = _named(resolved, "feed-job-card-entity", [])
        job = None
        if card:
            job_id = _JOB_LINK.search(json.dumps(card[0]))
            shown = [t.strip() for t in _text_under(card[0], []) if t.strip()]
            job = {
                key: value
                for key, value in zip(
                    ("title", "company", "location", "insight"), shown, strict=False
                )
            }
            if job_id:
                job["job_id"] = job_id.group(1)
        said = json.dumps(commentary[0]) if commentary else ""
        if not link and not body:
            continue
        posts.append(
            {
                key: value
                for key, value in {
                    "author": actor_texts[0] if actor_texts else None,
                    "author_public_identifier": slug.group(1)
                    if slug and is_member
                    else None,
                    "author_slug": slug.group(1) if slug and not is_member else None,
                    "posted_text": "".join(_text_under(when[0], [])).strip()
                    if when
                    else None,
                    # Exact, from the activity id in the permalink.
                    "posted_at_iso": _activity_time(activity.group(1))
                    if activity
                    else None,
                    "author_headline": json.loads(f'"{headline.group(1)}"')
                    if headline
                    else None,
                    "text": body,
                    "url": link,
                    **tally,
                    "job": job,
                    # Who and what the post links to in its own words.
                    "mentioned_people": list(
                        dict.fromkeys(_PROFILE_LINK.findall(said))
                    ),
                    "mentioned_companies": list(
                        dict.fromkeys(_COMPANY_LINK.findall(said))
                    ),
                }.items()
                if value not in (None, "", [])
            }
        )
    return posts


def _pager_body(
    keywords: str, start: int, count: int, date_posted: str | None, search_id: str
) -> str:
    """The content-search paging request, as the page sends it."""
    payload = {
        "startIndex": start,
        "keywords": keywords,
        "count": count,
        "sortBy": [],
        "postedBy": [],
        "datePosted": [date_posted] if date_posted else [],
        "contentType": [],
        "fromMember": [],
        "mentionsOrganization": [],
        "mentionsMember": [],
        "fromOrganization": [],
        "authorCompany": [],
        "authorIndustry": [],
        "authorJobTitle": [],
        "spellCheckEnabled": True,
        "clusterStartPosition": start,
        "searchId": search_id,
    }
    arguments = {
        "$type": "proto.sdui.actions.requests.RequestedArguments",
        "requestedStateKeys": [],
        "payload": payload,
        "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
    }
    return json.dumps(
        {
            "pagerId": _CONTENT_PAGER,
            "clientArguments": {
                **arguments,
                "states": [],
                "screenId": _CONTENT_SCREEN,
                "knownTemplateIds": [],
            },
            "paginationRequest": {
                "$type": "proto.sdui.actions.requests.PaginationRequest",
                "pagerId": _CONTENT_PAGER,
                "trigger": {
                    "$case": "itemDistanceTrigger",
                    "itemDistanceTrigger": {
                        "$type": "proto.sdui.actions.requests.ItemDistanceTrigger",
                        "preloadDistance": 3,
                        "preloadLength": 1500,
                    },
                },
                "retryCount": 2,
                "requestedArguments": arguments,
            },
        }
    )


def render_content(posts: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        f"{post.get('author') or '?'} - {post.get('posted_at_iso') or post.get('posted_text')}"
        f"\n{post.get('text') or ''}"
        for post in posts
    )


class VoyagerContent(VoyagerProfileViews):
    """Read posts and profile sidebars through LinkedIn's own actions."""

    surface = "content"

    async def _action(self, url: str, body: str) -> str:
        """POST one component or paging action and return its stream."""
        answer = await self._session.page.evaluate(
            _POST_STREAM_JS,
            {"url": url, "headers": await self._page_headers(), "body": body},
        )
        status = answer.get("status") if isinstance(answer, dict) else None
        if status in (401, 403):
            raise AuthenticationError(
                f"Voyager {self.surface} request rejected: HTTP {status}"
            )
        if status == 429:
            raise RateLimitError(f"Voyager {self.surface} rate limited: HTTP {status}")
        if status != 200:
            raise LinkedInScraperException(
                f"Voyager {self.surface} request failed: HTTP {status}"
            )
        return answer.get("text") or ""

    async def search_posts(
        self, keywords: str, date_posted: str | None = None, max_pages: int = 3
    ) -> dict[str, Any]:
        """Up to ``max_pages`` pages of ten posts matching a search."""
        from linkedin_mcp_server.scraping.search_urls import (
            CONTENT_DATE_POSTED_MAP,
            build_content_search_url,
        )

        if not keywords.strip():
            raise LinkedInScraperException(
                "keywords was blank. Pass the words to search for."
            )
        if not 1 <= max_pages <= 10:
            raise LinkedInScraperException(
                f"max_pages must be between 1 and 10, got {max_pages}."
            )
        # Upstream's builder is the validator: it refuses a recency token
        # LinkedIn would ignore, before any request.
        url = build_content_search_url(keywords, date_posted)
        token = CONTENT_DATE_POSTED_MAP[date_posted.strip()] if date_posted else None
        search_id = _search_id()
        posts: list[dict[str, Any]] = []
        seen: set[str] = set()
        complete = False
        for page in range(max_pages):
            if page:
                await self._session.delay(1.5)
            text = await self._action(
                f"{_ACTIONS}pagination?sduiid={_CONTENT_PAGER}",
                _pager_body(
                    keywords.strip(),
                    page * SEARCH_PAGE_SIZE,
                    SEARCH_PAGE_SIZE,
                    token,
                    search_id,
                ),
            )
            window = parse_content_posts(text)
            if not window and '"feed-full-update"' in text:
                raise LinkedInScraperException(
                    f"Voyager {self.surface} changed shape: posts are marked in "
                    "the answer but none parsed. Refusing to report that as no "
                    "results."
                )
            fresh = [p for p in window if (p.get("url") or p.get("text")) not in seen]
            for post in fresh:
                seen.add(post.get("url") or post.get("text") or "")
            posts.extend(fresh)
            if len(window) < SEARCH_PAGE_SIZE or not fresh:
                complete = True
                break
        return {
            "url": url,
            "sections": {"search_results": render_content(posts)},
            "references": {
                "search_results": [
                    {
                        "kind": "feed_post",
                        "url": post["url"].replace("https://www.linkedin.com", ""),
                        "text": post.get("author") or "",
                        "context": "search result",
                    }
                    for post in posts
                    if post.get("url")
                ]
            },
            "posts": posts,
            "count": len(posts),
            "complete": complete,
        }

    async def get_feed(self, num_posts: int = 10) -> dict[str, Any]:
        """The home feed's most recent posts."""
        from linkedin_mcp_server.voyager.person import parse_posts, render_posts

        if not 1 <= num_posts <= FEED_MAX:
            raise LinkedInScraperException(
                f"num_posts must be between 1 and {FEED_MAX}, got {num_posts}."
            )
        # A window comes back short: promotions sit among the posts with no
        # time of their own and are dropped, and the server itself answered 4
        # for 5. So windows are read until the count is met, each starting
        # where the last one ended and carrying its pagination token.
        posts: list[dict[str, Any]] = []
        seen: set[str] = set()
        start, token = 0, None
        for window in range(FEED_MAX_WINDOWS):
            if window:
                await self._session.delay(1.0)
            payload = await self._fetch(
                f"{_FEED}&count={max(num_posts - len(posts), FEED_MIN_WINDOW)}"
                f"&start={start}"
                + (f"&paginationToken={quote(token, safe='')}" if token else "")
            )
            data = payload.get("data") or {}
            listed = len(data.get("*elements") or data.get("elements") or [])
            fresh = [
                post
                for post in parse_posts(payload)
                if post.get("posted_at_iso") and post["activity_urn"] not in seen
            ]
            if not window:
                self._refuse_unexplained_zero(
                    rows=fresh if listed else [],
                    payload=payload,
                    path="data['*elements']",
                    container_found=self._has_rows_key(data),
                )
            seen.update(post["activity_urn"] for post in fresh)
            posts.extend(fresh)
            start += listed
            token = (data.get("metadata") or {}).get("paginationToken")
            if len(posts) >= num_posts or not listed or not fresh:
                break
        posts = posts[:num_posts]
        return {
            "url": "https://www.linkedin.com/feed/",
            "sections": {"feed": render_posts(posts)},
            "references": {
                "feed": [
                    {
                        "kind": "feed_post",
                        "url": post["url"].replace("https://www.linkedin.com", ""),
                        "text": post.get("author") or "",
                    }
                    for post in posts
                    if post.get("url")
                ][:FEED_MAX]
            },
            "posts": posts,
            "count": len(posts),
        }

    async def get_sidebar_profiles(self, linkedin_username: str) -> dict[str, Any]:
        """The people LinkedIn suggests beside a profile, by section."""
        from linkedin_mcp_server.scraping.identifiers import (
            normalize_person_identifier,
            person_profile_url,
        )

        username = normalize_person_identifier(linkedin_username)
        sidebar: dict[str, list[str]] = {}
        for index, (section, component) in enumerate(SIDEBAR_COMPONENTS.items()):
            if index:
                await self._session.delay(1.0)
            component_id = f"{_COMPONENT_PREFIX}{component}"
            text = await self._action(
                f"{_ACTIONS}component?componentId={component_id}&sduiid={component_id}",
                json.dumps(
                    {
                        "componentId": component_id,
                        "clientArguments": {
                            "payload": {
                                "vanityName": username,
                                "isDetailView": False,
                                "hideProfileCards": False,
                            },
                            "states": [],
                            "requestMetadata": {
                                "$type": "proto.sdui.common.RequestMetadata"
                            },
                            "screenId": _PROFILE_SCREEN,
                            "knownTemplateIds": [],
                        },
                    }
                ),
            )
            people = [
                name
                for name in dict.fromkeys(_PROFILE_LINK.findall(text))
                if name != username
            ]
            if people:
                sidebar[section] = [f"/in/{name}/" for name in people]
        return {"url": person_profile_url(username, "/"), "sidebar_profiles": sidebar}
