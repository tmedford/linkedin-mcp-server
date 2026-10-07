"""``get_saved_jobs``: the jobs tracker, read without opening it."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    LinkedInOperationError,
    RateLimitError,
)
from linkedin_mcp_server.voyager import jobs as jobs_module
from linkedin_mcp_server.voyager import profile_views as views_module
from linkedin_mcp_server.voyager.jobs import VoyagerSavedJobs, parse_tracker_jobs


@pytest.fixture(autouse=True)
def _no_held_action_headers():
    # `_reader` holds the page's action headers; nothing outlives the test.
    yield
    setattr(views_module, "_HEADER_CACHE", None)


def _record(job_id: int, **extra: Any) -> dict[str, Any]:
    return {
        "jobId": str(job_id),
        "jobTitle": f"Director of Product {job_id} ",
        "companyName": "Acme",
        "locationPrimary": "New York, NY",
        "workplaceTypeName": "Hybrid",
        "listedAt": "1790250732000",
        "originallyListedAt": "1790249956000",
        "existingNote": "",
        "currentStageKey": "Saved",
        "isVerified": True,
        **extra,
    }


def _stream(records: list[dict[str, Any]]) -> str:
    """Rows as the tracker renders them: each job's record sits inside its
    note action, next to smaller payloads that carry only the job id."""
    rows = []
    for record in records:
        rows.append(
            json.dumps(
                {
                    "viewName": "opportunity-tracker-add-note",
                    "unsave": {"payload": {"jobId": record["jobId"]}},
                    "note": {"payload": record},
                    "again": {"payload": record},
                },
                # Compact, as LinkedIn's stream is: the reader keys on it.
                separators=(",", ":"),
            )
        )
    return (
        "0:" + "\n1:".join(rows)
        if rows
        else '0:{"viewName":"opportunity-tracker-not-seeing-jobs"}'
    )


def test_each_job_is_read_once_from_its_record():
    jobs = parse_tracker_jobs(
        _stream([_record(2), _record(1, existingNote="call Tue")])
    )

    assert [j["job_id"] for j in jobs] == ["2", "1"]
    assert jobs[0] == {
        "job_id": "2",
        "title": "Director of Product 2",
        "company": "Acme",
        "location": "New York, NY",
        "workplace": "Hybrid",
        "listed_at_iso": "2026-09-24T11:52+00:00",
        "original_listed_at_iso": "2026-09-24T11:39+00:00",
        "stage": "Saved",
        "verified": True,
        "url": "https://www.linkedin.com/jobs/view/2/",
    }
    assert jobs[1]["note"] == "call Tue"


class _Page:
    def __init__(self, text: str, status: int = 200):
        self.text, self.status = text, status
        self.sent: list[dict[str, Any]] = []

    async def evaluate(self, _program: str, argument: Any = None) -> Any:
        self.sent.append(argument)
        return {"status": self.status, "text": self.text}


def _reader(page: _Page) -> VoyagerSavedJobs:
    session = MagicMock()
    session.page = page
    session.delay = AsyncMock()
    reader = VoyagerSavedJobs(session, MagicMock())
    setattr(reader, "_prefetch_headers", AsyncMock(return_value={"x-li-track": "{}"}))
    # The contacts read uses the page's action headers, held here already.
    setattr(views_module, "_HEADER_CACHE", (page, {"x-li-track": "{}"}))
    return reader


async def test_a_stage_is_asked_for_by_prefetching_the_tracker_route():
    page = _Page(_stream([_record(7)]))

    result = await _reader(page).get_saved_jobs(stage="interview")

    sent = page.sent[0]
    assert (
        sent["url"]
        == "https://www.linkedin.com/flagship-web/jobs-tracker/?stage=interview"
    )
    body = json.loads(sent["body"])
    assert body["isPrefetch"] is True
    assert body["requestedArguments"]["payload"] == {"stage": "interview"}
    assert result["job_ids"] == ["7"] and result["stage"] == "interview"
    assert result["count"] == 1


async def test_an_empty_stage_is_an_empty_list():
    result = await _reader(_Page(_stream([]))).get_saved_jobs(stage="applied")

    assert result["jobs"] == [] and result["count"] == 0


async def test_an_answer_that_is_not_the_tracker_is_refused():
    with pytest.raises(LinkedInOperationError, match="without the jobs tracker"):
        await _reader(_Page('0:["$","div",null,{}]')).get_saved_jobs()


async def test_jobs_named_but_not_parsed_are_refused():
    broken = '0:{"viewName":"opportunity-tracker-add-note","payload":{"jobId":"7","title":"moved"}}'

    with pytest.raises(LinkedInOperationError, match="changed shape"):
        await _reader(_Page(broken)).get_saved_jobs()


async def test_an_unknown_stage_is_refused_before_any_request():
    page = _Page(_stream([]))

    with pytest.raises(LinkedInOperationError, match="saved, draft"):
        await _reader(page).get_saved_jobs(stage="offers")

    assert page.sent == []


@pytest.fixture(autouse=True)
def _no_cached_headers():
    jobs_module.forget_prefetch_headers()
    yield
    jobs_module.forget_prefetch_headers()


async def test_a_non_tracker_answer_naming_a_job_is_not_called_a_shape_change():
    # An error component can embed a job id without being the tracker.
    stray = '0:{"viewName":"error","payload":{"jobId":"7"}}'

    with pytest.raises(LinkedInOperationError, match="without the jobs tracker"):
        await _reader(_Page(stray)).get_saved_jobs()


_PILE = (
    '8:["$","$L14",null,{"children":[["$","$L16",null,{"sortableImages":'
    '[{"sortingKey":"$undefined","image":"$L17"},{"sortingKey":"$undefined","image":"$L18"}],'
    '"maxVisibleItems":2,"overflowCount":["+8"],"itemSize":24}]]}]'
)


def test_a_rows_image_pile_counts_its_faces_and_its_overflow():
    from linkedin_mcp_server.voyager.jobs import parse_contacts

    assert parse_contacts(_PILE) == 10
    assert parse_contacts(_PILE.replace(',"overflowCount":["+8"]', "")) == 2
    assert parse_contacts("7:null") == 0


async def test_each_saved_job_is_asked_for_its_network_contacts():
    page = _Page(_stream([_record(7), _record(8)]))
    answers = {"7": {"status": 200, "text": _PILE}, "8": {"status": 500, "text": ""}}
    tracker = page.evaluate

    async def evaluate(program: str, argument: Any = None) -> Any:
        if "opportunityContacts" in argument["url"]:
            page.sent.append(argument)
            job = json.loads(argument["body"])["clientArguments"]["payload"]["jobId"]
            return answers[job]
        return await tracker(program, argument)

    setattr(page, "evaluate", evaluate)

    jobs = (await _reader(page).get_saved_jobs())["jobs"]

    assert jobs[0]["network_contacts"] == 10
    # Unread is absent, not zero.
    assert "network_contacts" not in jobs[1]


async def test_action_headers_are_taken_once_and_their_failure_skips_every_job():
    page = _Page(_stream([_record(7), _record(8), _record(9)]))
    reader = _reader(page)
    setattr(views_module, "_HEADER_CACHE", None)
    taken = AsyncMock(side_effect=LinkedInOperationError("no action seen"))
    with patch.object(views_module.VoyagerProfileViews, "_page_headers", taken):
        jobs = (await reader.get_saved_jobs())["jobs"]

    assert taken.await_count == 1
    assert len(jobs) == 3 and not any("network_contacts" in job for job in jobs)
    # Only the tracker itself was asked for.
    assert len(page.sent) == 1


@pytest.mark.parametrize(
    ("status", "error"), [(403, AuthenticationError), (429, RateLimitError)]
)
async def test_a_rejected_or_limited_contacts_read_raises(status, error):
    page = _Page(_stream([_record(7), _record(8)]))
    tracker = page.evaluate

    async def evaluate(program: str, argument: Any = None) -> Any:
        if "opportunityContacts" in argument["url"]:
            page.sent.append(argument)
            return {"status": status, "text": ""}
        return await tracker(program, argument)

    setattr(page, "evaluate", evaluate)

    with pytest.raises(error):
        await _reader(page).get_saved_jobs()
    # It stopped at the first job rather than asking for the second.
    assert len(page.sent) == 2


async def test_a_company_filter_holding_no_id_is_refused():
    from linkedin_mcp_server.voyager.jobs import selected_filters

    for blank in (" ", ",", " , "):
        with pytest.raises(LinkedInOperationError, match="company_id was"):
            selected_filters(company_id=blank)


async def test_only_the_first_rows_are_asked_for_their_contacts(monkeypatch):
    monkeypatch.setattr(jobs_module, "CONTACTS_MAX", 2)
    page = _Page(_stream([_record(7), _record(8), _record(9)]))

    jobs = (await _reader(page).get_saved_jobs())["jobs"]

    # The tracker, then one read per job up to the cap; the third is unread.
    assert len(page.sent) == 3
    assert [("network_contacts" in job) for job in jobs] == [True, True, False]
