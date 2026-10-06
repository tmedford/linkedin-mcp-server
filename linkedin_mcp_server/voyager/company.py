"""Read companies from LinkedIn's API: profile, posts, people and search.

Upstream's company tools each load a company page (about, posts, people) or the
company search page and return its text. These ask the endpoints behind them.

**Measured on 2026-10-03, one account.**

- **Profile.** ``organization/companies`` with ``q=universalName`` and
  ``decorationId=...organization.web.WebFullCompanyMain-12`` answers with the
  whole company: name, numeric id, tagline, description, website, industries,
  staff count and range, headquarters, founding year, type, specialities and
  follower count. Plain REST. The page itself uses a GraphQL query
  (``voyagerOrganizationDashCompanies.<id>``) whose id rotates, so it is not
  used.
- **Posts.** ``organization/updatesV2`` with ``q=companyRelevanceFeed`` returns
  the company's posts as the same ``UpdateV2`` records a member's posts come in,
  so :func:`~linkedin_mcp_server.voyager.person.parse_posts` reads them.
  ``paging.total`` read 501. ``feed/updatesV2?q=companyFeedByUniversalName``
  answered 400.
- **People.** The people tab asks the search service with
  ``flagshipSearchIntent:ORGANIZATIONS_PEOPLE_ALUMNI``, ``currentCompany`` and
  ``includeFiltersInResponse:true``. Over REST (the page uses a rotating query
  id) the same query returned 12 people and the tab's demographics as filter
  values with counts: ``geoUrn`` (where they live), ``schoolFilter``,
  ``currentFunction``, ``skillExplicit``, ``fieldOfStudy`` and ``network``.
  ``metadata.totalResultCount`` read 7469 for a company whose page says 7,868
  employees, so it is reported as LinkedIn's count.
- **Search.** ``search/dash/clusters`` with ``resultType:List(COMPANIES)``, the
  endpoint people search uses, returns each company with its numeric id.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.voyager.client import company_id
from linkedin_mcp_server.voyager.people_search import (
    VoyagerPeopleSearch,
    parse_people,
    render_people,
)

logger = logging.getLogger(__name__)

_API = "https://www.linkedin.com/voyager/api/"
_COMPANY = (
    f"{_API}organization/companies?decorationId="
    "com.linkedin.voyager.deco.organization.web.WebFullCompanyMain-12"
    "&q=universalName&universalName="
)
_POSTS = (
    f"{_API}organization/updatesV2?moduleKey=ORGANIZATION_MEMBER_FEED_DESKTOP"
    "&numComments=0&numLikes=0&q=companyRelevanceFeed"
)
_CLUSTERS = (
    f"{_API}search/dash/clusters?decorationId="
    # -186, not the -175 people search uses: measured on 2026-10-03, a
    # company result carries its insight line ("1 person from your school
    # was hired here") under this version and null under the older one.
    "com.linkedin.voyager.dash.deco.search.SearchClusterCollection-186"
)
_COMPANY_SLUG = re.compile(r"/company/([^/?#]+)")

#: The people tab's demographic charts, by LinkedIn's filter name.
DEMOGRAPHICS = {
    "geoUrn": "locations",
    "schoolFilter": "schools",
    "currentFunction": "functions",
    "skillExplicit": "skills",
    "fieldOfStudy": "fields_of_study",
    "network": "degrees",
}


def _text(node: Any) -> str | None:
    return node.get("text") if isinstance(node, dict) else None


def _clean(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if value not in (None, "", [])}


def parse_company(payload: dict[str, Any]) -> dict[str, Any] | None:
    """One company as a record, or None when the answer names none."""
    data = payload.get("data") or {}
    urns = data.get("*elements") or []
    by_urn = {
        entity.get("entityUrn"): entity
        for entity in payload.get("included") or []
        if isinstance(entity, dict)
    }
    company = by_urn.get(urns[0]) if urns else None
    if not isinstance(company, dict):
        return None
    headquarter = company.get("headquarter") or {}
    following = by_urn.get(company.get("*followingInfo") or "") or {}
    industries = [
        (by_urn.get(urn) or {}).get("localizedName")
        for urn in company.get("*companyIndustries") or []
    ]
    staff_range = company.get("staffCountRange") or {}
    return _clean(
        {
            "name": company.get("name"),
            "universal_name": company.get("universalName"),
            # What search_people, search_jobs and get_profile_views filter on.
            "company_id": company_id(company.get("entityUrn")),
            "tagline": company.get("tagline"),
            "description": company.get("description"),
            "website": company.get("companyPageUrl"),
            "industries": [name for name in industries if name],
            "staff_count": company.get("staffCount"),
            "staff_range": [staff_range.get("start"), staff_range.get("end")]
            if staff_range.get("start") is not None
            else None,
            "headquarters": ", ".join(
                part
                for part in (
                    headquarter.get("city"),
                    headquarter.get("geographicArea"),
                    headquarter.get("country"),
                )
                if part
            ),
            "founded_year": (company.get("foundedOn") or {}).get("year"),
            "company_type": (company.get("companyType") or {}).get("localizedName"),
            "specialities": company.get("specialities"),
            "followers": following.get("followerCount"),
            "url": company.get("url"),
            # Every office the page lists, the headquarters flagged.
            "locations": [
                _clean(
                    {
                        "description": place.get("description"),
                        "line1": place.get("line1"),
                        "city": place.get("city"),
                        "area": place.get("geographicArea"),
                        "postal_code": place.get("postalCode"),
                        "country": place.get("country"),
                        "headquarter": bool(place.get("headquarter")),
                    }
                )
                for place in company.get("confirmedLocations") or []
                if isinstance(place, dict)
            ],
            "showcase_pages": [
                {"name": page.get("name"), "company_id": company_id(urn)}
                for urn in company.get("showcasePages") or []
                for page in [by_urn.get(urn) or {}]
                if page.get("name")
            ],
        }
    )


def render_company(company: dict[str, Any]) -> str:
    lines = [company.get("name") or "Unknown company"]
    for key in ("tagline", "headquarters"):
        if company.get(key):
            lines.append(str(company[key]))
    if company.get("industries"):
        lines.append(", ".join(company["industries"]))
    for label, key in (
        ("Employees", "staff_count"),
        ("Followers", "followers"),
        ("Founded", "founded_year"),
        ("Type", "company_type"),
        ("Website", "website"),
    ):
        if company.get(key):
            lines.append(f"{label}: {company[key]}")
    if company.get("description"):
        lines += ["", company["description"]]
    return "\n".join(lines)


def parse_demographics(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """The people tab's breakdowns: each a list of {name, count, id}.

    They are the search service's filter values with their counts. A filter
    appears more than once in the answer; the first one with counts is kept.
    """
    found: dict[str, list[dict[str, Any]]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            name = DEMOGRAPHICS.get(str(node.get("parameterName")))
            values = node.get("primaryFilterValues") or node.get(
                "secondaryFilterValues"
            )
            if name and name not in found and isinstance(values, list):
                rows = [
                    {
                        "name": value.get("displayName"),
                        "count": value.get("count"),
                        "id": value.get("value"),
                    }
                    for value in values
                    if isinstance(value, dict) and value.get("count") is not None
                ]
                if rows:
                    found[name] = rows
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk((payload.get("data") or {}).get("metadata"))
    return found


def parse_companies(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool, int]:
    """Companies in ranked order, whether the container was found, and how
    many items the page held before non-companies were dropped."""
    data = payload.get("data") or {}
    found = isinstance(data, dict) and "elements" in data
    by_urn = {
        entity.get("entityUrn"): entity
        for entity in payload.get("included") or []
        if isinstance(entity, dict)
    }
    companies = []
    items_seen = 0
    for cluster in data.get("elements") or []:
        for item in (cluster or {}).get("items") or []:
            items_seen += 1
            entity = by_urn.get(
                ((item or {}).get("itemUnion") or {}).get("*entityResult")
            )
            if not entity:
                continue
            identifier = company_id(entity.get("trackingUrn"))
            slug = _COMPANY_SLUG.search(entity.get("navigationUrl") or "")
            # Clusters also carry feedback cards and promos.
            if not identifier or not slug:
                continue
            companies.append(
                _clean(
                    {
                        "name": _text(entity.get("title")),
                        "company_id": identifier,
                        "universal_name": slug.group(1),
                        "detail": _text(entity.get("primarySubtitle")),
                        "followers_text": _text(entity.get("secondarySubtitle")),
                        "summary": _text(entity.get("summary")),
                        # LinkedIn's line about your tie to the company.
                        "insight": next(
                            (
                                text
                                for text in (
                                    _text((row.get("simpleInsight") or {}).get("title"))
                                    for row in entity.get("insights") or []
                                    if isinstance(row, dict)
                                )
                                if text
                            ),
                            None,
                        ),
                        "url": f"/company/{slug.group(1)}/",
                    }
                )
            )
    return companies, found, items_seen


class VoyagerCompany(VoyagerPeopleSearch):
    """Read a company, its posts and its people without loading its pages."""

    surface = "company"

    async def _company(self, company_name: str) -> dict[str, Any]:
        from linkedin_mcp_server.scraping.identifiers import (
            normalize_company_identifier,
        )

        slug = normalize_company_identifier(company_name)
        payload = await self._fetch(f"{_COMPANY}{quote(slug, safe='')}")
        company = parse_company(payload)
        if company is None:
            raise LinkedInScraperException(
                f"Voyager {self.surface} found no company named {slug!r}. Pass "
                "the name from its LinkedIn URL (/company/<name>/); "
                "search_companies finds it."
            )
        return company

    async def get_company(self, company_name: str) -> dict[str, Any]:
        """One company, whole."""
        company = await self._company(company_name)
        return {
            "url": f"https://www.linkedin.com/company/{company['universal_name']}/",
            "sections": {"about": render_company(company)},
            "company": company,
            "company_id": company.get("company_id"),
        }

    async def get_company_posts(
        self, company_name: str, count: int = 10, start: int = 0
    ) -> dict[str, Any]:
        """One page of a company's posts."""
        from linkedin_mcp_server.scraping.identifiers import (
            normalize_company_identifier,
        )
        from linkedin_mcp_server.voyager.person import parse_posts, render_posts

        if start < 0 or not 1 <= count <= 50:
            raise LinkedInScraperException(
                f"start must be >= 0 and count between 1 and 50, got "
                f"start={start}, count={count}."
            )
        slug = normalize_company_identifier(company_name)
        payload = await self._fetch(
            f"{_POSTS}&companyIdOrUniversalName={quote(slug, safe='')}"
            f"&count={count}&start={start}"
        )
        data = payload.get("data") or {}
        found = self._has_rows_key(data)
        listed = len(data.get("*elements") or data.get("elements") or [])
        # Promotions come back among the posts with no author and no time.
        posts = [post for post in parse_posts(payload) if post.get("posted_at_iso")]
        self._refuse_unexplained_zero(
            rows=posts if listed else [],
            payload=payload,
            path="data['*elements']",
            container_found=found,
        )
        return {
            "url": f"https://www.linkedin.com/company/{slug}/posts/",
            "sections": {"posts": render_posts(posts)},
            "posts": posts,
            "count": len(posts),
            "start": start,
            "page_size": count,
            "total": (data.get("paging") or {}).get("total"),
            "at_end": None if not listed else listed < count,
        }

    async def get_company_people(
        self,
        company_name: str,
        keywords: str | None = None,
        start: int = 0,
        count: int = 12,
        schools: list[str] | None = None,
    ) -> dict[str, Any]:
        """People at a company, and the people tab's demographics.

        ``schools`` narrows to people who studied at any of the given schools,
        by LinkedIn's school id: the people tab's "Where they studied" filter,
        which the page writes as ``facetSchool=<id>,<id>`` in its address.
        """
        if start < 0 or not 1 <= count <= 50:
            raise LinkedInScraperException(
                f"start must be >= 0 and count between 1 and 50, got "
                f"start={start}, count={count}."
            )
        school_ids = [str(school).strip() for school in schools or []]
        if any(not school.isdigit() for school in school_ids):
            # A name here would be sent as a filter value LinkedIn ignores,
            # and the answer would be the whole company read as its alumni.
            raise LinkedInScraperException(
                f"schools takes LinkedIn school ids (digits), got {schools!r}. "
                "The id is in demographics.schools of this tool's answer, in a "
                "profile's education, and in the people tab's facetSchool."
            )
        company = await self._company(company_name)
        identifier = company.get("company_id")
        if not identifier:
            raise LinkedInScraperException(
                f"Voyager {self.surface} answered for {company_name!r} with no "
                "company id, which the people search is keyed on."
            )
        studied = f"schoolFilter:List({','.join(school_ids)})," if school_ids else ""
        words = (
            f"keywords:{quote(keywords.strip(), safe='')},"
            if keywords and keywords.strip()
            else ""
        )
        payload = await self._fetch(
            f"{_CLUSTERS}&origin=FACETED_SEARCH&q=all&query=({words}"
            "flagshipSearchIntent:ORGANIZATIONS_PEOPLE_ALUMNI,queryParameters:"
            f"(currentCompany:List({identifier}),{studied}"
            "resultType:List(ORGANIZATION_ALUMNI)),"
            f"includeFiltersInResponse:true)&start={start}&count={count}"
        )
        people, found, items_seen = parse_people(payload)
        self._refuse_unexplained_zero(
            rows=people,
            payload=payload,
            path="data.elements[].items[]",
            container_found=found,
        )
        demographics = parse_demographics(payload)
        slug = company["universal_name"]
        metadata = (payload.get("data") or {}).get("metadata") or {}
        return {
            "url": f"https://www.linkedin.com/company/{slug}/people/",
            "sections": {"employees": render_people(people)},
            "references": {
                "employees": [
                    {
                        "kind": "person",
                        "url": person["profile_url"],
                        "text": person.get("name") or "",
                        "context": "employee",
                    }
                    for person in people
                    if person.get("profile_url")
                ]
            },
            "company_id": identifier,
            "people": people,
            "count": len(people),
            "start": start,
            "page_size": count,
            "total": metadata.get("totalResultCount"),
            "at_end": None if not items_seen else items_seen < count,
            "demographics": demographics,
        }

    async def find_companies(
        self, keywords: str, start: int = 0, count: int = 10
    ) -> dict[str, Any]:
        """One page of companies matching a search."""
        from linkedin_mcp_server.scraping.search_urls import (
            build_company_search_url,
        )

        if not keywords.strip():
            raise LinkedInScraperException(
                "keywords was blank. Pass the words to search for."
            )
        if start < 0 or not 1 <= count <= 50:
            raise LinkedInScraperException(
                f"start must be >= 0 and count between 1 and 50, got "
                f"start={start}, count={count}."
            )
        payload = await self._fetch(
            f"{_CLUSTERS}&origin=GLOBAL_SEARCH_HEADER&q=all"
            f"&query=(keywords:{quote(keywords.strip(), safe='')},"
            "flagshipSearchIntent:SEARCH_SRP,queryParameters:"
            "(resultType:List(COMPANIES)),includeFiltersInResponse:false)"
            f"&start={start}&count={count}"
        )
        companies, found, items_seen = parse_companies(payload)
        self._refuse_unexplained_zero(
            rows=companies,
            payload=payload,
            path="data.elements[].items[]",
            container_found=found,
        )
        return {
            "url": build_company_search_url(keywords),
            "sections": {
                "search_results": "\n".join(
                    f"{c.get('name')} ({c.get('company_id')})"
                    + (f"\n    {c['detail']}" if c.get("detail") else "")
                    + (
                        f"\n    {c['followers_text']}"
                        if c.get("followers_text")
                        else ""
                    )
                    + (f"\n    {c['insight']}" if c.get("insight") else "")
                    for c in companies
                )
            },
            "references": {
                "search_results": [
                    {
                        "kind": "company",
                        "url": c["url"],
                        "text": c.get("name") or "",
                        "context": "search result",
                    }
                    for c in companies
                ]
            },
            "companies": companies,
            "count": len(companies),
            "start": start,
            "page_size": count,
            # What the page reports as "About N results".
            "total": (
                ((payload.get("data") or {}).get("metadata") or {}).get(
                    "totalResultCount"
                )
            ),
            "at_end": None if not items_seen else items_seen < count,
        }
