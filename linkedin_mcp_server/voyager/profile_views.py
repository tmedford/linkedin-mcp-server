"""Read who viewed the signed-in member's profile, from the API.

The routine that owns this surface has had to open the member's own browser on
``/analytics/profile-views/`` and read the page text, because no tool reached
it and an earlier capture of that page found no data request to copy.

``identity/wvmpCards`` answers with the viewers as JSON. Each identified viewer
is a card with the viewer's profile, how far they are from the signed-in
member, and **the exact time they viewed**, where the page says "1w ago".

**Measured on 2026-10-02, one account.**

- The answer is one ``WvmpCard`` holding ``insightCards``: groups of viewer
  cards. On this account there were seven: the summary (the most recent
  viewers, with the view count for the period and its change), notable
  viewers, three companies, one job title and one traffic source.
- A viewer card is one of three kinds. An identified viewer carries
  ``viewer.profile`` with a ``MiniProfile`` and ``distance``. A private-mode
  viewer carries only ``viewer.obfuscationString`` ("Recruiter at DualEntry").
  An aggregate carries ``wvmpCardType`` and a headline ("133 people with the
  job title Recruiter") and no viewer.
- The same person appears in more than one group, so viewers are de-duplicated
  by profile and each keeps the names of the groups it was seen in.

**That endpoint is not the whole list.** Its summary group holds the six most
recent viewers; ``start``, ``count`` and a time frame were each tried as
parameters and each was ignored. On this account 33 cards came back against
529 views in the period.

**The whole list comes from the page's own paging endpoint, called directly.**
The analytics page is built from server-rendered components, and it loads more
viewers by POSTing to ``rsc-action/actions/pagination`` with
``sduiid=com.linkedin.sdui.premium.wvmp.entityList``. That request takes a
``start``, a ``count`` and the selected period, and answers with a component
stream whose rows are parsed here. Measured on 2026-10-02:

- Its body is fixed apart from those three values. The page's own request was
  captured and is reproduced as ``_paging_body``; nothing in it is per-account.
- ``count`` is honoured beyond the page's own ten: 40 rows came back for 40.
- ``start`` is honoured: a page starting at row 60 began three weeks back.
- The period is the value of the date-range state, and the page sends it
  explicitly even for its default (``LAST_90_DAYS``).
- The sibling ``server-request`` action that renders the FIRST page ignores
  all three: sent with start, count and a period it returned the same first
  ten rows four times. So it is not used, and the first page is read through
  the paging action like every other.

**The headers are the page's own, taken once per browser session.** The paging
request carries about a dozen ``x-li-*`` headers naming the page instance and
client version, which cannot be invented. The analytics page is loaded once,
the headers of a request it sends are copied, and they are reused for every
call until the browser restarts.

**What was not the cause of a logout.** Earlier the same day the session was
found logged out after this family of request had been replayed with three
headers, and a direct call was blamed. That was never shown: the same hour
also saw every server process killed and restarted and some twenty direct
launches of the profile. Sent with the page's full headers, direct calls were
then made a dozen times across four runs and the session was valid after each.

A row is a rendered row: profile link, degree badge, headline, a relative time
("Viewed 3h ago") and sometimes a mutual-connection count held on another line
of the stream. The link is the identity. The time is LinkedIn's rounding, so
it is reported as the text it was plus an approximate instant, and marked
approximate; where the JSON endpoint also has that viewer its exact time wins.
Reading the relative time needs English units and is the one locale-dependent
step here.

Run live through the tool the same day: 365 days returned 296 named viewers
and 193 private ones in 38 seconds, reaching back eleven months; 7 days
returned 26 and 13 in one second, none older than days; the default returned
117 and 95. The session was valid afterwards. Before this the same lists had
been read by opening the page, wrapping its ``fetch`` and scrolling it, which
gave identical counts for 90 and 365 days and took minutes.

The recruiter-views page is a different surface and is not read here.
"""

from __future__ import annotations

import logging
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    LinkedInScraperException,
    RateLimitError,
)
from linkedin_mcp_server.voyager.client import VoyagerReader

logger = logging.getLogger(__name__)

_CARDS = "https://www.linkedin.com/voyager/api/identity/wvmpCards"
PAGE_URL = "https://www.linkedin.com/analytics/profile-views/"

_ELEMENTS_PATH = "included[WvmpCard].value.insightCards"

_PAGINATION = (
    "https://www.linkedin.com/flagship-web/rsc-action/actions/pagination"
    "?sduiid=com.linkedin.sdui.premium.wvmp.entityList"
)
_PAGER_ID = "com.linkedin.sdui.premium.wvmp.entityList"
_FILTERS = ("DATE_RANGE", "INTERESTING_VIEWER", "ORGANIZATION", "INDUSTRY", "LOCATION")
_FILTER_FIELDS = {
    "DATE_RANGE": "dateRangeSelectionFilters",
    "INTERESTING_VIEWER": "interestingViewerSelectionFilters",
    "ORGANIZATION": "organizationSelectionFilters",
    "INDUSTRY": "industrySelectionFilters",
    "LOCATION": "locationSelectionFilters",
}

#: Headers a page script may not set on fetch; the browser supplies its own.
_BROWSER_OWNED = frozenset(
    {
        "cookie",
        "host",
        "content-length",
        "origin",
        "referer",
        "user-agent",
        "accept-encoding",
        "connection",
        "priority",
    }
)

_POST_STREAM_JS = """async ({url, headers, body}) => {
    const r = await fetch(url, {
        method: 'POST', credentials: 'include', headers, body,
    });
    return {status: r.status, text: await r.text()};
}"""

#: Rows asked for per request. The page asks for ten; forty was measured to
#: be honoured, and fewer requests is the kinder way to read a long list.
PAGE_SIZE = 40
#: Requests per call, so a list that never ends cannot run away.
MAX_PAGES = 40
#: Seconds between requests.
PAGE_DELAY = 1.5

# Headers are valid for as long as the page that issued them, for the reason
# given beside `_QUERY_CACHE` in `messaging.py`.
_HEADER_CACHE: tuple[Any, dict[str, str]] | None = None


def forget_cached_headers() -> None:
    """Drop the copied page headers so the next read takes them again."""
    global _HEADER_CACHE
    _HEADER_CACHE = None


#: The periods LinkedIn's own filter offers, by their length in days. The
#: names are the page's, taken from a request it sent.
TIME_RANGES = {
    7: "WvmpSearchFilterTimeRange_LAST_7_DAYS",
    14: "WvmpSearchFilterTimeRange_LAST_14_DAYS",
    28: "WvmpSearchFilterTimeRange_LAST_28_DAYS",
    90: "WvmpSearchFilterTimeRange_LAST_90_DAYS",
    365: "WvmpSearchFilterTimeRange_LAST_365_DAYS",
}

#: The list's two orders, by the name a caller passes. The relevance value is
#: the one LinkedIn's own page sends for "Sort by most relevant"; measured
#: 2026-10-02 to reorder the list, where RELEVANCE and MOST_RELEVANT did not.
SORTS = {
    "recent": "ProfileViewSortType_TIME_DESCENDING",
    "relevant": "ProfileViewSortType_RELEVANCE_DESCENDING",
}

#: LinkedIn's "interesting viewers" filter, by the name a caller passes.
INTERESTING = {
    "can_help_you_get_a_job": "InterestingViewerType_CAN_HELP_YOU_GET_A_JOB",
    "senior_leader_in_your_industry": (
        "InterestingViewerType_SENIOR_LEADER_IN_YOUR_INDUSTRY"
    ),
    "senior_leader_with_your_job_function": (
        "InterestingViewerType_SENIOR_LEADER_WITH_YOUR_JOB_FUNCTION"
    ),
    "has_verifications": "InterestingViewerType_HAS_VERIFICATIONS",
}

# A private viewer's row links to a people search for others like them, and
# that link is where their employer is named by id.
_SEARCH_LINK = re.compile(
    r'"url":"(https://www\.linkedin\.com/search/results/people/\?[^"]+)"'
)

_ROW_MARKER = '"viewName":"viewer-list-item"'
_INLINE_TEXT = re.compile(
    r'"children":(?:\["((?:[^"\\\\]|\\\\.)*)"\]|"\$L([0-9a-f]+)")'
)
_PROFILE_LINK = re.compile(r"https://www\.linkedin\.com/in/([A-Za-z0-9_%-]+)")
_STREAM_LINE = re.compile(r"^([0-9a-f]+):(.*)$")
# The one locale table in this module: LinkedIn's English relative-time units.
_RELATIVE = re.compile(r"(\d+)\s*(mo|yr|h|d|w|m|s)\b")
# A member's name is rendered as attributed text: [[null, "Name", <badge>...
_NAME_TEXT = re.compile(r'"children":\[\[null,"((?:[^"\\\\]|\\\\.)*)"')
_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3_600,
    "d": 86_400,
    "w": 604_800,
    "mo": 2_592_000,
    "yr": 31_536_000,
}


_DISTANCE = {"DISTANCE_1": "1st", "DISTANCE_2": "2nd", "DISTANCE_3": "3rd"}


def _iso(milliseconds: Any) -> str | None:
    if not isinstance(milliseconds, (int, float)) or milliseconds <= 0:
        return None
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).isoformat(
        timespec="minutes"
    )


def _text(node: Any) -> str | None:
    return node.get("text") if isinstance(node, dict) else None


def _clean(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if value not in (None, "", [])}


def _group_label(
    insight: dict[str, Any], by_urn: dict[str, dict[str, Any]]
) -> tuple[str, str]:
    """What kind of group this is, and the name to call it by."""
    value = insight.get("value") or {}
    urn = str(insight.get("objectUrn") or "")
    if urn.endswith(":summary"):
        return "recent", "Most recent viewers"
    if "notableViewers" in urn:
        return "notable", "Notable viewers"
    if ":company:" in urn:
        company = by_urn.get(value.get("*miniCompany") or "") or {}
        return "company", company.get("name") or urn
    if ":occupation:" in urn:
        return "title", value.get("viewerTitle") or urn
    if ":source:" in urn:
        # The group's referrer is attributed text; a card's is a plain string.
        referrer = value.get("referrer")
        name = _text(referrer) if isinstance(referrer, dict) else referrer
        return "source", name or urn.rsplit(":", 1)[-1]
    return "other", urn


def parse_views(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Viewers and groups from a wvmpCards answer, or None when it has no card."""
    included = [e for e in payload.get("included") or [] if isinstance(e, dict)]
    by_urn = {e.get("entityUrn"): e for e in included if e.get("entityUrn")}
    card = next(
        (e for e in included if str(e.get("$type", "")).endswith(".WvmpCard")), None
    )
    if card is None:
        return None

    identified: dict[str, dict[str, Any]] = {}
    anonymous: list[dict[str, Any]] = []
    aggregates: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    seen_cards: set[str] = set()

    for insight in (card.get("value") or {}).get("insightCards") or []:
        value = insight.get("value") or {}
        kind, label = _group_label(insight, by_urn)
        if kind == "recent":
            summary = {
                "total_views": value.get("numViews"),
                "time_frame": value.get("timeFrame"),
                "change_percent": value.get("numViewsChangeInPercentage"),
            }
        members: list[str] = []
        for card_urn in value.get("*cards") or []:
            body = (by_urn.get(card_urn) or {}).get("value") or {}
            viewer = body.get("viewer") or {}
            profile = viewer.get("profile") or {}
            mini = by_urn.get(profile.get("*miniProfile") or "") or {}
            viewed_at = body.get("viewedAt")
            if mini:
                key = mini.get("dashEntityUrn") or mini.get("entityUrn") or ""
                members.append(mini.get("publicIdentifier") or key)
                reasons = [
                    _text((item.get("value") or {}).get("relevanceReason"))
                    for item in body.get("insights") or []
                    if isinstance(item, dict)
                ]
                distance = (profile.get("distance") or {}).get("value")
                entry = _clean(
                    {
                        "name": " ".join(
                            part
                            for part in (mini.get("firstName"), mini.get("lastName"))
                            if part
                        ),
                        "headline": mini.get("occupation"),
                        "public_identifier": mini.get("publicIdentifier"),
                        "profile_urn": mini.get("dashEntityUrn"),
                        "distance": distance,
                        "degree": _DISTANCE.get(distance or ""),
                        "viewed_at": viewed_at,
                        "viewed_at_iso": _iso(viewed_at),
                        "referrer": body.get("referrer"),
                        # True when an invitation to them is already pending.
                        "pending_invite": body.get("pendingInvitee"),
                        "notable_reason": next((r for r in reasons if r), None),
                    }
                )
                entry["seen_in"] = [label]
                known = identified.get(key)
                if known is None:
                    identified[key] = entry
                else:
                    if label not in known["seen_in"]:
                        known["seen_in"].append(label)
                    # Keep the latest view; a group can hold an older one.
                    if (viewed_at or 0) > (known.get("viewed_at") or 0):
                        entry["seen_in"] = known["seen_in"]
                        identified[key] = entry
                    for field in ("notable_reason", "referrer"):
                        if field not in identified[key] and field in entry:
                            identified[key][field] = entry[field]
                continue
            if card_urn in seen_cards:
                continue
            seen_cards.add(card_urn)
            if viewer.get("obfuscationString"):
                anonymous.append(
                    _clean(
                        {
                            "description": viewer.get("obfuscationString"),
                            "company": (viewer.get("occupation") or {}).get(
                                "entityName"
                            ),
                            "viewed_at": viewed_at,
                            "viewed_at_iso": _iso(viewed_at),
                            "referrer": body.get("referrer"),
                            "seen_in": label,
                        }
                    )
                )
            elif body.get("wvmpCardType") or body.get("headline"):
                aggregates.append(
                    _clean(
                        {
                            "kind": body.get("wvmpCardType"),
                            "description": _text(body.get("headline")),
                            "insight": _text(body.get("insight")),
                        }
                    )
                )
        groups.append(
            _clean(
                {
                    "kind": kind,
                    "label": label,
                    "views": value.get("numViews"),
                    "identified": members,
                }
            )
        )

    def newest_first(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(rows, key=lambda row: -(row.get("viewed_at") or 0))

    return {
        **summary,
        "viewers": newest_first(list(identified.values())),
        "anonymous_viewers": newest_first(anonymous),
        "aggregates": aggregates,
        "groups": groups,
    }


def render_views(views: dict[str, Any]) -> str:
    lines = [
        f"Profile viewers: {views.get('total_views')} ({views.get('time_frame')})",
        "",
    ]
    for viewer in views["viewers"]:
        degree = f" • {viewer['degree']}" if viewer.get("degree") else ""
        lines.append(
            f"{viewer.get('name')}{degree} - viewed {viewer.get('viewed_at_iso')}"
        )
        if viewer.get("headline"):
            lines.append(f"    {viewer['headline']}")
    for viewer in views["anonymous_viewers"]:
        lines.append(
            f"{viewer.get('description')} - viewed {viewer.get('viewed_at_iso')}"
        )
    for aggregate in views["aggregates"]:
        if aggregate.get("description"):
            lines.append(aggregate["description"])
    return "\n".join(lines)


def _approximate(relative: str | None, now: datetime) -> str | None:
    """The instant a relative time points at, to LinkedIn's own rounding."""
    match = _RELATIVE.search(relative or "")
    if not match:
        return None
    seconds = int(match.group(1)) * _UNIT_SECONDS[match.group(2)]
    return (now - timedelta(seconds=seconds)).isoformat(timespec="minutes")


def parse_stream_rows(text: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """Viewer rows from one component-stream answer, in the order rendered.

    The stream is lines of ``id:json``. A row is the span after a
    ``viewer-list-item`` marker; its texts are either inline or a reference to
    another line holding the string, so references are resolved first.
    """
    now = now or datetime.now(timezone.utc)
    lines: dict[str, str] = {}
    for line in text.split("\n"):
        match = _STREAM_LINE.match(line)
        if match:
            lines[match.group(1)] = match.group(2)

    def referenced(line_id: str) -> str | None:
        try:
            value = json.loads(lines.get(line_id, ""))
        except ValueError:
            return None
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
            return value[0]
        return None

    rows = []
    for chunk in text.split(_ROW_MARKER)[1:]:
        # A chunk runs to the next row's marker, and the last one runs to the
        # end of the stream; a row is over where its own line is.
        chunk = chunk.split("\n", 1)[0]
        texts: list[str] = []
        for inline, reference in _INLINE_TEXT.findall(chunk):
            if inline:
                try:
                    texts.append(json.loads(f'"{inline}"'))
                except ValueError:
                    continue
            else:
                resolved = referenced(reference)
                if resolved:
                    texts.append(resolved)
        if not texts:
            continue
        link = _PROFILE_LINK.search(chunk)
        badge = next((t for t in texts if t.startswith("\u2022")), None)
        viewed = next(
            (t for t in texts if t != badge and _RELATIVE.search(t) and len(t) < 24),
            None,
        )
        if link:
            # A named row renders degree badge, headline, time, then extras
            # such as mutual connections. The name is attributed text.
            rest = [t for t in texts if t not in (badge, viewed)]
            digit = re.search(r"\d", badge or "")
            name = _NAME_TEXT.search(chunk)
            row = {
                "public_identifier": link.group(1),
                "name": json.loads(f'"{name.group(1)}"') if name else None,
                "headline": rest[0] if rest else None,
                "degree": {"1": "1st", "2": "2nd", "3": "3rd"}.get(
                    digit.group(0) if digit else ""
                ),
                "viewed_text": viewed,
                "viewed_at_iso": _approximate(viewed, now),
                "viewed_at_approximate": True,
                "extra": rest[1:] or None,
            }
        elif viewed:
            # LinkedIn hides who this was but not where they work: the row
            # links to a search carrying the title and the employer's id, an
            # industry and place when it withholds the employer too, or only
            # a school's name. Measured 2026-10-02 over 28 days: 17, 5 and 2
            # of 24 private rows.
            search = _SEARCH_LINK.search(chunk)
            query = parse_qs(urlparse(search.group(1)).query) if search else {}
            title = (query.get("keywords") or [None])[0]
            description = texts[0]
            company = None
            if title and query.get("currentCompany") and description.startswith(title):
                # "<title> at <company>": the joining word is dropped by
                # position, not by spelling.
                rest = description[len(title) :].strip().split(" ", 1)
                company = rest[1] if len(rest) == 2 else None
            row = {
                "description": description,
                "title": title,
                "company": company,
                "company_id": (query.get("currentCompany") or [None])[0],
                "industry_id": (query.get("industry") or [None])[0],
                "geo_id": (query.get("geoUrn") or [None])[0],
                # "Someone at <school>": named in words, with no id at all.
                "school": (query.get("school") or [None])[0],
                "search_url": search.group(1) if search else None,
                "viewed_text": viewed,
                "viewed_at_iso": _approximate(viewed, now),
                "viewed_at_approximate": True,
            }
        else:
            row = {"aggregate": " - ".join(texts[:2])}
        rows.append(_clean(row))
    return rows


def _paging_body(
    start: int,
    count: int,
    period: str,
    selections: dict[str, list[str]] | None = None,
    sort: str = "ProfileViewSortType_TIME_DESCENDING",
) -> str:
    """The paging request, as the page sends it, for one window of the list.

    ``selections`` maps a filter name from ``_FILTERS`` to its selected
    values; the date range is always the period.
    """
    chosen = {**(selections or {}), "DATE_RANGE": [period]}

    def key(name: str) -> str:
        return f"entityListQueryFilterPrefixWvmpSearchFilterType_{name}"

    state_keys = [{"key": {"value": {"$case": "id", "id": key(n)}}} for n in _FILTERS]
    payload: dict[str, Any] = {
        "sortType": sort,
        "start": start,
        "count": count,
        "filterTypeList": [f"WvmpSearchFilterType_{n}" for n in _FILTERS],
    }
    for name in _FILTERS:
        payload[_FILTER_FIELDS[name]] = {
            "key": key(name),
            "namespace": "MemoryNamespace",
        }
    arguments = {
        "$type": "proto.sdui.actions.requests.RequestedArguments",
        "requestedStateKeys": state_keys,
        "payload": payload,
        "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
    }
    states = [
        {
            "key": key(name),
            "namespace": "MemoryNamespace",
            "value": list(chosen.get(name) or []),
            "originalProtoCase": "stringListValue",
            "protoKey": {
                "$type": "proto.sdui.Key",
                "value": {"$case": "id", "id": key(name)},
            },
        }
        for name in _FILTERS
    ]
    return json.dumps(
        {
            "pagerId": _PAGER_ID,
            "clientArguments": {
                **arguments,
                "states": states,
                "screenId": "com.linkedin.sdui.flagshipnav.premium.wvmp.WVMP",
                "knownTemplateIds": [],
            },
            "paginationRequest": {
                "$type": "proto.sdui.actions.requests.PaginationRequest",
                "pagerId": _PAGER_ID,
                "trigger": {
                    "$case": "itemDistanceTrigger",
                    "itemDistanceTrigger": {
                        "$type": "proto.sdui.actions.requests.ItemDistanceTrigger",
                        "preloadDistance": 3,
                        "preloadLength": 250,
                    },
                },
                "retryCount": 2,
                "requestedArguments": arguments,
            },
        }
    )


def _seconds(relative: str | None) -> int | None:
    match = _RELATIVE.search(relative or "")
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)] if match else None


class VoyagerProfileViews(VoyagerReader):
    """Read the signed-in member's profile viewers without opening the page."""

    surface = "profile-views"

    async def _page_headers(self) -> dict[str, str]:
        """The headers the analytics page sends, copied from one of its requests."""
        global _HEADER_CACHE
        page = self._session.page
        if _HEADER_CACHE is not None and _HEADER_CACHE[0] is page:
            return _HEADER_CACHE[1]

        seen: list[Any] = []

        def _capture(request: Any) -> None:
            if "/rsc-action/" in request.url and request.method == "POST":
                seen.append(request)

        page.on("request", _capture)
        try:
            await self._navigator._navigate_to_page(PAGE_URL)
            await self._session.check_rate_limit()
            for _ in range(20):
                if seen:
                    break
                await self._session.delay(1.0)
        finally:
            page.remove_listener("request", _capture)
        if not seen:
            raise LinkedInScraperException(
                "The profile-views page sent no component request to copy "
                "headers from, so its viewer list cannot be asked for. The "
                "page did not load, or LinkedIn changed how it is built."
            )
        headers = {
            name: value
            for name, value in seen[0].headers.items()
            if name.lower() not in _BROWSER_OWNED
            and not name.lower().startswith(("sec-", ":"))
        }
        _HEADER_CACHE = (page, headers)
        return headers

    async def _list_window(
        self,
        start: int,
        count: int,
        period: str,
        selections: dict[str, list[str]] | None = None,
        sort: str = SORTS["recent"],
    ) -> list[dict[str, Any]]:
        """One window of the viewer list, straight from the paging endpoint."""
        answer = await self._session.page.evaluate(
            _POST_STREAM_JS,
            {
                "url": _PAGINATION,
                "headers": await self._page_headers(),
                "body": _paging_body(start, count, period, selections, sort),
            },
        )
        status = answer.get("status") if isinstance(answer, dict) else None
        if status in (401, 403):
            raise AuthenticationError(
                f"Voyager {self.surface} list request rejected: HTTP {status}"
            )
        if status == 429:
            raise RateLimitError(
                f"Voyager {self.surface} list request rate limited: HTTP {status}"
            )
        if status != 200:
            raise LinkedInScraperException(
                f"Voyager {self.surface} list request failed: HTTP {status}"
            )
        text = answer.get("text") or ""
        rows = parse_stream_rows(text)
        if not rows and "viewer-list-item" in text:
            raise LinkedInScraperException(
                f"Voyager {self.surface} list changed shape: rows are marked "
                "in the answer but none parsed. Refusing to report that as "
                "the end of the list."
            )
        return rows

    async def _all_rows(
        self,
        period: str,
        selections: dict[str, list[str]] | None = None,
        sort: str = SORTS["recent"],
    ) -> tuple[list[dict[str, Any]], bool]:
        """The whole list for a period, and whether its end was reached."""
        rows: list[dict[str, Any]] = []
        keys: set[str] = set()
        for index in range(MAX_PAGES):
            if index:
                await self._session.delay(PAGE_DELAY)
            window = await self._list_window(
                index * PAGE_SIZE, PAGE_SIZE, period, selections, sort
            )
            viewers = [row for row in window if "aggregate" not in row]
            for row in window:
                key = json.dumps(
                    [
                        row.get("public_identifier"),
                        row.get("description"),
                        row.get("aggregate"),
                        row.get("viewed_text"),
                    ]
                )
                if key not in keys:
                    keys.add(key)
                    rows.append(row)
            # Measured against what was asked for: a short window is the end.
            if len(viewers) < PAGE_SIZE:
                return rows, True
        return rows, False

    async def get_profile_views(
        self,
        full: bool = True,
        days: int | None = None,
        interesting: str | None = None,
        company_id: str | None = None,
        industry_id: str | None = None,
        geo_id: str | None = None,
        sort: str = "recent",
    ) -> dict[str, Any]:
        """Read who viewed the profile: the JSON highlights, and the full list.

        With ``full`` the whole list is paged through as well; without it only
        the JSON endpoint is asked. ``days`` chooses the period the list
        covers and needs ``full``.
        """
        if days is not None and days not in TIME_RANGES:
            raise LinkedInScraperException(
                f"days was {days!r}. LinkedIn offers these periods: "
                f"{', '.join(str(d) for d in TIME_RANGES)}. Omit it for "
                "LinkedIn's default."
            )
        if sort not in SORTS:
            raise LinkedInScraperException(
                f"sort was {sort!r}. Pass one of: {', '.join(SORTS)}."
            )
        if sort != "recent" and not full:
            raise LinkedInScraperException(
                "sort needs the full list (full=True). The quick read returns "
                "LinkedIn's highlights as they are."
            )
        selections: dict[str, list[str]] = {}
        if interesting is not None:
            if interesting not in INTERESTING:
                raise LinkedInScraperException(
                    f"interesting was {interesting!r}. Pass one of: "
                    f"{', '.join(INTERESTING)}."
                )
            selections["INTERESTING_VIEWER"] = [INTERESTING[interesting]]
        for name, value, filter_name in (
            ("company_id", company_id, "ORGANIZATION"),
            ("industry_id", industry_id, "INDUSTRY"),
            ("geo_id", geo_id, "LOCATION"),
        ):
            if value is None:
                continue
            # Measured: the bare number filters; a URN in its place answers
            # HTTP 500. Refused here so the mistake names its own correction.
            if not str(value).strip().isdigit():
                raise LinkedInScraperException(
                    f"{name} was {value!r}. Pass LinkedIn's numeric id on its "
                    "own, not a URN or a name."
                )
            selections[filter_name] = [str(value).strip()]
        if selections and not full:
            raise LinkedInScraperException(
                "Filters need the full list (full=True). The quick read "
                "returns LinkedIn's highlights as they are."
            )
        if days is not None and not full:
            raise LinkedInScraperException(
                "days needs the full list (full=True). The quick read has no "
                "period to choose: it returns LinkedIn's highlights as they are."
            )
        payload = await self._fetch(_CARDS)
        views = parse_views(payload)
        self._refuse_unexplained_zero(
            rows=[views] if views else [],
            payload=payload,
            path=_ELEMENTS_PATH,
            container_found=views is not None,
        )
        if views is None:
            views = {
                "viewers": [],
                "anonymous_viewers": [],
                "aggregates": [],
                "groups": [],
            }
        list_ended: bool | None = None
        period_applied: bool | None = None
        if full:
            rows, list_ended = await self._all_rows(
                TIME_RANGES[days or 90], selections, SORTS[sort]
            )
            if days is not None:
                # A row older than the period means the period was ignored.
                # Nothing older proves little for a long period, so this can
                # only ever catch the failure, and is named for that.
                limit = days * 86_400 * 1.5
                period_applied = not any(
                    (_seconds(row.get("viewed_text")) or 0) > limit for row in rows
                )
            # With a filter on, the JSON highlights are not part of the
            # answer: they are unfiltered and would put back people the
            # filter removed. They still lend exact times to rows that match.
            exact = {v.get("public_identifier"): v for v in views["viewers"]}
            keep_highlights = not selections
            merged: dict[str, dict[str, Any]] = {}
            anonymous: list[dict[str, Any]] = []
            aggregates = list(views["aggregates"])
            for row in rows:
                slug = row.get("public_identifier")
                if slug:
                    if slug in merged:
                        # Newest first, or most relevant first: either way the
                        # first row is the one the chosen order puts first.
                        continue
                    known = exact.get(slug)
                    if known:
                        # The JSON endpoint has the exact time and more.
                        merged[slug] = {
                            **row,
                            **known,
                            "viewed_text": row.get("viewed_text"),
                        }
                        merged[slug].pop("viewed_at_approximate", None)
                    else:
                        merged[slug] = row
                elif row.get("description"):
                    anonymous.append(row)
                elif row.get("aggregate") and not any(
                    row["aggregate"] == a.get("description") for a in aggregates
                ):
                    aggregates.append({"description": row["aggregate"]})
            if keep_highlights:
                for slug, known in exact.items():
                    merged.setdefault(slug or known.get("name") or "", known)
            if rows or selections:
                # LinkedIn's relevance has no field to sort by again, so that
                # order is kept as the list gave it; highlights the list
                # lacked follow it.
                views["viewers"] = (
                    list(merged.values())
                    if sort == "relevant"
                    else sorted(
                        merged.values(),
                        key=lambda v: v.get("viewed_at_iso") or "",
                        reverse=True,
                    )
                )
                views["anonymous_viewers"] = anonymous
                views["aggregates"] = aggregates

        returned = len(views["viewers"]) + len(views["anonymous_viewers"])
        return {
            "url": PAGE_URL,
            "sections": {"profile_views": render_views(views)},
            **views,
            "count": len(views["viewers"]),
            "returned": returned,
            # Whether the list was read to its end. None when the list was not
            # read at all; False when the page limit cut the walk short.
            "complete": list_ended,
            "days": days,
            "sort": sort,
            "filters": {
                key: value
                for key, value in (
                    ("interesting", interesting),
                    ("company_id", company_id),
                    ("industry_id", industry_id),
                    ("geo_id", geo_id),
                )
                if value is not None
            },
            # False when a returned row is older than the period allows, which
            # means LinkedIn ignored it. True means nothing contradicted it.
            "period_applied": period_applied,
        }
