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
        "?timeRange=WvmpSearchFilterTimeRange_LAST_90_DAYS"
    ),
    "invitations-received": "https://www.linkedin.com/mynetwork/invitation-manager/",
    "invitations-sent": "https://www.linkedin.com/mynetwork/invitation-manager/sent/",
}

_VOYAGER = "/voyager/api"
_SETTLE_MS = 6000


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
            if _VOYAGER in request.url:
                entry = summarise(request.url)
                entry["method"] = request.method
                entry["_url"] = request.url
                requests.append(entry)

        def _on_response(response: Any) -> None:
            if _VOYAGER in response.url:
                statuses[response.url] = response.status

        page.on("request", _on_request)
        page.on("response", _on_response)
        try:
            await page.goto(SURFACES[surface], wait_until="domcontentloaded")
            # The interesting calls are XHRs fired after first paint, so settle
            # rather than returning the moment the document is ready.
            await page.wait_for_timeout(_SETTLE_MS)
        finally:
            page.remove_listener("request", _on_request)
            page.remove_listener("response", _on_response)

        for entry in requests:
            entry["status"] = statuses.get(entry.pop("_url"))

        rest = [r for r in requests if r["kind"] == "rest"]
        graphql = [r for r in requests if r["kind"] == "graphql"]
        return {
            "surface": surface,
            "url": SURFACES[surface],
            "total": len(requests),
            "rest_count": len(rest),
            "graphql_count": len(graphql),
            # The headline answer: can this surface leave the browser behind?
            "browser_free_possible": bool(rest) ,
            "rest": rest,
            "graphql": graphql,
        }
