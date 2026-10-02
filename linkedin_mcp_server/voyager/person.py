"""Read a person's whole profile in one request, and what it shares with yours.

Upstream's ``get_person_profile`` loads the profile page and one more page per
section asked for, and returns each as rendered text. Finding what two people
have in common from that means reading prose and guessing which "Zuora" is
which.

LinkedIn's REST profile finder takes a ``decorationId`` naming how much to
return. ``FullProfileWithEntities`` returns the profile with its sections as
entities: every position with its company URN and dates, every school with its
school URN, and so on. Entities with ids and dates can be compared exactly,
which is the point of this tool: ``common_ground`` is computed, not inferred.

**Measured on 2026-10-02, one account, two profiles.**

- ``identity/dash/profiles?q=memberIdentity&memberIdentity=<id>`` with
  ``decorationId=...profile.FullProfileWithEntities-93`` answered 200 with the
  profile at ``data['*elements'][0]`` and every section as a
  ``CollectionResponse`` the profile points at (``*profilePositionGroups``,
  ``*profileEducations``, ``*profileSkills`` ...). It is plain REST: no query
  id to rotate. The ``-93`` is a version; ``-91`` answered the same. When a
  version is retired the request fails loudly rather than returning less.
- Each collection carries ``paging.total``. Every section came back whole
  except skills, which is capped at 20 (20 of 34, and 20 of 22). So each
  section here reports ``total`` beside what was returned, and ``complete``
  says whether they match. A capped section is said to be capped.
- Positions are nested: a position GROUP per employer, each pointing at its
  own collection of positions. Reading groups as positions loses every title
  but the latest.
- ``...profile.WebTopCardCore-19`` carries a ``MemberRelationship`` whose union
  key says how the signed-in member relates to this one: ``self``,
  ``*connection`` for a first-degree connection.

**Not measured:** whether this read registers as a profile view. The profile
page's own tracking is a separate request this does not make, but that has not
been confirmed from the other side. Mutual connections are not in this payload
and are not reported.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    LinkedInScraperException,
    RateLimitError,
)
from linkedin_mcp_server.voyager.client import VoyagerReader

logger = logging.getLogger(__name__)

_PROFILES = "https://www.linkedin.com/voyager/api/identity/dash/profiles"
_DECORATION = "com.linkedin.voyager.dash.deco.identity.profile."
FULL_PROFILE = "FullProfileWithEntities-93"
TOP_CARD = "WebTopCardCore-19"

_ELEMENTS_PATH = "data['*elements']"
_PROFILE_URN_PREFIX = "urn:li:fsd_profile:"

# The signed-in member's own profile, kept per page for the reason given beside
# `_QUERY_CACHE` in `messaging.py`: a reader is built per call, and comparing
# ten people to one profile should read that one profile once.
_MY_PROFILE_CACHE: tuple[Any, dict[str, Any]] | None = None


def forget_my_profile() -> None:
    """Drop the cached own profile so the next comparison reads it again."""
    global _MY_PROFILE_CACHE
    _MY_PROFILE_CACHE = None


def _date(value: Any) -> str | None:
    """A LinkedIn date as YYYY, YYYY-MM or YYYY-MM-DD, as precise as it is."""
    if not isinstance(value, dict) or not value.get("year"):
        return None
    text = f"{value['year']:04d}"
    if value.get("month"):
        text += f"-{value['month']:02d}"
        if value.get("day"):
            text += f"-{value['day']:02d}"
    return text


def _range(entity: dict[str, Any]) -> dict[str, str | None]:
    span = entity.get("dateRange") or {}
    return {"start": _date(span.get("start")), "end": _date(span.get("end"))}


def _months(value: str | None, *, end: bool) -> int | None:
    """Months since year 0, taking a bare year as its first or last month."""
    if not value:
        return None
    parts = value.split("-")
    month = int(parts[1]) if len(parts) > 1 else (12 if end else 1)
    return int(parts[0]) * 12 + (month - 1)


def overlap(mine: dict[str, Any], theirs: dict[str, Any]) -> dict[str, Any] | None:
    """The span two dated entries have in common, or None.

    An entry with no end is ongoing. An entry with no start cannot be placed,
    so it overlaps nothing: claiming two people were somewhere together on a
    missing date would invent the relationship this exists to find.
    """
    starts = [
        _months(mine.get("start"), end=False),
        _months(theirs.get("start"), end=False),
    ]
    if None in starts:
        return None
    ends = [_months(mine.get("end"), end=True), _months(theirs.get("end"), end=True)]
    start = max(s for s in starts if s is not None)
    finite = [e for e in ends if e is not None]
    end = min(finite) if finite else None
    if end is not None and end < start:
        return None

    def text(months: int) -> str:
        return f"{months // 12:04d}-{months % 12 + 1:02d}"

    return {
        "start": text(start),
        "end": text(end) if end is not None else None,
        "months": (end - start + 1) if end is not None else None,
    }


class _Payload:
    """One normalized answer: entities by URN, and the collections they point at."""

    def __init__(self, payload: dict[str, Any]):
        self.by_urn = {
            entity["entityUrn"]: entity
            for entity in payload.get("included") or []
            if isinstance(entity, dict) and entity.get("entityUrn")
        }

    def get(self, urn: Any) -> dict[str, Any]:
        return self.by_urn.get(urn) or {} if isinstance(urn, str) else {}

    def collection(
        self, owner: dict[str, Any], key: str
    ) -> tuple[list[dict], int | None]:
        """The entities a ``*key`` pointer leads to, and the server's total."""
        container = self.get(owner.get(f"*{key}"))
        urns = container.get("*elements") or []
        total = (container.get("paging") or {}).get("total")
        rows = [self.by_urn[urn] for urn in urns if urn in self.by_urn]
        return rows, total if isinstance(total, int) else None


def _section(rows: list[dict[str, Any]], total: int | None) -> dict[str, Any]:
    return {
        "items": rows,
        "returned": len(rows),
        "total": total,
        # None when the server gave no total: not knowing is not "complete".
        "complete": (len(rows) >= total) if total is not None else None,
    }


def _clean(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if value not in (None, "", [])}


def parse_profile(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn a FullProfileWithEntities answer into plain sections."""
    data = payload.get("data") or {}
    urns = data.get("*elements") or []
    box = _Payload(payload)
    profile = box.get(urns[0]) if urns else {}

    def organisation(entity: dict[str, Any], pointer: str) -> dict[str, Any]:
        linked = box.get(entity.get(pointer))
        return {"urn": entity.get(pointer), "url": linked.get("url")}

    positions: list[dict[str, Any]] = []
    groups, group_total = box.collection(profile, "profilePositionGroups")
    for group in groups:
        rows, _ = box.collection(group, "profilePositionInPositionGroup")
        for row in rows:
            company = organisation(row, "*company")
            positions.append(
                _clean(
                    {
                        "title": row.get("title"),
                        "company": row.get("companyName"),
                        "company_urn": company["urn"],
                        "company_url": company["url"],
                        **_range(row),
                        "location": row.get("locationName"),
                        "description": row.get("description"),
                    }
                )
            )

    def rows(key: str, shape: Any) -> dict[str, Any]:
        found, total = box.collection(profile, key)
        return _section([_clean(shape(row)) for row in found], total)

    def education(row: dict[str, Any]) -> dict[str, Any]:
        school = organisation(row, "*school")
        return {
            "school": row.get("schoolName"),
            "school_urn": school["urn"],
            "school_url": school["url"],
            "degree": row.get("degreeName"),
            "field": row.get("fieldOfStudy"),
            **_range(row),
            "activities": row.get("activities"),
            "description": row.get("description"),
        }

    geo = box.get((profile.get("geoLocation") or {}).get("*geo"))
    urn = profile.get("entityUrn") or ""
    return {
        "identity": _clean(
            {
                "name": " ".join(
                    part
                    for part in (profile.get("firstName"), profile.get("lastName"))
                    if part
                ),
                "headline": profile.get("headline"),
                "summary": profile.get("summary"),
                "public_identifier": profile.get("publicIdentifier"),
                "profile_urn": urn,
                "location": geo.get("defaultLocalizedName"),
                "industry": box.get(profile.get("*industry")).get("name"),
                "premium": profile.get("premium"),
                "creator": profile.get("creator"),
            }
        ),
        # Employers, not titles, are what LinkedIn pages and totals: `total` is
        # the number of position groups, so `complete` is about employers.
        "positions": {
            **_section(positions, None),
            "employers": len(groups),
            "total_employers": group_total,
            "complete": (len(groups) >= group_total)
            if group_total is not None
            else None,
        },
        "education": rows("profileEducations", education),
        "skills": rows("profileSkills", lambda r: {"name": r.get("name")}),
        "certifications": rows(
            "profileCertifications",
            lambda r: {
                "name": r.get("name"),
                "authority": r.get("authority"),
                "company_urn": r.get("*company"),
                **_range(r),
                "url": r.get("url"),
            },
        ),
        "honors": rows(
            "profileHonors",
            lambda r: {
                "title": r.get("title"),
                "issuer": r.get("issuer"),
                "issued": _date(r.get("issuedOn")),
                "description": r.get("description"),
            },
        ),
        "languages": rows(
            "profileLanguages",
            lambda r: {"name": r.get("name"), "proficiency": r.get("proficiency")},
        ),
        "organizations": rows(
            "profileOrganizations",
            lambda r: {
                "name": r.get("name"),
                "position": r.get("positionHeld"),
                **_range(r),
                "description": r.get("description"),
            },
        ),
        "volunteering": rows(
            "profileVolunteerExperiences",
            lambda r: {
                "role": r.get("role"),
                "organization": r.get("companyName"),
                "company_urn": r.get("*company"),
                "cause": r.get("cause"),
                **_range(r),
                "description": r.get("description"),
            },
        ),
        "projects": rows(
            "profileProjects",
            lambda r: {
                "title": r.get("title"),
                **_range(r),
                "url": r.get("url"),
                "description": r.get("description"),
            },
        ),
        "publications": rows(
            "profilePublications",
            lambda r: {
                "name": r.get("name"),
                "publisher": r.get("publisher"),
                "published": _date(r.get("publishedOn")),
                "url": r.get("url"),
                "description": r.get("description"),
            },
        ),
        "patents": rows(
            "profilePatents",
            lambda r: {
                "title": r.get("title"),
                "issuer": r.get("issuer"),
                "number": r.get("patentNumber") or r.get("applicationNumber"),
                "pending": r.get("pending"),
                "filed": _date(r.get("filedOn")),
                "issued": _date(r.get("issuedOn")),
                "description": r.get("description"),
            },
        ),
        "courses": rows(
            "profileCourses",
            lambda r: {"name": r.get("name"), "number": r.get("number")},
        ),
        "test_scores": rows(
            "profileTestScores",
            lambda r: {
                "name": r.get("name"),
                "score": r.get("score"),
                "date": _date(r.get("dateOn")),
            },
        ),
    }


def _shared(
    mine: list[dict[str, Any]],
    theirs: list[dict[str, Any]],
    *,
    urn_key: str,
    name_key: str,
) -> list[dict[str, Any]]:
    """Entries at the same organisation, matched by URN and by name otherwise.

    The URN is the identity. A name is used only when one side has no URN,
    which is how LinkedIn stores an employer or school typed in free text; two
    entries that both carry URNs and differ are different places even when
    their names agree.
    """

    def key(entry: dict[str, Any]) -> tuple[str, str] | None:
        if entry.get(urn_key):
            return ("urn", entry[urn_key])
        name = (entry.get(name_key) or "").strip().casefold()
        return ("name", name) if name else None

    def name_only(entry: dict[str, Any]) -> tuple[str, str] | None:
        name = (entry.get(name_key) or "").strip().casefold()
        return ("name", name) if name else None

    shared: list[dict[str, Any]] = []
    for their in theirs:
        for my in mine:
            same = key(my) is not None and key(my) == key(their)
            if not same and not (my.get(urn_key) and their.get(urn_key)):
                same = name_only(my) is not None and name_only(my) == name_only(their)
            if not same:
                continue
            shared.append(
                {
                    name_key: their.get(name_key) or my.get(name_key),
                    urn_key: their.get(urn_key) or my.get(urn_key),
                    "matched_by": "urn"
                    if my.get(urn_key) and their.get(urn_key)
                    else "name",
                    "mine": my,
                    "theirs": their,
                    # None means the two were there at different times, or one
                    # entry has no start date to place it by.
                    "overlap": overlap(my, their),
                }
            )
    return shared


def _by_employer(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Overlapping title pairs rolled up to one span per employer.

    Two people who shared an employer across several of each other's titles
    produce one pair per title, which reads as several short stints. The
    question a person asks is "how long were we both there", so the spans are
    merged: adjacent and overlapping months are joined, and a gap is kept as a
    gap in `months` rather than papered over.
    """
    spans: dict[str, list[tuple[int, int | None]]] = {}
    names: dict[str, str] = {}
    for pair in pairs:
        key = (pair.get("company") or "").strip().casefold()
        if not key or not pair.get("overlap"):
            continue
        names.setdefault(key, pair["company"])
        start = _months(pair["overlap"]["start"], end=False)
        end = _months(pair["overlap"]["end"], end=True)
        if start is not None:
            spans.setdefault(key, []).append((start, end))

    def text(months: int) -> str:
        return f"{months // 12:04d}-{months % 12 + 1:02d}"

    rolled = []
    for key, found in spans.items():
        covered: set[int] = set()
        ongoing_from: int | None = None
        for start, end in found:
            if end is None:
                ongoing_from = (
                    start if ongoing_from is None else min(ongoing_from, start)
                )
            else:
                covered.update(range(start, end + 1))
        first = min([start for start, _ in found])
        rolled.append(
            {
                "company": names[key],
                "start": text(first),
                "end": None if ongoing_from is not None else text(max(covered)),
                # Counted month by month, so a gap between two stints is not
                # counted. None while either side is still there.
                "months": None if ongoing_from is not None else len(covered),
            }
        )
    return sorted(rolled, key=lambda entry: entry["start"])


def common_ground(mine: dict[str, Any], theirs: dict[str, Any]) -> dict[str, Any]:
    """What two parsed profiles share, each match carrying both sides' entries."""

    def items(profile: dict[str, Any], section: str) -> list[dict[str, Any]]:
        return (profile.get(section) or {}).get("items") or []

    def names(profile: dict[str, Any], section: str, field: str) -> dict[str, str]:
        return {
            (item.get(field) or "").strip().casefold(): item.get(field) or ""
            for item in items(profile, section)
            if (item.get(field) or "").strip()
        }

    def shared_names(section: str, field: str) -> list[str]:
        my, their = names(mine, section, field), names(theirs, section, field)
        return sorted(their[name] for name in my.keys() & their.keys())

    companies = _shared(
        items(mine, "positions"),
        items(theirs, "positions"),
        urn_key="company_urn",
        name_key="company",
    )
    my_location = (mine.get("identity") or {}).get("location")
    their_location = (theirs.get("identity") or {}).get("location")
    return {
        "companies": companies,
        # The strongest signal there is: the same employer at the same time.
        "worked_together": [c for c in companies if c["overlap"]],
        # The same thing as one line per employer: how long both were there.
        "worked_together_by_employer": _by_employer(
            [c for c in companies if c["overlap"]]
        ),
        "schools": _shared(
            items(mine, "education"),
            items(theirs, "education"),
            urn_key="school_urn",
            name_key="school",
        ),
        "organizations": shared_names("organizations", "name"),
        "volunteering": shared_names("volunteering", "organization"),
        "certification_authorities": shared_names("certifications", "authority"),
        "languages": shared_names("languages", "name"),
        # Both skill lists are capped at 20 by the server, so an absent skill
        # here is not evidence that it is not shared.
        "skills": shared_names("skills", "name"),
        "same_location": bool(my_location) and my_location == their_location,
        "location": their_location if my_location == their_location else None,
    }


class VoyagerPersonReader(VoyagerReader):
    """Read a member's full profile without rendering it."""

    surface = "person"

    def _url(self, identifier: str, decoration: str) -> str:
        return (
            f"{_PROFILES}?q=memberIdentity"
            f"&memberIdentity={quote(identifier, safe='')}"
            f"&decorationId={_DECORATION}{decoration}"
        )

    async def _full_profile(self, identifier: str) -> dict[str, Any]:
        payload = await self._fetch(self._url(identifier, FULL_PROFILE))
        data = payload.get("data") or {}
        found = self._has_rows_key(data)
        urns = [
            urn
            for urn in (data.get("*elements") or [] if found else [])
            if isinstance(urn, str) and urn.startswith(_PROFILE_URN_PREFIX)
        ]
        self._refuse_unexplained_zero(
            rows=urns, payload=payload, path=_ELEMENTS_PATH, container_found=found
        )
        if len(urns) != 1:
            raise LinkedInScraperException(
                f"Voyager {self.surface} found {len(urns)} members for "
                f"{identifier!r}, not exactly one. Pass the /in/ public "
                "identifier exactly as a profile URL shows it."
            )
        return parse_profile(payload)

    async def _relationship(self, identifier: str) -> str | None:
        """How the signed-in member relates to this one, or None if unknown.

        Best effort: a profile is still worth returning when this second read
        fails, and an unknown relationship is said to be unknown.
        """
        try:
            payload = await self._fetch(self._url(identifier, TOP_CARD))
        except (AuthenticationError, RateLimitError):
            raise
        except LinkedInScraperException as exc:
            logger.info("Relationship read unavailable: %s", exc)
            return None
        for entity in payload.get("included") or []:
            if not str(entity.get("$type", "")).endswith(".MemberRelationship"):
                continue
            union = entity.get("memberRelationshipUnion") or {}
            if "self" in union:
                return "self"
            if "*connection" in union or "connection" in union:
                return "connection"
            # Anything else is named as LinkedIn names it rather than guessed
            # at: the non-connection shapes have not been observed.
            keys = sorted(key.lstrip("*") for key in union if not key.startswith("$"))
            return keys[0] if keys else None
        return None

    async def _my_profile(self) -> dict[str, Any]:
        global _MY_PROFILE_CACHE
        page = self._session.page
        if _MY_PROFILE_CACHE is not None and _MY_PROFILE_CACHE[0] is page:
            return _MY_PROFILE_CACHE[1]
        mine = await self._full_profile((await self._mailbox_urn()).rsplit(":", 1)[-1])
        _MY_PROFILE_CACHE = (page, mine)
        return mine

    async def get_person(
        self, linkedin_username: str, compare_to_me: bool = True
    ) -> dict[str, Any]:
        """Read one member's whole profile, and what it shares with yours."""
        from linkedin_mcp_server.scraping.identifiers import (
            normalize_person_identifier,
            person_profile_url,
        )

        identifier = normalize_person_identifier(linkedin_username)
        profile = await self._full_profile(identifier)
        relationship = await self._relationship(identifier)

        result: dict[str, Any] = {
            "url": person_profile_url(
                profile["identity"].get("public_identifier") or identifier, "/"
            ),
            "sections": {"profile": render_profile(profile)},
            "relationship": relationship,
            **profile,
            "incomplete_sections": sorted(
                name
                for name, section in profile.items()
                if isinstance(section, dict) and section.get("complete") is False
            ),
        }
        if compare_to_me and relationship != "self":
            mine = await self._my_profile()
            if mine["identity"].get("profile_urn") != profile["identity"].get(
                "profile_urn"
            ):
                result["common_ground"] = common_ground(mine, profile)
        return result


def render_profile(profile: dict[str, Any]) -> str:
    """The profile as readable text, for consumers that read `sections`."""
    identity = profile.get("identity") or {}
    lines = [identity.get("name") or "Unknown"]
    for key in ("headline", "location", "industry"):
        if identity.get(key):
            lines.append(identity[key])
    if identity.get("summary"):
        lines += ["", "About", identity["summary"]]

    def span(item: dict[str, Any]) -> str:
        if not item.get("start") and not item.get("end"):
            return ""
        return f" ({item.get('start') or '?'} to {item.get('end') or 'present'})"

    positions = (profile.get("positions") or {}).get("items") or []
    if positions:
        lines += ["", "Experience"]
        for item in positions:
            lines.append(
                f"{item.get('title') or '?'} at {item.get('company') or '?'}{span(item)}"
            )
    schools = (profile.get("education") or {}).get("items") or []
    if schools:
        lines += ["", "Education"]
        for item in schools:
            degree = ", ".join(p for p in (item.get("degree"), item.get("field")) if p)
            lines.append(
                f"{item.get('school') or '?'}{': ' + degree if degree else ''}{span(item)}"
            )
    for title, section, field in (
        ("Skills", "skills", "name"),
        ("Certifications", "certifications", "name"),
        ("Honors", "honors", "title"),
        ("Languages", "languages", "name"),
        ("Organizations", "organizations", "name"),
        ("Volunteering", "volunteering", "organization"),
        ("Projects", "projects", "title"),
        ("Publications", "publications", "name"),
        ("Patents", "patents", "title"),
    ):
        body = profile.get(section) or {}
        names = [item.get(field) for item in body.get("items") or [] if item.get(field)]
        if not names:
            continue
        note = ""
        if body.get("complete") is False:
            note = f" (first {body['returned']} of {body['total']})"
        lines += ["", f"{title}{note}", ", ".join(names)]
    return "\n".join(lines)
