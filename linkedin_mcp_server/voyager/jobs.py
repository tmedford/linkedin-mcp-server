"""Search jobs and read a job posting from LinkedIn's API, not its pages.

Upstream's ``search_jobs`` loads the results page and scrolls it, and its
``get_job_details`` loads the posting page; both return page text. This asks the
endpoints those pages are built from and returns records.

**Measured on 2026-10-02, one account.**

- **Search** is ``voyagerJobsDashJobCards`` with
  ``decorationId=...jobs.search.JobSearchCardsCollection-221`` and
  ``q=jobSearch``: plain REST, no query id to rotate. The search page issued
  exactly this. Its ``query`` takes ``keywords``, a ``locationUnion`` and
  ``selectedFilters``.
- Every filter upstream offers changed the result set, sent as the page's own
  codes (the same ones upstream's URL builder maps to): ``timePostedRange``
  (``r86400``), ``jobType`` (``F``), ``experience`` (``5``), ``workplaceType``
  (``2``), ``applyWithLinkedin`` (``true``) and ``sortBy`` (``DD``). A place
  is ``locationUnion:(geoId:<id>)`` or ``(seoLocation:(location:<text>))``;
  both gave 559 for New York against 2076 without.
- ``start`` pages it (offset 25 began with other jobs) and ``count`` is
  honoured at 50. Unlike people search, ``paging.total`` moved with every
  filter, so here it is reported as LinkedIn's count of matches.
- **The order is ``data.elements[].jobCardUnion.*jobPostingCard``.** The card
  carries title, company, location, benefits, LinkedIn's insight ("You'd be a
  top applicant") and footer items typed ``LISTED_DATE`` (with ``timeAt``),
  ``EASY_APPLY_TEXT`` and ``PROMOTED``. Those types are read, never the words.
- **A posting** is ``jobs/jobPostings/<id>`` with
  ``decorationId=com.linkedin.voyager.deco.jobs.web.shared.WebFullJobPosting-65``:
  the whole posting in one answer, description included. The page itself reads
  a dozen GraphQL "detail section" queries whose ids rotate; the dash variants
  of this resource answered 400.
- **The page writes; this does not.** Opening the search page posts the query
  to ``voyagerJobsDashJobSearchHistories?action=addFromQuery``, so every
  upstream search lands in the member's job-search history. Nothing here posts.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.voyager.people_search import VoyagerPeopleSearch

logger = logging.getLogger(__name__)

_API = "https://www.linkedin.com/voyager/api/"
_CARDS = (
    f"{_API}voyagerJobsDashJobCards?decorationId="
    "com.linkedin.voyager.dash.deco.jobs.search.JobSearchCardsCollection-221"
)
_POSTING = (
    f"{_API}jobs/jobPostings/{{job_id}}?decorationId="
    "com.linkedin.voyager.deco.jobs.web.shared.WebFullJobPosting-65"
)
_ELEMENTS_PATH = "data.elements[].jobCardUnion.*jobPostingCard"

#: Results per page, as the search page asks for them.
PAGE_SIZE = 25

#: Workplace type ids, as LinkedIn numbers them (upstream's WORK_TYPE_MAP).
_WORKPLACE = {"1": "on_site", "2": "remote", "3": "hybrid"}


def _filters() -> dict[str, tuple[str, dict[str, str]]]:
    """Each upstream argument -> (LinkedIn's filter name, name -> code)."""
    from linkedin_mcp_server.scraping.search_urls import (
        EXPERIENCE_LEVEL_MAP,
        JOB_DATE_POSTED_MAP,
        JOB_TYPE_MAP,
        SORT_BY_MAP,
        WORK_TYPE_MAP,
    )

    return {
        "date_posted": ("timePostedRange", JOB_DATE_POSTED_MAP),
        "job_type": ("jobType", JOB_TYPE_MAP),
        "experience_level": ("experience", EXPERIENCE_LEVEL_MAP),
        "work_type": ("workplaceType", WORK_TYPE_MAP),
        "sort_by": ("sortBy", SORT_BY_MAP),
    }


def selected_filters(**arguments: Any) -> str:
    """The ``selectedFilters`` body for upstream's filter arguments.

    Upstream passes an unknown value through to the URL, where LinkedIn
    ignores it and answers unfiltered while the result reads as filtered.
    Here a value must be one of upstream's names or the code it maps to, or
    the call is refused naming the accepted ones.
    """
    parts = []
    for argument, (name, mapping) in _filters().items():
        value = arguments.get(argument)
        if not value:
            continue
        # Only some filters take several values; date and sort take one.
        values = [v.strip() for v in str(value).split(",") if v.strip()]
        if name in ("timePostedRange", "sortBy") and len(values) > 1:
            raise LinkedInScraperException(
                f"{argument} takes one value, got {value!r}."
            )
        codes = []
        for item in values:
            if item in mapping:
                codes.append(mapping[item])
            elif item in mapping.values():
                codes.append(item)
            else:
                raise LinkedInScraperException(
                    f"{argument} was {item!r}. Pass one of: {', '.join(mapping)}."
                )
        parts.append(f"{name}:List({','.join(codes)})")
    if arguments.get("easy_apply"):
        parts.append("applyWithLinkedin:List(true)")
    company = arguments.get("company_id")
    if company:
        ids = [c.strip() for c in str(company).split(",") if c.strip()]
        if not all(c.isdigit() for c in ids):
            raise LinkedInScraperException(
                f"company_id was {company!r}. Pass LinkedIn's numeric company "
                "id (get_recruiter_views and search_jobs report it), not a name."
            )
        parts.append(f"company:List({','.join(ids)})")
    return ",".join(parts)


def _text(node: Any) -> str | None:
    return node.get("text") if isinstance(node, dict) else None


def _iso(milliseconds: Any) -> str | None:
    if not isinstance(milliseconds, (int, float)) or milliseconds <= 0:
        return None
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).isoformat(
        timespec="minutes"
    )


def _attributed_text(node: Any) -> str | None:
    """Text with LinkedIn's line-break attributes turned back into newlines.

    The description's ``text`` has no newlines of its own; where they go is
    a ``LineBreak`` attribute at an offset.
    """
    text = _text(node)
    if not text:
        return text
    breaks = sorted(
        {
            attribute.get("start")
            for attribute in node.get("attributes") or []
            if isinstance(attribute, dict)
            and "lineBreak" in (attribute.get("attributeKindUnion") or {})
            and isinstance(attribute.get("start"), int)
        },
        reverse=True,
    )
    for start in breaks:
        text = text[:start] + "\n" + text[start:]
    return text.strip()


def _clean(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if value not in (None, "", [])}


def parse_job_cards(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool, int]:
    """Jobs in the order LinkedIn ranked them, whether the container was
    found, and how many results the page held before any was dropped."""
    data = payload.get("data") or {}
    found = isinstance(data, dict) and "elements" in data
    by_urn = {
        entity.get("entityUrn"): entity
        for entity in payload.get("included") or []
        if isinstance(entity, dict)
    }
    jobs = []
    items_seen = 0
    for element in data.get("elements") or []:
        card_urn = ((element or {}).get("jobCardUnion") or {}).get("*jobPostingCard")
        if not card_urn:
            continue
        items_seen += 1
        card = by_urn.get(card_urn) or {}
        posting = str(card.get("jobPostingUrn") or card.get("*jobPosting") or "")
        job_id = posting.rsplit(":", 1)[-1] if posting else None
        if not job_id or not job_id.isdigit():
            continue
        footer = {
            item.get("type"): item
            for item in card.get("footerItems") or []
            if isinstance(item, dict)
        }
        logo = next(
            (
                (attribute.get("detailDataUnion") or {}).get("companyLogo")
                for attribute in (card.get("logo") or {}).get("attributes") or []
                if isinstance(attribute, dict)
            ),
            None,
        )
        company = by_urn.get(logo or "") or {}
        jobs.append(
            _clean(
                {
                    "job_id": job_id,
                    "title": card.get("jobPostingTitle")
                    or (_text(card.get("title")) or "").strip(),
                    "company": _text(card.get("primaryDescription"))
                    or company.get("name"),
                    "company_id": str(logo).rsplit(":", 1)[-1] if logo else None,
                    "location": _text(card.get("secondaryDescription")),
                    "detail": _text(card.get("tertiaryDescription")),
                    "insight": _text((card.get("relevanceInsight") or {}).get("text")),
                    "listed_at_iso": _iso(
                        (footer.get("LISTED_DATE") or {}).get("timeAt")
                    ),
                    # By footer TYPE, so the words do not matter.
                    "easy_apply": "EASY_APPLY_TEXT" in footer,
                    "promoted": "PROMOTED" in footer,
                    "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
                }
            )
        )
    return jobs, found, items_seen


def render_jobs(jobs: list[dict[str, Any]]) -> str:
    lines = []
    for job in jobs:
        lines.append(f"{job.get('title')} - {job.get('company')} ({job.get('job_id')})")
        for key in ("location", "insight", "detail"):
            if job.get(key):
                lines.append(f"    {job[key]}")
    return "\n".join(lines)


def parse_posting(payload: dict[str, Any]) -> dict[str, Any] | None:
    """One job posting as a record, or None when the answer has none."""
    data = payload.get("data") or {}
    if not isinstance(data, dict) or not data.get("jobPostingId"):
        return None
    by_urn = {
        entity.get("entityUrn"): entity
        for entity in payload.get("included") or []
        if isinstance(entity, dict)
    }
    company_urn = (data.get("companyDetails") or {}).get("company") or ""
    company = by_urn.get(company_urn) or {}
    apply = data.get("applyMethod") or {}
    apply_type = str(apply.get("$type") or "")
    workplaces = [
        _WORKPLACE.get(str(urn).rsplit(":", 1)[-1], str(urn))
        for urn in data.get("workplaceTypes") or []
    ]
    return _clean(
        {
            "job_id": str(data.get("jobPostingId")),
            "title": data.get("title"),
            "company": company.get("name"),
            "company_id": company_urn.rsplit(":", 1)[-1] if company_urn else None,
            "company_url": company.get("url"),
            "company_size": company.get("staffCount"),
            "location": data.get("formattedLocation"),
            "workplace": workplaces,
            "employment_status": data.get("formattedEmploymentStatus"),
            "experience_level": data.get("formattedExperienceLevel"),
            "industries": data.get("formattedIndustries"),
            "job_functions": data.get("formattedJobFunctions"),
            "listed_at_iso": _iso(data.get("listedAt")),
            "original_listed_at_iso": _iso(data.get("originalListedAt")),
            "expire_at_iso": _iso(data.get("expireAt")),
            "closed_at_iso": _iso(data.get("closedAt")),
            # LISTED, CLOSED...: LinkedIn's enum, not text.
            "job_state": data.get("jobState"),
            "applies": data.get("applies"),
            "views": data.get("views"),
            # Offsite means the company's own site; otherwise LinkedIn's own
            # apply flow (Easy Apply). Told apart by the method's type.
            "easy_apply": bool(apply_type) and not apply_type.endswith("OffsiteApply"),
            "apply_url": apply.get("companyApplyUrl"),
            "description": _attributed_text(data.get("description")),
            "url": f"https://www.linkedin.com/jobs/view/{data.get('jobPostingId')}/",
        }
    )


def render_posting(job: dict[str, Any]) -> str:
    head = [
        job.get("title"),
        job.get("company"),
        job.get("location"),
        ", ".join(
            part
            for part in (job.get("employment_status"), job.get("experience_level"))
            if part
        ),
        f"Listed {job.get('listed_at_iso')}" if job.get("listed_at_iso") else None,
        f"Apply: {job['apply_url']}" if job.get("apply_url") else None,
    ]
    lines = [line for line in head if line]
    if job.get("description"):
        lines += ["", job["description"]]
    return "\n".join(lines)


class VoyagerJobs(VoyagerPeopleSearch):
    """Find jobs and read postings without loading LinkedIn's job pages."""

    surface = "jobs"

    async def find_jobs(
        self,
        keywords: str,
        location: str | None = None,
        max_pages: int = 3,
        date_posted: str | None = None,
        job_type: str | None = None,
        experience_level: str | None = None,
        work_type: str | None = None,
        easy_apply: bool = False,
        sort_by: str | None = None,
        company_id: str | None = None,
    ) -> dict[str, Any]:
        """Read up to ``max_pages`` pages of 25 jobs matching a search."""
        from linkedin_mcp_server.scraping.search_urls import build_job_search_url

        if not keywords.strip() and not company_id:
            # Measured: a search on a company alone answers (494 roles for
            # one company with no keywords), so words are optional then.
            raise LinkedInScraperException(
                "keywords was blank. Pass the words to search for, or a "
                "company_id to list that company's jobs."
            )
        if not 1 <= max_pages <= 10:
            raise LinkedInScraperException(
                f"max_pages must be between 1 and 10, got {max_pages}."
            )
        filters = selected_filters(
            date_posted=date_posted,
            job_type=job_type,
            experience_level=experience_level,
            work_type=work_type,
            easy_apply=easy_apply,
            sort_by=sort_by,
            company_id=company_id,
        )
        place = ""
        resolved: dict[str, Any] | None = None
        candidates: list[dict[str, Any]] = []
        if location and location.strip():
            # A place LinkedIn knows becomes a geo filter and is reported back;
            # anything else ("Remote") is refused by the lookup, and work_type
            # is the filter for remote work.
            resolved, candidates = await self._geo(location)
            place = f",locationUnion:(geoId:{resolved['geo_id']})"
        words = (
            f"keywords:{quote(keywords.strip(), safe='')}" if keywords.strip() else ""
        )
        head = ",".join(part for part in (words, place.lstrip(",")) if part)
        query = (
            f"(origin:JOB_SEARCH_PAGE_OTHER_ENTRY,{head + ',' if head else ''}"
            f"selectedFilters:({filters}),spellCorrectionEnabled:true)"
        )

        jobs: list[dict[str, Any]] = []
        total: int | None = None
        complete = False
        for page in range(max_pages):
            if page:
                await self._session.delay(1.0)
            payload = await self._fetch(
                f"{_CARDS}&count={PAGE_SIZE}&q=jobSearch&query={query}"
                f"&start={page * PAGE_SIZE}"
            )
            found_jobs, found, items_seen = parse_job_cards(payload)
            if page == 0:
                self._refuse_unexplained_zero(
                    rows=found_jobs,
                    payload=payload,
                    path=_ELEMENTS_PATH,
                    container_found=found,
                )
            reported = ((payload.get("data") or {}).get("paging") or {}).get("total")
            if isinstance(reported, int):
                total = reported
            seen = {job["job_id"] for job in jobs}
            fresh = [job for job in found_jobs if job["job_id"] not in seen]
            jobs.extend(fresh)
            # A page with nothing new means the offset stopped moving the
            # results; reading on would only repeat it until max_pages.
            if items_seen < PAGE_SIZE or (page and not fresh):
                complete = True
                break

        result: dict[str, Any] = {
            "url": build_job_search_url(
                keywords,
                location,
                date_posted,
                job_type,
                experience_level,
                work_type,
                easy_apply,
                sort_by,
            ),
            "sections": {"search_results": render_jobs(jobs)},
            "job_ids": [job["job_id"] for job in jobs],
            "jobs": jobs,
            "count": len(jobs),
            "total": total,
            "complete": complete,
        }
        if resolved is not None:
            result["location_resolved"] = resolved
            result["location_candidates"] = candidates
        return result

    async def get_job(self, job_id: str) -> dict[str, Any]:
        """One job posting, whole."""
        job_id = str(job_id).strip()
        if not job_id.isdigit():
            raise LinkedInScraperException(
                f"job_id was {job_id!r}. Pass the numeric id from a job URL "
                "(/jobs/view/<id>/) or from search_jobs."
            )
        payload = await self._fetch(_POSTING.format(job_id=job_id))
        job = parse_posting(payload)
        if job is None:
            raise LinkedInScraperException(
                f"Voyager {self.surface} answered for job {job_id} with no "
                "posting in it."
            )
        return {
            "url": job["url"],
            "sections": {"job_posting": render_posting(job)},
            "job": job,
        }


# --- Saved jobs (the jobs tracker) ----------------------------------------
#
# LinkedIn moved saved jobs to ``/jobs-tracker/``, a server-rendered page with
# a tab per stage. Measured on 2026-10-03, one account:
#
# - The page issues no data request for its list: the rows arrive with the
#   page. The client loads a server-rendered route by POSTing to
#   ``/flagship-web/<route>/`` with ``isPrefetch: true`` (observed for the
#   feed and the member's own profile), and the same call on
#   ``/flagship-web/jobs-tracker/`` answered with the tracker as a component
#   stream, without the page being opened.
# - ``stage`` selects the tab, in the query string and the payload: ``saved``,
#   ``draft`` and ``clicked_apply`` (shown together as "In Progress"),
#   ``applied``, ``interview``, ``archived``. ``applied`` answered with no rows
#   on an account whose Applied tab read 0.
# - Each row's "Add note" action carries the job as a record: ``jobId``,
#   ``jobTitle``, ``companyName``, ``locationPrimary``, ``workplaceTypeName``,
#   ``listedAt``, ``originallyListedAt``, ``existingNote``, ``currentStageKey``,
#   ``isVerified``. Those records are read, not the rendered text.
# - The stream carried no pager, and its ten records matched the ten rows the
#   page showed. A tab heading read 11; what the eleventh is was not found.
# - The prefetch needs the client's own ``x-li-*`` headers, so they are copied
#   once per browser session from a prefetch the feed page sends.

TRACKER_URL = "https://www.linkedin.com/jobs-tracker/"
_TRACKER_ROUTE = "https://www.linkedin.com/flagship-web/jobs-tracker/"
STAGES = ("saved", "draft", "clicked_apply", "applied", "interview", "archived")
_RECORD_START = '{"jobId":"'

# Valid for as long as the page that issued them, as in profile_views.
_PREFETCH_HEADERS: tuple[Any, dict[str, str]] | None = None


def forget_prefetch_headers() -> None:
    """Drop the copied prefetch headers so the next read takes them again."""
    global _PREFETCH_HEADERS
    _PREFETCH_HEADERS = None


def parse_tracker_jobs(text: str) -> list[dict[str, Any]]:
    """Jobs from a tracker stream, in the order listed, one per job id."""
    decoder = json.JSONDecoder()
    jobs: dict[str, dict[str, Any]] = {}
    position = text.find(_RECORD_START)
    while position >= 0:
        try:
            record, _ = decoder.raw_decode(text, position)
        except ValueError:
            record = None
        # A row also carries smaller {"jobId": ...} payloads for its other
        # actions; the record is the one that names the job.
        if isinstance(record, dict) and record.get("jobTitle"):
            job_id = str(record.get("jobId") or "")
            if job_id.isdigit() and job_id not in jobs:
                location = record.get("locationPrimary")
                jobs[job_id] = _clean(
                    {
                        "job_id": job_id,
                        "title": str(record.get("jobTitle") or "").strip(),
                        "company": record.get("companyName"),
                        "location": location,
                        "workplace": record.get("workplaceTypeName"),
                        "listed_at_iso": _iso(_number(record.get("listedAt"))),
                        "original_listed_at_iso": _iso(
                            _number(record.get("originallyListedAt"))
                        ),
                        "stage": record.get("currentStageKey"),
                        "note": record.get("existingNote"),
                        "verified": record.get("isVerified"),
                        "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
                    }
                )
        position = text.find(_RECORD_START, position + 1)
    return list(jobs.values())


def _number(value: Any) -> float | None:
    """LinkedIn sends these timestamps as strings of digits."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def render_tracker(jobs: list[dict[str, Any]]) -> str:
    lines = []
    for job in jobs:
        place = " · ".join(
            part for part in (job.get("location"), job.get("workplace")) if part
        )
        lines.append(f"{job.get('title')} - {job.get('company')} ({job.get('job_id')})")
        if place:
            lines.append(f"    {place}")
        if job.get("note"):
            lines.append(f"    Note: {job['note']}")
    return "\n".join(lines)


class VoyagerSavedJobs(VoyagerJobs):
    """Read the jobs tracker without opening it."""

    surface = "saved-jobs"

    async def _prefetch_headers(self) -> dict[str, str]:
        """The headers the client sends when it prefetches a route."""
        from linkedin_mcp_server.voyager.profile_views import _BROWSER_OWNED

        global _PREFETCH_HEADERS
        page = self._session.page
        if _PREFETCH_HEADERS is not None and _PREFETCH_HEADERS[0] is page:
            return _PREFETCH_HEADERS[1]
        seen: list[Any] = []

        def _capture(request: Any) -> None:
            if (
                "/flagship-web/" in request.url
                and "/rsc-action/" not in request.url
                and request.method == "POST"
            ):
                seen.append(request)

        page.on("request", _capture)
        try:
            await self._navigator._navigate_to_page("https://www.linkedin.com/feed/")
            await self._session.check_rate_limit()
            for _ in range(25):
                if seen:
                    break
                await self._session.delay(1.0)
        finally:
            page.remove_listener("request", _capture)
        if not seen:
            raise LinkedInScraperException(
                "LinkedIn's feed sent no route prefetch to copy headers from, so "
                "the jobs tracker cannot be asked for. The page did not load, or "
                "LinkedIn changed how it loads routes."
            )
        headers = {
            name: value
            for name, value in seen[0].headers.items()
            if name.lower() not in _BROWSER_OWNED
            and not name.lower().startswith(("sec-", ":"))
        }
        _PREFETCH_HEADERS = (page, headers)
        return headers

    async def get_saved_jobs(
        self, max_pages: int = 3, stage: str = "saved"
    ) -> dict[str, Any]:
        """The jobs in one stage of the tracker. ``max_pages`` is upstream's
        argument and has nothing to page: a stage arrives in one answer."""
        from linkedin_mcp_server.core.exceptions import (
            AuthenticationError,
            RateLimitError,
        )
        from linkedin_mcp_server.voyager.profile_views import _POST_STREAM_JS

        if stage not in STAGES:
            raise LinkedInScraperException(
                f"stage was {stage!r}. Pass one of: {', '.join(STAGES)}."
            )
        answer = await self._session.page.evaluate(
            _POST_STREAM_JS,
            {
                "url": f"{_TRACKER_ROUTE}?stage={stage}",
                "headers": await self._prefetch_headers(),
                "body": json.dumps(
                    {
                        "requestedArguments": {
                            "payload": {"stage": stage},
                            "states": [],
                            "requestMetadata": {
                                "$type": "proto.sdui.common.RequestMetadata"
                            },
                            "screenId": "",
                            "knownTemplateIds": [],
                        },
                        "isPrefetch": True,
                    }
                ),
            },
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
        text = answer.get("text") or ""
        jobs = parse_tracker_jobs(text)
        if not jobs and _RECORD_START in text:
            raise LinkedInScraperException(
                f"Voyager {self.surface} changed shape: the tracker names jobs "
                "but none parsed. Refusing to report that as an empty stage."
            )
        if "opportunity-tracker" not in text:
            raise LinkedInScraperException(
                f"Voyager {self.surface} answered without the jobs tracker in "
                "it. Refusing to report that as an empty stage."
            )
        return {
            "url": f"{TRACKER_URL}?stage={stage}",
            "sections": {"saved_jobs": render_tracker(jobs)},
            "job_ids": [job["job_id"] for job in jobs],
            "jobs": jobs,
            "count": len(jobs),
            "stage": stage,
        }
