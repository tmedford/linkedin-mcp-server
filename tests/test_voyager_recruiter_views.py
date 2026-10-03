"""``get_recruiter_views``: which recruiters viewed the signed-in member's profile."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.voyager import profile_views as views_module
from linkedin_mcp_server.voyager.profile_views import (
    VoyagerRecruiterViews,
    parse_recruiter_rows,
)

NOW = datetime(2026, 10, 2, 21, 0, tzinfo=timezone.utc)
MARKER = {"viewName": "viewer-list-item"}


def _key(n: int) -> str:
    return json.dumps({"threadlineDecoration": None, "key": f"k{n}"})


def _layout(ref: str, url: str, company: str) -> list[Any]:
    """One rendering of a row: text by reference, logo, and a marked button."""
    return [
        "$",
        "div",
        None,
        {
            "children": [
                ["$", "img", None, {"a11yText": company}],
                ref,
                [
                    "$",
                    "$L4",
                    None,
                    {
                        "viewTrackingSpecs": MARKER,
                        "children": {
                            "buttonProps": {"text": ["View jobs"]},
                            "action": {"url": url},
                        },
                    },
                ],
            ]
        },
    ]


def _texts(*parts: str) -> list[Any]:
    return [
        "$",
        "div",
        None,
        {"children": [["$", "p", None, {"children": [p]}] for p in parts]},
    ]


def _stream(rows: list[tuple[str, list[str], str]], start: int = 0) -> str:
    """Rows of (company, rendered texts, link), as the pager renders them."""
    items: list[Any] = []
    lines: list[str] = []
    for index, (company, texts, url) in enumerate(rows):
        ref = f"{start + index + 16:x}"
        lines.append(f"{ref}:{json.dumps(_texts(*texts))}")
        items.append([_key(3 * index), ["$", "hr", None, {}]])
        items.append([_key(3 * index + 1), _layout(f"$L{ref}", url, company)])
        items.append([_key(3 * index + 2), _layout(f"$L{ref}", url, company)])
    # A length-prefixed text row with no newline after it, as LinkedIn sends.
    blob = "x" * 10
    root = "0:" + json.dumps(["$", "div", None, {"children": items}])
    return f"3:T{len(blob):x},{blob}" + root + "\n" + "\n".join(lines)


JOBS = "https://www.linkedin.com/jobs/search-results/?currentJobId=44&origin=RECRUITER_FLYWHEEL&f_C=1001&keywords=jobs"
INSIGHTS = "https://www.linkedin.com/company/2002/insights/"


def test_a_row_is_read_once_from_its_two_layouts():
    rows = parse_recruiter_rows(
        _stream(
            [
                (
                    "Acme",
                    [
                        "Recruiter at Acme",
                        "Viewed 1h ago",
                        "You'd be a top applicant for 6 roles",
                    ],
                    JOBS,
                ),
                (
                    "Globex",
                    ["Recruiter at Globex", "Staffing", "Viewed 2d ago"],
                    INSIGHTS,
                ),
            ]
        ),
        NOW,
    )

    assert rows == [
        {
            "description": "Recruiter at Acme",
            "company": "Acme",
            "company_id": "1001",
            "viewed_text": "Viewed 1h ago",
            "viewed_at_iso": "2026-10-02T20:00+00:00",
            "viewed_at_approximate": True,
            "insight": "You'd be a top applicant for 6 roles",
            "has_jobs": True,
            "jobs_url": JOBS,
            "job_id": "44",
        },
        {
            "description": "Recruiter at Globex",
            "company": "Globex",
            "company_id": "2002",
            "industry": "Staffing",
            "viewed_text": "Viewed 2d ago",
            "viewed_at_iso": "2026-09-30T21:00+00:00",
            "viewed_at_approximate": True,
            "has_jobs": False,
            "company_insights_url": INSIGHTS,
        },
    ]


def test_two_identical_looking_views_stay_two_rows():
    same = ("Acme", ["Recruiter at Acme", "Viewed 1h ago"], JOBS)

    assert len(parse_recruiter_rows(_stream([same, same]), NOW)) == 2


class _Page:
    def __init__(self, rows: list[tuple[str, list[str], str]], short_by: int = 0):
        self.rows = rows
        self.short_by = short_by
        self.bodies: list[dict[str, Any]] = []

    async def evaluate(self, _program: str, argument: Any = None) -> Any:
        body = json.loads(argument["body"])
        self.bodies.append(body)
        payload = body["clientArguments"]["payload"]
        start, count = payload["start"], payload["count"]
        # LinkedIn answered a window of 40 with 39: honour that here.
        chunk = self.rows[start : start + count - self.short_by]
        return {"status": 200, "text": _stream(chunk, start) if chunk else ""}


def _reader(page: _Page) -> VoyagerRecruiterViews:
    session = MagicMock()
    session.page = page
    session.delay = AsyncMock()
    reader = VoyagerRecruiterViews(session, MagicMock())
    setattr(reader, "_page_headers", AsyncMock(return_value={"x-li-track": "{}"}))
    return reader


def _many(n: int) -> list[tuple[str, list[str], str]]:
    return [
        (
            f"Co{i}",
            [f"Recruiter at Co{i}", f"Viewed {i + 1}d ago"],
            f"https://www.linkedin.com/company/{i + 1}/insights/",
        )
        for i in range(n)
    ]


async def test_a_short_window_is_not_taken_for_the_end():
    page = _Page(_many(50), short_by=1)

    result = await _reader(page).get_recruiter_views(days=365)

    # 39 from the first window, then the rest: the list is read past it.
    assert [b["clientArguments"]["payload"]["start"] for b in page.bodies] == [
        0,
        40,
        80,
    ]
    assert result["count"] == 49
    assert result["complete"] is True


async def test_the_period_and_seen_companies_are_sent_as_the_page_sends_them():
    rows = _many(41)
    rows[0] = ("Acme", ["Recruiter at Acme", "Viewed 1h ago"], JOBS)
    page = _Page(rows)

    await _reader(page).get_recruiter_views(days=7)

    first, second = page.bodies[0], page.bodies[1]
    assert first["pagerId"] == "com.linkedin.sdui.pagers.premium.wvmp.recruiterList"
    payload = second["clientArguments"]["payload"]
    assert payload["timeRange"] == "WvmpSearchFilterTimeRange_LAST_7_DAYS"
    assert payload["seenCompanyIds"][0] == "1001"
    assert payload["seenCompanyIdsWithJobs"] == ["1001"]
    assert first["clientArguments"]["payload"]["seenCompanyIds"] == []
    assert "seenCompanyIdsWithJobs" not in first["clientArguments"]["payload"]


async def test_marked_rows_that_do_not_parse_are_refused():
    class Broken(_Page):
        async def evaluate(self, _program: str, argument: Any = None) -> Any:
            return {
                "status": 200,
                "text": '0:["$","div",null,{"viewTrackingSpecs":{"viewName":"viewer-list-item"}}]',
            }

    with pytest.raises(LinkedInScraperException, match="changed shape"):
        await _reader(Broken([])).get_recruiter_views()


async def test_an_unknown_period_is_refused_before_any_request():
    page = _Page([])

    with pytest.raises(LinkedInScraperException, match="7, 14, 28, 90, 365"):
        await _reader(page).get_recruiter_views(days=30)

    assert page.bodies == []


@pytest.fixture(autouse=True)
def _no_cached_headers():
    views_module.forget_cached_headers()
    yield
    views_module.forget_cached_headers()


async def test_a_closing_rollup_is_reported_apart_from_the_recruiters():
    rows = _many(2) + [("", ["38 other recruiters"], "https://www.linkedin.com/x")]
    page = _Page(rows)

    result = await _reader(page).get_recruiter_views()

    assert result["aggregates"] == ["38 other recruiters"]
    assert [r["company"] for r in result["recruiters"]] == ["Co0", "Co1"]
    assert "38 other recruiters" in result["sections"]["recruiter_views"]


async def test_two_closing_rollups_are_both_kept():
    link = "https://www.linkedin.com/x"
    # The second rollup arrives alone, in the next window: the case where it
    # was lost, since a window with nothing new ends the read.
    rows = (
        _many(39)
        + [("", ["38 other recruiters"], link)]
        + [("", ["6 other recruiters"], link)]
    )

    result = await _reader(_Page(rows)).get_recruiter_views()

    assert result["aggregates"] == ["38 other recruiters", "6 other recruiters"]


async def test_the_same_looking_view_in_two_windows_is_two_views():
    rows = _many(39) + [("Acme", ["Recruiter at Acme", "Viewed 1h ago"], JOBS)] * 2

    result = await _reader(_Page(rows)).get_recruiter_views()

    assert sum(1 for r in result["recruiters"] if r["company"] == "Acme") == 2


async def test_a_window_that_repeats_the_last_one_ends_the_walk():
    class IgnoresStart(_Page):
        async def evaluate(self, _program: str, argument: Any = None) -> Any:
            self.bodies.append(json.loads(argument["body"]))
            return {"status": 200, "text": _stream(self.rows[:40])}

    page = IgnoresStart(_many(40))

    result = await _reader(page).get_recruiter_views()

    assert len(page.bodies) == 2
    assert result["count"] == 40
