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

**Contact info, mutual connections and posts are three more REST reads.** The
pages that show them are rendered on the server now and issue no data request
of their own, so there was nothing to observe; each endpoint below was found by
asking for it and reading the answer, the same day.

- ``...profile.ProfileContactInfo-2`` on the same finder returns the contact
  fields the member lets this viewer see: websites, email, phone numbers,
  Twitter handles, messengers, address, birthday. A field the member does not
  share is null, which is not the same as the member having none.
- ``identity/profiles/<id>/memberConnections?q=inCommon`` returns the people
  both members are connected to, with ``paging.total``. Each row carries the
  connection's ``MiniProfile`` and an introduction insight. This is the answer
  to "who could introduce us". It needs the member's id, not their public
  identifier.
- ``identity/profileUpdatesV2?q=memberShareFeed&profileUrn=<urn>`` returns the
  member's posts and reposts. The order is ``data['*elements']``; ``included``
  also holds the originals of reshares, so reading ``included`` alone doubles
  them. An activity id is a snowflake whose high bits are its creation time in
  milliseconds, which is where ``posted_at_iso`` comes from; checked against
  LinkedIn's own "5 months ago" on six posts.
  It pages by ``paginationToken`` and ignores ``start``: an offset of five
  returned the first five again.

The legacy ``profileContactInfo`` and ``networkinfo`` routes answer HTTP 410.

**Not measured:** whether these reads register as a profile view. The profile
page's own tracking is a separate request this does not make, but that has not
been confirmed from the other side.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
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
CONTACT_INFO = "ProfileContactInfo-2"

_LEGACY = "https://www.linkedin.com/voyager/api/identity"

#: How many mutual connections `get_person` reads inline. The rest are one
#: `get_mutual_connections` call away, and `complete` says when there are more.
MUTUAL_INLINE = 40

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


def parse_contact(payload: dict[str, Any]) -> dict[str, Any]:
    """The contact fields this viewer is allowed to see, absent ones dropped."""
    profile: dict[str, Any] = {}
    for entity in payload.get("included") or []:
        if str(entity.get("$type", "")).endswith(".Profile"):
            profile = entity
            break
    phones = [
        _clean(
            {
                "number": (phone.get("phoneNumber") or {}).get("number")
                if isinstance(phone.get("phoneNumber"), dict)
                else phone.get("number"),
                "type": phone.get("type"),
            }
        )
        for phone in profile.get("phoneNumbers") or []
        if isinstance(phone, dict)
    ]
    email = profile.get("emailAddress")
    return _clean(
        {
            "email": email.get("emailAddress") if isinstance(email, dict) else email,
            "phones": [phone for phone in phones if phone],
            "websites": [
                _clean(
                    {
                        "url": site.get("url"),
                        "category": site.get("category"),
                        "label": site.get("label"),
                    }
                )
                for site in profile.get("websites") or []
                if isinstance(site, dict) and site.get("url")
            ],
            "twitter": [
                handle.get("name")
                for handle in profile.get("twitterHandles") or []
                if isinstance(handle, dict) and handle.get("name")
            ],
            "messengers": [
                _clean({"provider": im.get("provider"), "id": im.get("id")})
                for im in profile.get("instantMessengers") or []
                if isinstance(im, dict)
            ],
            "address": profile.get("address"),
            "birthday": _date(profile.get("birthDateOn"))
            or (
                f"--{profile['birthDateOn']['month']:02d}-{profile['birthDateOn']['day']:02d}"
                if isinstance(profile.get("birthDateOn"), dict)
                and profile["birthDateOn"].get("month")
                and profile["birthDateOn"].get("day")
                else None
            ),
        }
    )


def parse_mutual(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], int | None, bool]:
    """Mutual connections, the server's total, and whether the list was found."""
    data = payload.get("data") or {}
    found = isinstance(data, dict) and "elements" in data
    minis = {
        entity.get("entityUrn"): entity
        for entity in payload.get("included") or []
        if isinstance(entity, dict)
    }
    rows = []
    for element in data.get("elements") or []:
        mini = minis.get(element.get("*miniProfile")) or {}
        if not mini:
            continue
        insight = element.get("introductionBrokerInsight") or {}
        rows.append(
            _clean(
                {
                    "name": " ".join(
                        part
                        for part in (mini.get("firstName"), mini.get("lastName"))
                        if part
                    ),
                    "headline": mini.get("occupation"),
                    "public_identifier": mini.get("publicIdentifier"),
                    "profile_urn": mini.get("dashEntityUrn"),
                    "distance": (element.get("distance") or {}).get("value"),
                    # LinkedIn's own suggested ask, kept because it is what
                    # the member would see on the "ask for an intro" button.
                    "suggested_ask": (insight.get("preFilledText") or {}).get("text"),
                }
            )
        )
    total = (data.get("paging") or {}).get("total")
    return rows, total if isinstance(total, int) else None, found


def _activity_time(urn: str) -> str | None:
    """When an activity was created, read from its id.

    The id is a snowflake: its bits above the low 22 are a millisecond epoch.
    Anything that is not such an id answers None rather than a wrong date.
    """
    tail = urn.rsplit(":", 1)[-1]
    if not tail.isdigit() or len(tail) < 18:
        return None
    return datetime.fromtimestamp((int(tail) >> 22) / 1000, tz=timezone.utc).isoformat(
        timespec="minutes"
    )


def parse_posts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """A member's posts and reposts, in the order the server lists them."""
    included = [e for e in payload.get("included") or [] if isinstance(e, dict)]
    updates = {
        entity.get("entityUrn"): entity
        for entity in included
        if str(entity.get("$type", "")).endswith(".UpdateV2")
    }
    counts = {
        entity.get("urn"): entity
        for entity in included
        if str(entity.get("$type", "")).endswith(".SocialActivityCounts")
    }

    def text(node: Any) -> str | None:
        inner = (node or {}).get("text") if isinstance(node, dict) else None
        return inner.get("text") if isinstance(inner, dict) else inner

    posts = []
    for urn in (payload.get("data") or {}).get("*elements") or []:
        update = updates.get(urn)
        if not update:
            continue
        activity = (update.get("updateMetadata") or {}).get("urn") or ""
        actor = update.get("actor") or {}
        original = updates.get(update.get("*resharedUpdate")) or {}
        tally = counts.get(activity) or {}
        posts.append(
            _clean(
                {
                    "activity_urn": activity,
                    "url": f"https://www.linkedin.com/feed/update/{activity}/"
                    if activity
                    else None,
                    "posted_at_iso": _activity_time(activity),
                    "author": text({"text": actor.get("name")}),
                    # Present when the member reposted someone else's update
                    # without adding to it; `author` is then the original's.
                    "repost_header": text(
                        {"text": (update.get("header") or {}).get("text")}
                    ),
                    "text": text(update.get("commentary")),
                    "reshared_text": text(original.get("commentary")),
                    "reshared_author": text(
                        {"text": (original.get("actor") or {}).get("name")}
                    ),
                    "likes": tally.get("numLikes"),
                    "comments": tally.get("numComments"),
                    "shares": tally.get("numShares"),
                }
            )
        )
    return posts


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

    async def _contact(self, identifier: str) -> dict[str, Any] | None:
        """Contact fields, or None when the read itself failed. Best effort."""
        try:
            payload = await self._fetch(self._url(identifier, CONTACT_INFO))
        except (AuthenticationError, RateLimitError):
            raise
        except LinkedInScraperException as exc:
            logger.info("Contact read unavailable: %s", exc)
            return None
        return parse_contact(payload)

    async def _member_id(self, linkedin_username: str) -> tuple[str, str]:
        """A member's id and canonical profile URL, from any identifier."""
        from linkedin_mcp_server.scraping.identifiers import (
            normalize_person_identifier,
            person_profile_url,
        )

        identifier = normalize_person_identifier(linkedin_username)
        payload = await self._fetch(
            f"{_PROFILES}?q=memberIdentity&memberIdentity={quote(identifier, safe='')}"
        )
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
        return urns[0].rsplit(":", 1)[-1], person_profile_url(identifier, "/")

    async def _mutual(
        self, member_id: str, *, start: int, count: int
    ) -> dict[str, Any]:
        payload = await self._fetch(
            f"{_LEGACY}/profiles/{quote(member_id, safe='')}/memberConnections"
            f"?q=inCommon&start={start}&count={count}"
        )
        rows, total, found = parse_mutual(payload)
        self._refuse_unexplained_zero(
            rows=rows,
            payload=payload,
            path="data.elements",
            container_found=found,
        )
        return {
            "items": rows,
            "returned": len(rows),
            "start": start,
            "total": total,
            # Measured against the server's total. None when it gave none.
            "complete": (start + len(rows) >= total) if total is not None else None,
        }

    async def get_mutual_connections(
        self, linkedin_username: str, start: int = 0, count: int = MUTUAL_INLINE
    ) -> dict[str, Any]:
        """Read one page of the connections you share with a member."""
        if start < 0 or count < 1:
            raise LinkedInScraperException(
                f"start must be >= 0 and count >= 1, got start={start}, count={count}."
            )
        member_id, url = await self._member_id(linkedin_username)
        page = await self._mutual(member_id, start=start, count=count)
        lines = [
            f"{row.get('name')} - {row.get('headline') or ''}".rstrip(" -")
            for row in page["items"]
        ]
        return {
            "url": url,
            "sections": {"mutual_connections": "\n".join(lines)},
            "mutual_connections": page["items"],
            "count": page["returned"],
            "start": start,
            "page_size": count,
            "total": page["total"],
            "at_end": page["complete"] if page["items"] else None,
        }

    async def get_person_posts(
        self, linkedin_username: str, count: int = 10, cursor: str | None = None
    ) -> dict[str, Any]:
        """Read one page of a member's posts and reposts, newest activity first."""
        if count < 1:
            raise LinkedInScraperException(f"count must be >= 1, got {count}.")
        if cursor is not None and not cursor.strip():
            raise LinkedInScraperException(
                "cursor was blank. OMIT the argument for the first page, or "
                "pass a next_cursor from a previous call."
            )
        member_id, url = await self._member_id(linkedin_username)
        urn = quote(f"{_PROFILE_URN_PREFIX}{member_id}", safe="")
        # Measured on 2026-10-02: this endpoint IGNORES `start`. Asking for
        # start=5 returned the first five again, with the same token. Only
        # `paginationToken` moves the page, so that is the cursor.
        paging = f"&paginationToken={quote(cursor.strip(), safe='')}" if cursor else ""
        payload = await self._fetch(
            f"{_LEGACY}/profileUpdatesV2?count={count}&includeLongTermHistory=true"
            "&moduleKey=member-shares%3Aphone&numComments=0&numLikes=0"
            f"&profileUrn={urn}&q=memberShareFeed{paging}"
        )
        data = payload.get("data") or {}
        posts = parse_posts(payload)
        self._refuse_unexplained_zero(
            rows=posts,
            payload=payload,
            path=_ELEMENTS_PATH,
            container_found=self._has_rows_key(data),
        )
        token = (data.get("metadata") or {}).get("paginationToken")
        if not isinstance(token, str) or not token or token == (cursor or "").strip():
            # A token equal to the one supplied is the server re-serving the
            # page just read; handing it back would loop the caller forever.
            token = None
        lines = []
        for post in posts:
            who = post.get("repost_header") or post.get("author") or "?"
            body = post.get("text") or post.get("reshared_text") or ""
            lines.append(f"{who} - {post.get('posted_at_iso')}\n{body}")
        return {
            "url": f"{url}recent-activity/all/",
            "sections": {"posts": "\n\n".join(lines)},
            "posts": posts,
            "count": len(posts),
            "page_size": count,
            "next_cursor": token,
            # Measured from what came back; an empty page proves nothing.
            "at_end": None if not posts else len(posts) < count,
        }

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
        contact = await self._contact(identifier)
        member_id = (profile["identity"].get("profile_urn") or "").rsplit(":", 1)[-1]

        result: dict[str, Any] = {
            "url": person_profile_url(
                profile["identity"].get("public_identifier") or identifier, "/"
            ),
            # Keyed as upstream keys it. The degree is in the text because
            # consumers of the old tool read "1st" off the rendered page.
            "sections": {"main_profile": render_profile(profile, relationship)},
            "relationship": relationship,
            # None when the read failed; {} when it worked and the member
            # shares nothing with this viewer. Those are different answers.
            "contact": contact,
            **profile,
            "incomplete_sections": sorted(
                name
                for name, section in profile.items()
                if isinstance(section, dict) and section.get("complete") is False
            ),
        }
        if relationship != "self" and member_id:
            # Who could introduce the two of you. Read here because it is the
            # other half of "how are we connected", beside common_ground.
            result["mutual_connections"] = await self._mutual(
                member_id, start=0, count=MUTUAL_INLINE
            )
        if compare_to_me and relationship != "self":
            mine = await self._my_profile()
            if mine["identity"].get("profile_urn") != profile["identity"].get(
                "profile_urn"
            ):
                result["common_ground"] = common_ground(mine, profile)
        return result


def render_profile(profile: dict[str, Any], relationship: str | None = None) -> str:
    """The profile as readable text, for consumers that read `sections`."""
    identity = profile.get("identity") or {}
    lines = [identity.get("name") or "Unknown"]
    if relationship == "connection":
        lines.append("1st degree connection")
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
