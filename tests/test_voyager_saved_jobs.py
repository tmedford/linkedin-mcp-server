"""``get_saved_jobs``: the jobs tracker, read without opening it."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.voyager import jobs as jobs_module
from linkedin_mcp_server.voyager.jobs import VoyagerSavedJobs, parse_tracker_jobs


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
    with pytest.raises(LinkedInScraperException, match="without the jobs tracker"):
        await _reader(_Page('0:["$","div",null,{}]')).get_saved_jobs()


async def test_jobs_named_but_not_parsed_are_refused():
    broken = '0:{"viewName":"opportunity-tracker-add-note","payload":{"jobId":"7","title":"moved"}}'

    with pytest.raises(LinkedInScraperException, match="changed shape"):
        await _reader(_Page(broken)).get_saved_jobs()


async def test_an_unknown_stage_is_refused_before_any_request():
    page = _Page(_stream([]))

    with pytest.raises(LinkedInScraperException, match="saved, draft"):
        await _reader(page).get_saved_jobs(stage="offers")

    assert page.sent == []


@pytest.fixture(autouse=True)
def _no_cached_headers():
    jobs_module.forget_prefetch_headers()
    yield
    jobs_module.forget_prefetch_headers()
