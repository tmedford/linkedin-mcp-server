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

**This is not the whole list, and cannot be made to be one from here.** The
summary group holds the six most recent viewers; ``start``, ``count`` and a
time frame were each tried as parameters and each was ignored. On this account
33 cards came back against 529 views in the period. The page's own full list
comes from a server-rendered component stream (``rsc-action`` with
``sduiid=WvmpEntityList``), ten at a time with relative times, which answers
when replayed but returns a UI tree rather than data. So this tool reports how
many views LinkedIn counted beside how many viewers it returned, and a caller
that polls it regularly will see each viewer as they arrive at the top.

The recruiter-views page is a different surface and is not read here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from linkedin_mcp_server.voyager.client import VoyagerReader

logger = logging.getLogger(__name__)

_CARDS = "https://www.linkedin.com/voyager/api/identity/wvmpCards"
PAGE_URL = "https://www.linkedin.com/analytics/profile-views/"

_ELEMENTS_PATH = "included[WvmpCard].value.insightCards"
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


class VoyagerProfileViews(VoyagerReader):
    """Read the signed-in member's profile viewers without opening the page."""

    surface = "profile-views"

    async def get_profile_views(self) -> dict[str, Any]:
        """Read the viewers LinkedIn currently surfaces, newest first."""
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
        returned = len(views["viewers"]) + len(views["anonymous_viewers"])
        total = views.get("total_views")
        return {
            "url": PAGE_URL,
            "sections": {"profile_views": render_views(views)},
            **views,
            "count": len(views["viewers"]),
            "returned": returned,
            # LinkedIn counted `total_views`; this many were named or
            # described. The rest are not reachable from this endpoint.
            "complete": (returned >= total) if isinstance(total, int) else None,
        }
