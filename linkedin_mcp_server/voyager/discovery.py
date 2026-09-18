"""Observe which API transactions a LinkedIn surface issues for itself.

This is an INVESTIGATION instrument, not a data reader. It navigates to a
surface, records every Voyager request the page makes on its own behalf, and
reports their shape. Nothing is clicked and no response is parsed.

The point is to find out, per surface, which of two worlds we are in:

- **REST** (``/voyager/api/...``) - a plain URL with query parameters. It can
  be called with nothing but a cookie jar, so a tool built on it needs no
  browser at all once the endpoint is known.
- **GraphQL** (``/voyager/api/graphql?queryId=...``) - a PERSISTED query whose
  id is a hash LinkedIn rotates. It cannot be synthesised or pinned, so it has
  to be observed at runtime, which keeps a browser in the loop.

That distinction decides how far a surface can be lifted away from the browser,
so it is the first thing worth measuring and the cheapest.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

# Deliberately an allowlist. An unknown surface raises instead of navigating
# somewhere arbitrary and reporting an empty capture, because an empty capture
# and a wrong page look identical in the result.
SURFACES: Dict[str, str] = {
    "profile-views": "https://www.linkedin.com/analytics/profile-views/",
    "recruiter-views": (
        "https://www.linkedin.com/analytics/recruiter-views/"
        "?timeRange=WvmpSearchFilterTimeRange_LAST_365_DAYS"
    ),
    "invitations-received": "https://www.linkedin.com/mynetwork/invitation-manager/",
    "invitations-sent": "https://www.linkedin.com/mynetwork/invitation-manager/sent/",
}

_VOYAGER = "/voyager/api"
# Measured: at 6000ms the profile-views and invitations-sent captures came back
# BYTE-IDENTICAL - 13 shared queries, 10 shared REST paths, and not one call
# belonging to either surface. That is the app shell booting; each surface's own
# data query fires later. Two different pages producing the same traffic is the
# signal that the window closed too early, and it is a better check than any
# single capture looking "full".
_SETTLE_MS = 20000


def classify(url: str) -> str:
    """REST, GraphQL or neither - the only distinction this module exists for."""
    path = urlparse(url).path
    if not path.startswith(_VOYAGER):
        return "other"
    if path.endswith("/graphql"):
        return "graphql"
    return "rest"


def summarise(url: str) -> Dict[str, Any]:
    """Describe a request without keeping its values.

    Query VALUES are dropped on purpose. They carry profile ids, cursors and
    session-scoped tokens, and none of that is needed to answer "what shape is
    this endpoint". Keys and the queryId are enough, and they are safe to log.
    """
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    kind = classify(url)
    out: Dict[str, Any] = {
        "kind": kind,
        "path": parsed.path,
        "param_keys": sorted(params),
    }
    if kind == "graphql":
        # The one value worth keeping: it names the persisted query, and its
        # presence is what makes the surface browser-bound.
        query_id = params.get("queryId", [""])[0]
        out["query_id"] = query_id
        out["rotating_hash"] = "." in query_id
    return out


class VoyagerDiscovery:
    """Navigate a surface and report the API transactions it issues."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def observe(self, surface: str) -> Dict[str, Any]:
        if surface not in SURFACES:
            raise ValueError(
                f"Unknown surface {surface!r}. Known: {sorted(SURFACES)}. "
                "Refusing to navigate rather than return an empty capture, "
                "which would be indistinguishable from a surface that issues "
                "no API calls."
            )

        page = self._session.page
        requests: List[Dict[str, Any]] = []
        statuses: Dict[str, int] = {}

        def _on_request(request: Any) -> None:
            # Capture EVERY xhr/fetch, not just /voyager/api. Filtering to that
            # prefix first presupposed the answer: the profile-views page
            # renders real viewer rows while issuing no surface-specific
            # /voyager/api call at all, so the prefix filter hid the very
            # request this module exists to find.
            if request.resource_type not in ("xhr", "fetch"):
                return
            entry = summarise(request.url)
            entry["method"] = request.method
            entry["_url"] = request.url
            requests.append(entry)

        def _on_response(response: Any) -> None:
            statuses[response.url] = response.status

        page.on("request", _on_request)
        page.on("response", _on_response)
        try:
            await page.goto(SURFACES[surface], wait_until="domcontentloaded")
            # The interesting calls are XHRs fired after first paint, so settle
            # rather than returning the moment the document is ready.
            try:
                await page.wait_for_load_state("networkidle", timeout=_SETTLE_MS)
            except Exception:  # noqa: BLE001 - a busy page never goes idle, and
                # that is not a failure: the fixed settle below still applies.
                pass
            await page.wait_for_timeout(_SETTLE_MS)
            landed_url = page.url
            landed_title = await page.title()
        finally:
            page.remove_listener("request", _on_request)
            page.remove_listener("response", _on_response)

        for entry in requests:
            entry["status"] = statuses.get(entry.pop("_url"))

        rest = [r for r in requests if r["kind"] == "rest"]
        graphql = [r for r in requests if r["kind"] == "graphql"]
        other = [r for r in requests if r["kind"] == "other"]

        # Whether we LANDED where we aimed. Without this a redirect to the feed
        # or an interstitial produces a full, healthy-looking capture of the
        # WRONG page's traffic, and every count below describes something we
        # did not ask about.
        requested = SURFACES[surface]
        landed_on_target = landed_url.rstrip("/").startswith(
            requested.split("?")[0].rstrip("/")
        )

        # Every LinkedIn page fires generic chrome - nav, badging, settings,
        # /voyager/api/me. Their presence says nothing about whether THIS
        # surface's data is REST, so the question is deliberately left open
        # here rather than answered by a count that cannot answer it.
        return {
            "surface": surface,
            "requested_url": requested,
            "landed_url": landed_url,
            "landed_title": landed_title,
            "landed_on_target": landed_on_target,
            "total": len(requests),
            "rest_count": len(rest),
            "graphql_count": len(graphql),
            "other_count": len(other),
            "rest": rest,
            "graphql": graphql,
            "other": other,
        }
