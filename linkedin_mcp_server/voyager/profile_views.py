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

**The whole list is read off the page's own requests, and nothing is sent.**
The analytics page loads its viewer list from a server-rendered component
stream (``rsc-action`` with ``sduiid=WvmpEntityList``), ten rows at a time as
it scrolls. On 2026-10-02 that request was replayed from here with three of
the dozen headers the page sends, and the session was logged out on the next
call. So this module never issues that request. It loads the real page, lets
the page ask, and reads a copy of each answer: the page's ``fetch`` is wrapped
before the document runs so every response it receives is also kept. The rows
are then parsed out of the stream.

What a row carries is a rendered row: profile link, degree badge, headline and
a relative time ("Viewed 3h ago"). The link is the identity. The time is
LinkedIn's rounding, so it is reported as the text it was plus an approximate
instant, and marked approximate; where the JSON endpoint also has that viewer
its exact time wins. Reading the relative time needs English units and is the
one locale-dependent step here.

Run live the same day, the walk read the list to its end: 117 named viewers
and 95 private ones against 529 counted views, the remainder being repeat
views and the recruiters LinkedIn reports only as a number. The session was
still valid afterwards.

The recruiter-views page is a different surface and is not read here.
"""

from __future__ import annotations

import logging
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from linkedin_mcp_server.voyager.client import VoyagerReader

logger = logging.getLogger(__name__)

_CARDS = "https://www.linkedin.com/voyager/api/identity/wvmpCards"
PAGE_URL = "https://www.linkedin.com/analytics/profile-views/"

_ELEMENTS_PATH = "included[WvmpCard].value.insightCards"

#: Installed before the page's own scripts run. It forwards every call
#: untouched and keeps a copy of what came back from the component endpoint.
_TEE_JS = """(() => {
    if (window.__liMcpTee) return;
    window.__liMcpTee = true;
    window.__liMcpRsc = [];
    const original = window.fetch;
    window.fetch = async function (...args) {
        const response = await original.apply(this, args);
        try {
            const target = args[0];
            const url = String((target && target.url) || target || '');
            if (url.includes('/rsc-action/')) {
                response.clone().text().then(text => {
                    window.__liMcpRsc.push({url, text});
                }).catch(() => {});
            }
        } catch (error) {}
        return response;
    };
})()"""

_READ_TEE_JS = """() => (window.__liMcpRsc || []).map(entry => ({
    url: entry.url, text: entry.text,
}))"""

_SCROLL_JS = """() => {
    const root = document.scrollingElement || document.documentElement;
    root.scrollTo(0, root.scrollHeight);
    for (const element of document.querySelectorAll('main, main *')) {
        const style = getComputedStyle(element);
        if ((style.overflowY === 'auto' || style.overflowY === 'scroll')
                && element.scrollHeight > element.clientHeight + 20) {
            element.scrollTop = element.scrollHeight;
        }
    }
    return root.scrollHeight;
}"""

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

#: Scroll rounds before giving up on a list that keeps growing, and how many
#: rounds with nothing new mean the list has ended.
MAX_ROUNDS = 60
IDLE_ROUNDS = 3
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
            row = {
                "description": texts[0],
                "viewed_text": viewed,
                "viewed_at_iso": _approximate(viewed, now),
                "viewed_at_approximate": True,
            }
        else:
            row = {"aggregate": " - ".join(texts[:2])}
        rows.append(_clean(row))
    return rows


class VoyagerProfileViews(VoyagerReader):
    """Read the signed-in member's profile viewers without opening the page."""

    surface = "profile-views"

    async def _page_rows(self, max_rounds: int) -> tuple[list[dict[str, Any]], bool]:
        """Every row the analytics page loads for itself, and whether it ended.

        Sends nothing. The page is opened and scrolled; the answers it fetches
        are copied as they arrive and parsed. The second value is True when
        scrolling stopped producing answers, False when the round limit cut
        the walk short.
        """
        page = self._session.page
        await page.add_init_script(_TEE_JS)
        await self._navigator._navigate_to_page(PAGE_URL)
        await self._session.check_rate_limit()

        # The copies live in the page's own world, where its fetch was
        # wrapped. Patchright evaluates in an isolated world by default, which
        # sees none of them: the first live run read an empty list that way
        # and reported the walk as finished.
        seen = 0
        idle = 0
        ended = False
        for _ in range(max_rounds):
            await self._session.delay(2.0)
            captured = await page.evaluate(_READ_TEE_JS, isolated_context=False)
            count = len(captured) if isinstance(captured, list) else 0
            if count > seen:
                seen, idle = count, 0
            else:
                idle += 1
                if idle >= IDLE_ROUNDS:
                    ended = True
                    break
            await page.evaluate(_SCROLL_JS)
        captured = await page.evaluate(_READ_TEE_JS, isolated_context=False)

        now = datetime.now(timezone.utc)
        rows: list[dict[str, Any]] = []
        keys: set[str] = set()
        for entry in captured if isinstance(captured, list) else []:
            text = entry.get("text") if isinstance(entry, dict) else None
            if not isinstance(text, str) or _ROW_MARKER not in text:
                continue
            for row in parse_stream_rows(text, now):
                # The same row can arrive twice (a re-render); a viewer who
                # came back twice is two rows with different times.
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
        return rows, ended

    async def get_profile_views(self, full: bool = True) -> dict[str, Any]:
        """Read who viewed the profile: the JSON highlights, and the full list.

        With ``full`` the analytics page is also opened and its own list read
        to the end; without it only the JSON endpoint is asked.
        """
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
        if full:
            rows, list_ended = await self._page_rows(MAX_ROUNDS)
            exact = {v.get("public_identifier"): v for v in views["viewers"]}
            merged: dict[str, dict[str, Any]] = {}
            anonymous: list[dict[str, Any]] = []
            aggregates = list(views["aggregates"])
            for row in rows:
                slug = row.get("public_identifier")
                if slug:
                    if slug in merged:
                        continue  # rows are newest first; keep the latest view
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
            for slug, known in exact.items():
                merged.setdefault(slug or known.get("name") or "", known)
            if rows:
                views["viewers"] = sorted(
                    merged.values(),
                    key=lambda v: v.get("viewed_at_iso") or "",
                    reverse=True,
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
            # Whether the page's list was read to its end. None when the list
            # was not read at all; False when the walk was cut short.
            "complete": list_ended,
        }
