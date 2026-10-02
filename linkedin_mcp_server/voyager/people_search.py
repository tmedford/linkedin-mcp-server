"""Search for people through LinkedIn's search API.

Upstream's ``search_people`` loads the results page and returns its text, one
page of ten, leaving the caller to pick people out of prose. This asks the
search service the page is built from and returns each person as a record with
their identifier, so a result can be handed straight to ``get_person_profile``.

**Measured on 2026-10-02, one account.**

- ``search/dash/clusters`` with ``decorationId=...SearchClusterCollection-175``
  and ``query=(keywords:<kw>,flagshipSearchIntent:SEARCH_SRP,queryParameters:
  (resultType:List(PEOPLE),...))`` answers with the results. Plain REST: no
  query id to rotate. ``-165`` and ``-186`` answered the same.
- The filters are query parameters: ``network:List(F,S)``,
  ``currentCompany:List(<numeric id>)``, ``geoUrn:List(<geo id>)``. Each changed
  the result set.
- ``start`` and ``count`` page it. Two pages of ten did not overlap, and both
  were contained in one page of fifty, which is the largest count tried.
- **The order of results is ``data.elements[].items[]``, not ``included``.**
  ``included`` holds the same entities in another order.
- ``paging.total`` read 150 for every query tried, with and without filters. It
  is a ceiling the service reports, not a count of matches, so it is passed
  through as ``total_reported`` and nothing is decided from it.
- A place name is turned into a geo id by ``voyagerSearchDashReusableTypeahead``
  with ``type=GEO``; "New York" answered with five candidates, the first being
  "New York, United States". Upstream's page took the location as text in the
  URL. Here the top candidate is used as a real geo filter and reported back,
  with the others, so a wrong guess is visible rather than silent.

The search page itself is rendered on the server now and issues no data
request, so these were found by asking for them, not by observation.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.voyager.client import VoyagerReader

logger = logging.getLogger(__name__)

_API = "https://www.linkedin.com/voyager/api/"
_DECORATION = "com.linkedin.voyager.dash.deco.search."
_CLUSTERS = (
    f"{_API}search/dash/clusters?decorationId={_DECORATION}SearchClusterCollection-175"
)
_TYPEAHEAD = (
    f"{_API}voyagerSearchDashReusableTypeahead"
    f"?decorationId={_DECORATION}typeahead.ReusableTypeaheadCollection-27"
)
_GEO_TYPES = "MARKET_AREA,COUNTRY_REGION,ADMIN_DIVISION_1,CITY"

#: The largest page measured to work.
MAX_COUNT = 50
DEFAULT_COUNT = 10

_ELEMENTS_PATH = "data.elements[].items[]"
_PROFILE_IN_URN = re.compile(r"urn:li:fsd_profile:([A-Za-z0-9_-]+)")
_SLUG_IN_URL = re.compile(r"/in/([^/?#]+)")
_DISTANCE = {"DISTANCE_1": "1st", "DISTANCE_2": "2nd", "DISTANCE_3": "3rd"}


def _text(node: Any) -> str | None:
    return node.get("text") if isinstance(node, dict) else None


def parse_people(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """People in the order the service ranked them, and whether rows were found."""
    data = payload.get("data") or {}
    found = isinstance(data, dict) and "elements" in data
    results = {
        entity.get("entityUrn"): entity
        for entity in payload.get("included") or []
        if isinstance(entity, dict)
    }
    people = []
    for cluster in data.get("elements") or []:
        for item in (cluster or {}).get("items") or []:
            entity = results.get(
                ((item or {}).get("itemUnion") or {}).get("*entityResult")
            )
            if not entity:
                continue
            urn = _PROFILE_IN_URN.search(entity.get("entityUrn") or "")
            url = entity.get("navigationUrl") or ""
            slug = _SLUG_IN_URL.search(url)
            # A result with neither a profile id nor a profile link is not a
            # person: clusters also carry promos and feedback cards.
            if not urn and not slug:
                continue
            distance = (entity.get("entityCustomTrackingInfo") or {}).get(
                "memberDistance"
            )
            insights = [
                _text((insight.get("simpleInsight") or {}).get("title"))
                for insight in entity.get("insights") or []
                if isinstance(insight, dict)
            ]
            person = {
                "name": _text(entity.get("title")),
                "headline": _text(entity.get("primarySubtitle")),
                "location": _text(entity.get("secondarySubtitle")),
                "public_identifier": slug.group(1) if slug else None,
                "profile_urn": f"urn:li:fsd_profile:{urn.group(1)}" if urn else None,
                "profile_url": f"/in/{slug.group(1)}/" if slug else None,
                # LinkedIn's own enum, and the degree it stands for. Structural,
                # so it does not depend on the "1st" badge's locale.
                "distance": distance,
                "degree": _DISTANCE.get(distance or ""),
                "summary": _text(entity.get("summary")),
                "insight": next((text for text in insights if text), None),
            }
            people.append({k: v for k, v in person.items() if v not in (None, "")})
    return people, found


def render_people(people: list[dict[str, Any]]) -> str:
    lines = []
    for person in people:
        degree = f" • {person['degree']}" if person.get("degree") else ""
        lines.append(f"{person.get('name') or 'LinkedIn Member'}{degree}")
        for key in ("headline", "location", "insight"):
            if person.get(key):
                lines.append(f"    {person[key]}")
    return "\n".join(lines)


class VoyagerPeopleSearch(VoyagerReader):
    """Find people by keyword and filter without rendering the results page."""

    surface = "people-search"

    async def _geo(self, location: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """The geo a place name most likely means, and the other candidates."""
        if location.strip().isdigit():
            chosen = {"geo_id": location.strip(), "name": None}
            return chosen, [chosen]
        payload = await self._fetch(
            f"{_TYPEAHEAD}&keywords={quote(location.strip(), safe='')}&q=type"
            f"&query=(typeaheadFilterQuery:(geoSearchTypes:List({_GEO_TYPES})))"
            "&type=GEO"
        )
        candidates = []
        for element in (payload.get("data") or {}).get("elements") or []:
            urn = (element or {}).get("trackingUrn") or ""
            if urn.startswith("urn:li:geo:"):
                candidates.append(
                    {
                        "geo_id": urn.rsplit(":", 1)[-1],
                        "name": _text(element.get("title")),
                    }
                )
        if not candidates:
            # Refused rather than searched without it: an ignored filter
            # answers with the unfiltered set while reading as filtered.
            raise LinkedInScraperException(
                f"LinkedIn does not recognise {location!r} as a place, so it "
                "cannot be used as a location filter. Try a city, region or "
                "country name, or put the word in keywords."
            )
        return candidates[0], candidates[:5]

    async def find_people(
        self,
        keywords: str,
        location: str | None = None,
        network: list[str] | None = None,
        current_company: str | None = None,
        start: int = 0,
        count: int = DEFAULT_COUNT,
    ) -> dict[str, Any]:
        """Read one page of people matching a search."""
        from linkedin_mcp_server.scraping.search_urls import build_people_search_url

        if not keywords.strip():
            raise LinkedInScraperException(
                "keywords was blank. Pass the words to search for."
            )
        if start < 0 or not 1 <= count <= MAX_COUNT:
            raise LinkedInScraperException(
                f"start must be >= 0 and count between 1 and {MAX_COUNT}, got "
                f"start={start}, count={count}."
            )
        # Upstream's builder is the validator: it refuses a network token or a
        # company name LinkedIn would silently ignore, before any request.
        url = build_people_search_url(keywords, location, network, current_company)

        parameters = {"resultType": ["PEOPLE"]}
        if network:
            parameters["network"] = list(network)
        if current_company:
            parameters["currentCompany"] = [current_company]
        resolved: dict[str, Any] | None = None
        candidates: list[dict[str, Any]] = []
        if location and location.strip():
            resolved, candidates = await self._geo(location)
            parameters["geoUrn"] = [resolved["geo_id"]]

        facets = ",".join(
            f"{key}:List({','.join(quote(value, safe='') for value in values)})"
            for key, values in parameters.items()
        )
        payload = await self._fetch(
            f"{_CLUSTERS}&origin=GLOBAL_SEARCH_HEADER&q=all"
            f"&query=(keywords:{quote(keywords.strip(), safe='')},"
            f"flagshipSearchIntent:SEARCH_SRP,queryParameters:({facets}),"
            f"includeFiltersInResponse:false)&start={start}&count={count}"
        )
        people, found = parse_people(payload)
        self._refuse_unexplained_zero(
            rows=people, payload=payload, path=_ELEMENTS_PATH, container_found=found
        )

        total = ((payload.get("data") or {}).get("paging") or {}).get("total")
        result: dict[str, Any] = {
            "url": url,
            "sections": {"search_results": render_people(people)},
            # The shape upstream's tool returned people in, so a caller that
            # read profile links out of `references` still finds them.
            "references": {
                "search_results": [
                    {
                        "kind": "person",
                        "url": person["profile_url"],
                        "text": person.get("name") or "",
                        "context": "search result",
                    }
                    for person in people
                    if person.get("profile_url")
                ]
            },
            "people": people,
            "count": len(people),
            "start": start,
            "page_size": count,
            # A ceiling the service reports, not a count of matches.
            "total_reported": total if isinstance(total, int) else None,
            # Measured from what came back, never from the reported total.
            "at_end": None if not people else len(people) < count,
        }
        if resolved is not None:
            result["location_resolved"] = resolved
            result["location_candidates"] = candidates
        return result
