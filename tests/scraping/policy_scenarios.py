"""Canonical semantic scraping-policy scenarios."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from difflib import unified_diff
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import asyncio
import inspect
import json

from patchright.async_api import Page
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.callbacks import ProgressCallback
from linkedin_mcp_server.scraping import capture as capture_module
from linkedin_mcp_server.scraping import company as company_module
from linkedin_mcp_server.scraping import feed as feed_module
from linkedin_mcp_server.scraping import job_pages as job_pages_module
from linkedin_mcp_server.scraping import jobs as jobs_module
from linkedin_mcp_server.scraping import navigation as navigation_module
from linkedin_mcp_server.scraping import person as person_module
from linkedin_mcp_server.scraping import session as session_module
from linkedin_mcp_server.scraping import LinkedInExtractor
from linkedin_mcp_server.scraping.fields import COMPANY_SECTIONS, PERSON_SECTIONS
from linkedin_mcp_server.server import create_mcp_server
from linkedin_mcp_server.voyager import person as voyager_person
from linkedin_mcp_server.voyager import thread_reply

from .support.policy_trace import (
    FakeClock,
    ScriptedPage,
    ScriptedResponse,
    TraceRecorder,
    bind_effective,
)


ROOT = Path(__file__).parents[2]
TRACE_ROOT = ROOT / "tests" / "fixtures" / "scraping-policy" / "v1"
_TOOL_SCHEMAS: dict[str, dict[str, Any]] | None = None

_COMMON_ALLOWED = {
    "boundary.auth",
    "boundary.auth_quick",
    "boundary.drain",
    "boundary.modal",
    "boundary.rate_limit",
    "boundary.scroll_body",
    "boundary.scroll_sidebar",
    "boundary.stabilize",
    "boundary.trace",
    "callback.complete",
    "callback.progress",
    "callback.start",
    "evaluate",
    "evaluate_handle",
    "handle.as_element",
    "handle.dispose",
    "handle.evaluate",
    "keyboard.press",
    "keyboard.type",
    "listener.add",
    "listener.emit",
    "listener.remove",
    "locator.click",
    "locator.count",
    "locator.by_role",
    "locator.create",
    "locator.derive",
    "locator.is_visible",
    "locator.scroll_into_view",
    "locator.wait_for",
    "mouse.move",
    "mouse.wheel",
    "navigate",
    "sleep",
    "wait_for_function",
    "wait_for_load_state",
    "wait_for_selector",
}


class TraceCallbacks(ProgressCallback):
    """Record progress callbacks without a mock object."""

    def __init__(self, recorder: TraceRecorder):
        self.recorder = recorder

    async def on_start(self, scraper_type: str, url: str) -> None:
        self.recorder.record("callback.start", operation=scraper_type, url=url)

    async def on_progress(self, message: str, percent: int) -> None:
        self.recorder.record("callback.progress", message=message, percent=percent)

    async def on_complete(self, scraper_type: str, result: Any) -> None:
        self.recorder.record(
            "callback.complete", operation=scraper_type, result_url=result["url"]
        )


@contextmanager
def _diagnostics_bindings(diagnostics: Any) -> Iterator[None]:
    """Bind the issue-report boundary in each exercised scraping module.

    A separate context manager rather than more items in `boundaries`, which
    sat on exactly 20 and is the whole of CPython's static block budget inside
    an async generator: the twenty-first raised `SyntaxError: too many
    statically nested blocks` at import, before any test ran. Each module
    holds its own name for the function, so patching fewer than all of them
    lets the real one write an issue report for a scripted page.
    """

    with (
        patch.object(capture_module, "build_issue_diagnostics", diagnostics),
        patch.object(feed_module, "build_issue_diagnostics", diagnostics),
        patch.object(person_module, "build_issue_diagnostics", diagnostics),
        patch.object(company_module, "build_issue_diagnostics", diagnostics),
        patch.object(job_pages_module, "build_issue_diagnostics", diagnostics),
        patch.object(jobs_module, "build_issue_diagnostics", diagnostics),
    ):
        yield


@asynccontextmanager
async def boundaries(
    recorder: TraceRecorder,
    clock: FakeClock,
    *,
    auth_result: str | None = None,
) -> AsyncIterator[None]:
    real_scroll_body = session_module.scroll_to_bottom
    real_scroll_sidebar = session_module.scroll_job_sidebar
    real_drain = feed_module.FeedScraper._drain_listener_tasks

    async def trace(_page: Any, label: str, *, extra: Any = None) -> None:
        recorder.record("boundary.trace", label=label, extra=extra)

    async def auth_quick(_page: Any) -> None:
        recorder.record("boundary.auth_quick", result=None)
        return None

    async def auth(_page: Any) -> str | None:
        recorder.record("boundary.auth", result=auth_result)
        return auth_result

    async def remember(_page: Any) -> bool:
        return False

    async def stabilize(description: str, _logger: Any) -> None:
        recorder.record(
            "boundary.stabilize",
            description=description,
            result=None,
        )

    async def rate_limit(_page: Any) -> None:
        recorder.record("boundary.rate_limit")

    async def modal(_page: Any) -> bool:
        recorder.record("boundary.modal", dismissed=False)
        return False

    async def scroll_body(*args: Any, **kwargs: Any) -> None:
        values = bind_effective(real_scroll_body, *args, **kwargs)
        values.pop("page")
        recorder.record("boundary.scroll_body", **values, actual_scrolls=2)
        clock.advance(values["pause_time"] * 2)

    async def scroll_sidebar(*args: Any, **kwargs: Any) -> bool:
        values = bind_effective(real_scroll_sidebar, *args, **kwargs)
        values.pop("page")
        recorder.record(
            "boundary.scroll_sidebar", **values, actual_scrolls=2, moved=False
        )
        clock.advance(0.4)
        return False

    async def drain(tasks: list[Any]) -> None:
        # The real drain is what runs; the event only marks where it happens,
        # so swapping it with listener removal shows up as a reordered trace.
        recorder.record("boundary.drain", pending=len(tasks))
        await real_drain(tasks)

    def diagnostics(error: Exception, **values: Any) -> dict[str, Any]:
        return {
            "error_type": type(error).__name__,
            "error_message": str(error),
            "context": values.get("context"),
        }

    with (
        patch.object(navigation_module, "record_page_trace", trace),
        patch.object(navigation_module, "detect_auth_barrier_quick", auth_quick),
        patch.object(navigation_module, "detect_auth_barrier", auth),
        patch.object(navigation_module, "resolve_remember_me_prompt", remember),
        patch.object(navigation_module, "stabilize_navigation", stabilize),
        # Every binding of each shared boundary, because the workflows that
        # reach it are split across the modules mid-relocation: generic
        # capture, the feed, conversation reader and message sender go through
        # `ScrapingSession`, while the job pages import the helper into
        # `job_pages`. Patching one side only lets the real helper loose on a
        # scripted page. The scrolls have no facade binding left at all — the job
        # reader held the last one — and neither has the modal close, whose
        # last facade caller left with the conversation reader.
        patch.object(session_module, "detect_rate_limit", rate_limit),
        patch.object(job_pages_module, "detect_rate_limit", rate_limit),
        patch.object(session_module, "handle_modal_close", modal),
        patch.object(job_pages_module, "handle_modal_close", modal),
        patch.object(session_module, "scroll_to_bottom", scroll_body),
        patch.object(job_pages_module, "scroll_to_bottom", scroll_body),
        patch.object(session_module, "scroll_job_sidebar", scroll_sidebar),
        patch.object(job_pages_module, "scroll_job_sidebar", scroll_sidebar),
        _diagnostics_bindings(diagnostics),
        # `staticmethod`, or the class attribute would bind `self` in front of
        # the pending list and the replacement would never match the call.
        patch.object(
            feed_module.FeedScraper,
            "_drain_listener_tasks",
            staticmethod(drain),
        ),
        patch.object(session_module.asyncio, "sleep", clock.sleep),
        patch.object(session_module.time, "monotonic", clock.monotonic),
    ):
        yield


def _page(recorder: TraceRecorder, *, url: str = "about:blank") -> ScriptedPage:
    return ScriptedPage(recorder, url=url).script("evaluate:root_content")


def _extractor(page: ScriptedPage) -> LinkedInExtractor:
    return LinkedInExtractor(cast(Page, page))


def _complete_mapping_result(result: dict[str, Any], **derived: Any) -> dict[str, Any]:
    overlap = result.keys() & derived.keys()
    if overlap:
        raise AssertionError(
            f"derived result fields overlap raw result: {sorted(overlap)}"
        )
    return {**result, **derived}


def _root(text: str, references: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "source": "root",
        "text": text,
        "references": references or [],
    }


async def _generic_capture_scenario(
    name: str, url: str, *, max_scrolls: int | None = None
) -> dict[str, Any]:
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder).script("evaluate:root_content", _root("Policy content"))
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("extract_page", "section"):
            result = await extractor.extract_page(
                url, "section", max_scrolls=max_scrolls
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "extract_page",
            "arguments": {
                "url": url,
                "section_name": "section",
                "max_scrolls": max_scrolls,
            },
        },
        {"text": result.text, "references": result.references},
    )


async def _person_sections_scenario() -> dict[str, Any]:
    name = "scrape_person__all_sections"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    # 13 valid company anchors against the documented cap of 12 for a section,
    # so dropping the cap shows up as a fourteenth reference in the trace.
    overflowing = [
        {
            "href": f"https://www.linkedin.com/company/policy-employer-{index}/",
            "text": f"Employer {index}",
        }
        for index in range(13)
    ]
    roots = [
        _root(
            f"{section} content",
            overflowing if section == "experience" else None,
        )
        for section in PERSON_SECTIONS
    ]
    page.script("evaluate:root_content", *roots)
    _script_profile_target(page)
    page.declare_locator("main button", "show_more")
    page.declare_derived(
        "show_more",
        "filter:^Show (more|all)\\b/re.IGNORECASE|re.UNICODE",
        "show_more.filtered",
    )
    page.script("show_more.filtered.count", *([0] * 8))
    extractor = _extractor(page)
    callbacks = TraceCallbacks(recorder)
    async with boundaries(recorder, clock):
        with recorder.context("scrape_person"):
            result = await extractor.scrape_person(
                "ada-lovelace", set(PERSON_SECTIONS), callbacks=callbacks
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "scrape_person",
            "arguments": {
                "username": "ada-lovelace",
                "requested": list(PERSON_SECTIONS),
            },
        },
        _complete_mapping_result(result, section_names=list(result["sections"])),
    )


async def _company_sections_scenario() -> dict[str, Any]:
    name = "scrape_company__all_sections"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder).script(
        "evaluate:root_content",
        *[_root(f"{section} content") for section in COMPANY_SECTIONS],
    )
    extractor = _extractor(page)
    callbacks = TraceCallbacks(recorder)
    async with boundaries(recorder, clock):
        with recorder.context("scrape_company"):
            result = await extractor.scrape_company(
                "analytical-engine", set(COMPANY_SECTIONS), callbacks=callbacks
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "scrape_company",
            "arguments": {
                "company_name": "analytical-engine",
                "requested": list(COMPANY_SECTIONS),
            },
        },
        _complete_mapping_result(result, section_names=list(result["sections"])),
    )


async def _job_search_scenario(route: str = "/jobs/search/") -> dict[str, Any]:
    is_alias = "search-results" in route
    name = "search_jobs__route_alias" if is_alias else "search_jobs__baseline"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    page.goto_landings.append(f"https://www.linkedin.com{route}?keywords=python")
    ids = ["101", "102"] if is_alias else [str(101 + index) for index in range(16)]
    references = [
        {
            "href": "https://www.linkedin.com/jobs/view/101/",
            "text": "Senior policy engineer",
            "heading": "",
        },
        *[
            {
                "href": f"https://www.linkedin.com/company/company-{index}/",
                "text": f"Company {index}",
                "heading": "",
            }
            for index in range(20)
        ],
    ]
    page.script("evaluate:root_content", _root("Python jobs", references))
    page.script("evaluate:job_total_pages", None)
    page.script("evaluate:job_ids", {"ids": ids, "scoped": True})
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("search_jobs", "search_results"):
            result = await extractor.search_jobs("python", max_pages=1)
    page.assert_clean()
    return recorder.trace(
        {"method": "search_jobs", "arguments": {"keywords": "python", "max_pages": 1}},
        result,
    )


async def _job_search_upgrade_scenario() -> dict[str, Any]:
    recorder = TraceRecorder(
        "search_jobs__stopping_page_metadata_upgrade", _COMMON_ALLOWED
    )
    clock = FakeClock(recorder)
    page = _page(recorder)
    first = [
        {
            "href": "https://www.linkedin.com/jobs/view/101/",
            "text": "Job",
            "heading": "",
        }
    ]
    second = [
        {
            "href": "https://www.linkedin.com/jobs/view/101/",
            "text": "Senior policy engineer with richer stopping-page metadata",
            "heading": "",
        }
    ]
    page.script(
        "evaluate:root_content",
        _root("First page", first),
        _root("Stopping page", second),
    )
    page.script("evaluate:job_total_pages", None)
    page.script(
        "evaluate:job_ids",
        {"ids": ["101"], "scoped": True},
        {"ids": ["101"], "scoped": True},
    )
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("search_jobs", "search_results"):
            result = await extractor.search_jobs("python", max_pages=2)
    page.assert_clean()
    return recorder.trace(
        {"method": "search_jobs", "arguments": {"keywords": "python", "max_pages": 2}},
        result,
    )


async def _saved_jobs_scenario() -> dict[str, Any]:
    name = "get_saved_jobs__redirect_caps_and_upgrade"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    page.goto_landings.append("https://www.linkedin.com/jobs-tracker/")
    first_references = [
        *[
            {
                "href": f"https://www.linkedin.com/jobs/view/{100 + index}/",
                "text": f"Job {100 + index}",
                "heading": "",
            }
            for index in range(12)
        ],
        {
            "href": "https://www.linkedin.com/company/cap-boundary/",
            "text": "Cap boundary company",
            "heading": "",
        },
        *[
            {
                "href": f"https://www.linkedin.com/jobs/view/{113 + index}/",
                "text": f"Job {113 + index}",
                "heading": "",
            }
            for index in range(7)
        ],
    ]
    second_references = [
        {
            "href": "https://www.linkedin.com/jobs/view/100/",
            "text": "Senior policy engineer with richer duplicate metadata",
            "heading": "",
        },
        *[
            {
                "href": f"https://www.linkedin.com/jobs/view/{112 + index}/",
                "text": f"Job {112 + index}",
                "heading": "",
            }
            for index in range(19)
        ],
    ]
    page.script(
        "evaluate:root_content",
        _root("Saved jobs page one", first_references),
        _root("Saved jobs page two", second_references),
    )
    page.script("evaluate:saved_job_total_pages", 2)
    page.script(
        "evaluate:job_ids",
        {"ids": [str(100 + index) for index in range(10)], "scoped": False},
        {"ids": [str(110 + index) for index in range(10)], "scoped": False},
    )
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("get_saved_jobs", "saved_jobs"):
            result = await extractor.get_saved_jobs(max_pages=2)
    page.assert_clean()
    references = result.get("references", {}).get("saved_jobs", [])
    return recorder.trace(
        {"method": "get_saved_jobs", "arguments": {"max_pages": 2}},
        _complete_mapping_result(result, reference_count=len(references)),
    )


async def _feed_stale_scenario() -> dict[str, Any]:
    name = "extract_feed__stale_stop"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder).script("evaluate:root_content", _root("Feed content"))
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("extract_feed", "feed"):
            result = await extractor.extract_feed(num_posts=10)
    page.assert_clean()
    return recorder.trace(
        {"method": "extract_feed", "arguments": {"num_posts": 10}},
        {"references": result.references, "text": result.text},
    )


async def _feed_response_scenario(*, body_failure: bool) -> dict[str, Any]:
    suffix = "body_failure" if body_failure else "body_success"
    recorder = TraceRecorder(
        f"extract_feed__{suffix}",
        _COMMON_ALLOWED | {"response.body.start", "response.body.finish"},
    )
    clock = FakeClock(recorder)
    page = _page(recorder).script("evaluate:root_content", _root("Feed content"))
    body: bytes | BaseException
    if body_failure:
        body = RuntimeError("response body unavailable")
    else:
        body = (
            b'{"postSlugUrl":"https://www.linkedin.com/posts/'
            b'policy-ugcPost-123-example"}'
        )
    response = ScriptedResponse(
        recorder,
        "https://www.linkedin.com/feed/",
        body,
    )
    page.script("mouse.wheel", lambda: page.emit("response", response))
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("extract_feed", "feed"):
            result = await extractor.extract_feed(num_posts=1)
    page.assert_clean()
    return recorder.trace(
        {"method": "extract_feed", "arguments": {"num_posts": 1}},
        {"references": result.references, "text": result.text},
    )


_MESSAGE_PROFILE_URL = "https://www.linkedin.com/in/ada-lovelace/"
_MESSAGE_COMPOSE_URL = (
    "https://www.linkedin.com/messaging/compose/"
    "?recipient=ACoAA-policy&profileUrn=urn%3Ali%3Afsd_profile%3AACoAA-policy"
)
_MESSAGE_ROUTE = "https://www.linkedin.com/messaging/thread/2-policy-thread==/"
_MESSAGE_TARGET = {
    "profilePath": "/in/ada-lovelace/",
    "profileUrn": "ACoAA-policy",
}
_VALID_COMPOSER = {
    "status": "valid",
    "active": False,
    "empty": True,
    "submitCount": 1,
    "submitUsable": True,
}


def _profile_target(
    status: str = "resolved", *, profile_urn: str = "ACoAA-policy"
) -> dict[str, Any]:
    if status == "resolved":
        compose_url = (
            _MESSAGE_COMPOSE_URL
            if profile_urn == "ACoAA-policy"
            else "https://www.linkedin.com/messaging/compose/?recipient=" + profile_urn
        )
        return {
            "status": "resolved",
            "pageUrl": _MESSAGE_PROFILE_URL,
            "displayName": "Ada Lovelace",
            "composeHrefs": [compose_url],
        }
    if status == "unavailable":
        return {"status": "unavailable", "pageUrl": _MESSAGE_PROFILE_URL}
    return {"status": "unresolved"}


def _script_profile_target(
    page: ScriptedPage,
    status: str = "resolved",
    *,
    profile_urn: str = "ACoAA-policy",
) -> None:
    page.script(
        "wait_for_function:profile_message_target_ready",
        None
        if status == "resolved"
        else PlaywrightTimeoutError("profile Message action did not resolve"),
    )
    page.script(
        "evaluate:profile_message_target",
        _profile_target(status, profile_urn=profile_urn),
    )


def _script_message_surface(
    page: ScriptedPage,
    *,
    states: tuple[dict[str, Any], ...],
    route: str = _MESSAGE_ROUTE,
) -> None:
    page.goto_landings.append(_MESSAGE_PROFILE_URL)
    page.goto_landings.append(route)
    _script_profile_target(page)
    page.script("wait_for_function:message_composer_ready", None)
    page.script("evaluate:message_composer_state", *states)


def _script_message_owner(page: ScriptedPage, *, write: str = "written") -> None:
    page.script("evaluate_handle:message_composer_owner", True)
    page.script("handle-1.evaluate:message_composer_write", write)
    page.script("handle-1.evaluate:message_composer_dispose", None)


def _script_confirmation_cleanup(page: ScriptedPage) -> None:
    page.script("evaluate:message_confirmation_dispose", None)


def _script_owned_text_cleanup(page: ScriptedPage, *, removed: bool) -> None:
    page.script("handle-1.evaluate:message_composer_cleanup", removed)


async def _message_target_scenario(status: str) -> dict[str, Any]:
    recorder = TraceRecorder(f"send_message__target_{status}", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    page.goto_landings.append(_MESSAGE_PROFILE_URL)
    _script_profile_target(page, status)
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("send_message", "message"):
            result = await extractor.send_message(
                "ada-lovelace",
                "New text",
                confirm_send=True,
                profile_urn="ACoAA-policy",
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "send_message",
            "arguments": {"target_resolution": status, "confirm_send": True},
        },
        result,
    )


async def _messaging_dry_run_scenario() -> dict[str, Any]:
    recorder = TraceRecorder("send_message__dry_run", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    _script_message_surface(page, states=(_VALID_COMPOSER,))
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("send_message", "message"):
            result = await extractor.send_message(
                "ada-lovelace",
                "New text",
                confirm_send=False,
                profile_urn="ACoAA-policy",
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "send_message",
            "arguments": {
                "linkedin_username": "ada-lovelace",
                "message": "New text",
                "confirm_send": False,
                "profile_urn": "ACoAA-policy",
            },
        },
        result,
    )


async def _occupied_message_scenario(*, restored_during_write: bool) -> dict[str, Any]:
    suffix = "restored_during_write" if restored_during_write else "existing_draft"
    recorder = TraceRecorder(f"send_message__{suffix}", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    second_state = (
        _VALID_COMPOSER
        if restored_during_write
        else {**_VALID_COMPOSER, "empty": False}
    )
    _script_message_surface(page, states=(_VALID_COMPOSER, second_state))
    if restored_during_write:
        _script_message_owner(page, write="occupied")
        _script_owned_text_cleanup(page, removed=False)
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("send_message", "message"):
            result = await extractor.send_message(
                "ada-lovelace", "New text", confirm_send=True
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "send_message",
            "arguments": {
                "confirm_send": True,
                "composer_occupied": suffix,
            },
        },
        result,
    )


async def _messaging_submission_scenario(outcome: str) -> dict[str, Any]:
    recorder = TraceRecorder(f"send_message__{outcome}", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    _script_message_surface(page, states=(_VALID_COMPOSER, _VALID_COMPOSER))
    _script_message_owner(page)

    if outcome == "pre_submit_cleanup":
        page.script("handle-1.evaluate:message_submit_ready", "invalid")
        _script_owned_text_cleanup(page, removed=True)
    else:
        ready_states = ("disabled", "ready") if outcome == "sent" else ("ready",)
        page.script("handle-1.evaluate:message_submit_ready", *ready_states)
        page.script("evaluate:message_confirmation_prepare", "confirmation-1")
        if outcome == "submission_rejected":
            page.script("handle-1.evaluate:message_submit", "invalid")
            _script_confirmation_cleanup(page)
            _script_owned_text_cleanup(page, removed=True)
        elif outcome == "submission_interrupted":
            page.script(
                "handle-1.evaluate:message_submit",
                RuntimeError("submission round trip interrupted"),
            )
            _script_confirmation_cleanup(page)
        else:
            page.script("handle-1.evaluate:message_submit", "clicked")
            page.script(
                "wait_for_function:message_confirmation_ready",
                None
                if outcome == "sent"
                else PlaywrightTimeoutError("same-node transition not observed"),
            )
            _script_confirmation_cleanup(page)

    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("send_message", "message"):
            result = await extractor.send_message(
                "ada-lovelace", "New text", confirm_send=True
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "send_message",
            "arguments": {"confirm_send": True, "submission_outcome": outcome},
        },
        result,
    )


async def _messaging_cancellation_scenario() -> dict[str, Any]:
    recorder = TraceRecorder("send_message__confirmation_cancelled", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    _script_message_surface(page, states=(_VALID_COMPOSER, _VALID_COMPOSER))
    _script_message_owner(page)
    page.script("handle-1.evaluate:message_submit_ready", "ready")
    page.script("evaluate:message_confirmation_prepare", "confirmation-1")
    page.script("handle-1.evaluate:message_submit", "clicked")
    page.script(
        "wait_for_function:message_confirmation_ready", asyncio.CancelledError()
    )
    _script_confirmation_cleanup(page)
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("send_message", "message"):
            try:
                await extractor.send_message(
                    "ada-lovelace", "New text", confirm_send=True
                )
            except asyncio.CancelledError:
                result = {"raised": "CancelledError"}
            else:
                raise AssertionError("message confirmation cancellation was swallowed")
    page.assert_clean()
    return recorder.trace(
        {
            "method": "send_message",
            "arguments": {"confirm_send": True, "cancelled_during": "confirmation"},
        },
        result,
    )


async def _invalid_message_scenario(message: str, label: str) -> dict[str, Any]:
    recorder = TraceRecorder(f"send_message__invalid_{label}", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("send_message", "message"):
            result = await extractor.send_message(
                "ada-lovelace",
                message,
                confirm_send=True,
                profile_urn="ACoAA-policy",
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "send_message",
            "arguments": {
                "linkedin_username": "ada-lovelace",
                "message_case": label,
                "confirm_send": True,
            },
        },
        result,
    )


async def _single_capture_facade_scenario(method: str) -> dict[str, Any]:
    name = f"{method}__baseline"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder).script("evaluate:root_content", _root("Result content"))
    extractor = _extractor(page)
    arguments: dict[str, Any]
    async with boundaries(recorder, clock):
        with recorder.context(method):
            if method == "get_company_employees":
                arguments = {"company_name": "analytical-engine", "keywords": "math"}
                result = await extractor.get_company_employees(**arguments)
            elif method == "scrape_job":
                arguments = {"job_id": "123"}
                result = await extractor.scrape_job(**arguments)
            elif method == "search_people":
                arguments = {"keywords": "analyst", "network": ["F"]}
                result = await extractor.search_people(**arguments)
            elif method == "search_companies":
                arguments = {"keywords": "engine"}
                result = await extractor.search_companies(**arguments)
            elif method == "search_posts":
                arguments = {"keywords": "mathematics", "max_pages": 2}
                result = await extractor.search_posts(**arguments)
            else:
                raise AssertionError(method)
    page.assert_clean()
    return recorder.trace(
        {"method": method, "arguments": arguments},
        _complete_mapping_result(result, section_names=list(result["sections"])),
    )


async def _single_capture_error_scenario() -> dict[str, Any]:
    recorder = TraceRecorder("scrape_job__capture_error", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder).script(
        "evaluate:root_content", RuntimeError("synthetic capture failure")
    )
    extractor = _extractor(page)
    arguments = {"job_id": "123"}
    async with boundaries(recorder, clock):
        with recorder.context("scrape_job"):
            result = await extractor.scrape_job(**arguments)
    page.assert_clean()
    return recorder.trace(
        {"method": "scrape_job", "arguments": arguments},
        _complete_mapping_result(result, section_names=list(result["sections"])),
    )


async def _get_my_profile_scenario() -> dict[str, Any]:
    name = "get_my_profile__baseline"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    page.goto_landings.append("https://www.linkedin.com/in/ada-lovelace/")
    page.script("evaluate:root_content", _root("Own profile"))
    _script_profile_target(page, profile_urn="ACoAA-self")
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("get_my_profile", "main_profile"):
            result = await extractor.get_my_profile()
    page.assert_clean()
    return recorder.trace(
        {"method": "get_my_profile", "arguments": {}},
        result,
    )


async def _connect_scenario() -> dict[str, Any]:
    name = "connect_with_person__self_profile"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    page.script("evaluate:root_content", _root("Own profile"))
    _script_profile_target(page, "unavailable")
    page.script(
        "evaluate:connection_action_signals",
        {
            "hasInvite": False,
            "hasComposeInActionRoot": False,
            "hasEditIntro": True,
            "hasLabeledActionButton": True,
            "hasLabeledActionAnchor": False,
            "hasIncomingActionRow": False,
        },
    )
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("connect_with_person", "main_profile"):
            result = await extractor.connect_with_person("ada-lovelace")
    page.assert_clean()
    return recorder.trace(
        {"method": "connect_with_person", "arguments": {"username": "ada-lovelace"}},
        result,
    )


async def _sidebar_scenario() -> dict[str, Any]:
    name = "get_sidebar_profiles__baseline"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder).script(
        "evaluate:sidebar_profiles", {"sections": {}, "showAllUrls": {}}
    )
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("get_sidebar_profiles", "sidebar"):
            result = await extractor.get_sidebar_profiles("ada-lovelace")
    page.assert_clean()
    return recorder.trace(
        {"method": "get_sidebar_profiles", "arguments": {"username": "ada-lovelace"}},
        result,
    )


async def _conversations_page_scenario() -> dict[str, Any]:
    """Record what `get_conversations` does to the page.

    The point of the trace is the side-effect profile, not the payload: this
    tool's whole claim is that it reads the mailbox WITHOUT clicking
    conversation rows, and therefore without marking anything read. The events
    below are that claim in machine-checkable form. One click is recorded, on
    the paging control, and no conversation row is ever touched.
    """
    name = "get_conversations__baseline"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)

    base = (
        "https://www.linkedin.com/voyager/api/voyagerMessagingGraphQL/graphql"
        "?queryId=messengerConversations.deadbeef&variables="
        "(query:(predicateUnions:List((conversationCategoryPredicate:"
        "(category:PRIMARY_INBOX)))),count:20,"
        "mailboxUrn:urn:li:fsd_profile:ACoAAme"
    )
    page_load_url = f"{base})"
    cursored_url = f"{base},nextCursor:SEED)"

    # The sidebar mounts, then the paging control is found by role and clicked
    # once. Clicking it is what makes LinkedIn issue the cursor-bearing query
    # that discovery needs; no row is clicked.
    page.script("wait_for_selector:conversation_rows", None)
    page.script("evaluate:scroll_main_region", True)
    page.declare_role("button", "Load more conversations", "load-more")
    page.declare_derived("load-more", "first", "load-more-first")
    page.script("load-more.count", 1)

    def _click_emits_the_paging_query() -> None:
        """Clicking the control is what makes LinkedIn issue both queries.

        Scripted as a callable so the emission happens at the moment of the
        click, which is the real ordering: discovery cannot observe a request
        that has not been made yet.
        """
        page.emit("request", _ScriptedRequest(page_load_url))
        page.emit("request", _ScriptedRequest(cursored_url))

    page.script("load-more-first.click", _click_emits_the_paging_query)

    conversation = {
        "$type": "com.linkedin.messenger.Conversation",
        "entityUrn": "urn:li:msg_conversation:1",
        "conversationUrl": "/messaging/thread/2-abc/",
        "lastActivityAt": 1_700_000_000_000,
        "unreadCount": 0,
        "categories": ["INBOX"],
        "*conversationParticipants": [],
    }
    payload = {
        "data": {
            "data": {
                "messengerConversationsByCategoryQuery": {
                    "metadata": {},
                    "*elements": ["urn:li:msg_conversation:1"],
                }
            }
        },
        "included": [conversation],
    }
    page.script(
        "evaluate:voyager_conversations_fetch",
        {"body": json.dumps(payload)},
    )

    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("get_conversations", "conversation"):
            arguments = {}
            result = await extractor.get_conversations()
    page.assert_clean()
    return recorder.trace(
        {"method": "get_conversations", "arguments": arguments},
        result,
    )


async def _invitations_scenario() -> dict[str, Any]:
    """Record what `get_invitations` does to the page.

    The claim being pinned is the side-effect profile, same as the
    conversations walk: reading the invitation board must not navigate, must
    not click, and must not accept, ignore or withdraw anything. One evaluate,
    nothing else. The alternative this replaces drives a browser to the
    invitation manager and scrolls a lazy list until it settles.

    The evaluate is classified as ``voyager_conversations_fetch`` because the
    authenticated fetch is now shared by every reader in the package and the
    classifier keys on its csrf-token marker. The name is inherited rather than
    accurate; renaming it would edit a line upstream owns for no behavioural
    gain.
    """
    name = "get_invitations__baseline"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)

    payload = {
        "data": {
            "data": {
                "*elements": [
                    {
                        "invitation": {
                            "entityUrn": "urn:li:invitation:1",
                            "invitationType": "CONNECTION",
                            "invitationState": "PENDING",
                            "sentTime": 1_700_000_000_000,
                            "sharedSecret": "s3cret",
                            "customMessage": True,
                            "message": "Happy to connect",
                        },
                        "fromMember": {
                            "entityUrn": "urn:li:member:1",
                            "firstName": "Ada",
                            "lastName": "Lovelace",
                            "occupation": "Engineer",
                            "publicIdentifier": "ada-lovelace",
                        },
                    }
                ]
            }
        },
        "included": [],
    }
    page.script(
        "evaluate:voyager_conversations_fetch",
        {"body": json.dumps(payload)},
    )

    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("get_invitations", "invitations"):
            arguments: dict[str, Any] = {}
            result = await extractor.get_invitations()
    page.assert_clean()
    return recorder.trace(
        {"method": "get_invitations", "arguments": arguments},
        result,
    )


async def _thread_reply_scenario(outcome: str) -> dict[str, Any]:
    """Record what `reply_to_thread` does to the page.

    The claim being pinned is the side-effect profile. A reply never
    navigates, never clicks and never types: a dry run is two reads and no
    write, and a confirmed reply is one read and one write. The alternative
    this replaces opened the thread, which marks it read, and typed into its
    composer.

    Every evaluate is classified as ``voyager_conversations_fetch`` for the
    reason given on the invitations scenario: the classifier keys on the
    csrf-token marker the shared client carries.
    """
    recorder = TraceRecorder(f"reply_to_thread__{outcome}", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    confirm_send = outcome != "dry_run"
    me = {
        "body": json.dumps(
            {"included": [{"dashEntityUrn": "urn:li:fsd_profile:ACoAA-me"}]}
        )
    }
    conversation = (
        "urn:li:msg_conversation:(urn:li:fsd_profile:ACoAA-me,2-policy-thread==)"
    )
    if outcome == "dry_run":
        thread = {
            "included": [
                {
                    "$type": "com.linkedin.messenger.Message",
                    "deliveredAt": 1_700_000_000_000,
                    "body": {"text": "Earlier message"},
                }
            ]
        }
        page.script(
            "evaluate:voyager_conversations_fetch",
            me,
            {"body": json.dumps(thread)},
        )
    elif outcome == "sent":
        created = {
            "value": {
                "entityUrn": "urn:li:msg_message:(urn:li:fsd_profile:ACoAA-me,2-new)",
                "conversationUrn": conversation,
                "deliveredAt": 1_700_000_100_000,
            }
        }
        page.script(
            "evaluate:voyager_conversations_fetch",
            me,
            {"status": 200, "body": json.dumps(created)},
        )
    else:
        page.script(
            "evaluate:voyager_conversations_fetch",
            me,
            {"status": 400, "body": '{"status":400}'},
        )

    extractor = _extractor(page)
    # The two per-send tokens are random by design, and a trace has to be
    # reproducible, so they are pinned for the recording only.
    with (
        patch.object(thread_reply, "_origin_token", return_value="policy-origin-token"),
        patch.object(thread_reply, "_tracking_id", return_value="policy-tracking-id"),
    ):
        async with boundaries(recorder, clock):
            with recorder.context("reply_to_thread", "message"):
                result = await extractor.reply_to_thread(
                    _MESSAGE_ROUTE, "New text", confirm_send=confirm_send
                )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "reply_to_thread",
            "arguments": {"confirm_send": confirm_send, "outcome": outcome},
        },
        result,
    )


async def _thread_scenario() -> dict[str, Any]:
    """Record what `get_thread` does to the page.

    Two evaluates and nothing else: who is signed in, then the thread. No
    navigation, which is the claim, because navigating to a thread is what
    marks it read.
    """
    name = "get_thread__baseline"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    me = "urn:li:fsd_profile:ACoAA-me"
    other = "urn:li:msg_messagingParticipant:urn:li:fsd_profile:ACoAA-ada"
    payload = {
        "data": {
            "data": {"messengerMessagesBySyncToken": {"*elements": ["urn:li:msg:1"]}}
        },
        "included": [
            {
                "$type": "com.linkedin.messenger.MessagingParticipant",
                "entityUrn": other,
                "hostIdentityUrn": "urn:li:fsd_profile:ACoAA-ada",
                "participantType": {
                    "member": {
                        "firstName": {"text": "Ada"},
                        "lastName": {"text": "Lovelace"},
                        "profileUrl": "https://www.linkedin.com/in/ada-lovelace",
                    }
                },
            },
            {
                "$type": "com.linkedin.messenger.Message",
                "entityUrn": "urn:li:msg_message:1",
                "*sender": other,
                "deliveredAt": 1_700_000_000_000,
                "body": {"text": "Earlier message"},
            },
        ],
    }
    page.script(
        "evaluate:voyager_conversations_fetch",
        {"body": json.dumps({"included": [{"dashEntityUrn": me}]})},
        {"body": json.dumps(payload)},
    )
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("get_thread", "thread"):
            arguments = {"thread_id": "2-policy-thread=="}
            result = await extractor.get_thread("2-policy-thread==")
    page.assert_clean()
    return recorder.trace({"method": "get_thread", "arguments": arguments}, result)


def _policy_profile(urn: str, first: str, company: str, start: int) -> dict[str, Any]:
    """A full-profile answer in the shape LinkedIn returns it, with one job."""
    group = f"{urn}:group"
    return {
        "data": {"*elements": [urn]},
        "included": [
            {
                "entityUrn": urn,
                "firstName": first,
                "lastName": "Policy",
                "publicIdentifier": first.lower(),
                "*profilePositionGroups": f"{urn}:groups",
                "*profileSkills": f"{urn}:skills",
            },
            {
                "entityUrn": f"{urn}:groups",
                "*elements": [group],
                "paging": {"total": 1},
            },
            {"entityUrn": group, "*profilePositionInPositionGroup": f"{group}:rows"},
            {"entityUrn": f"{group}:rows", "*elements": [f"{group}:job"]},
            {
                "entityUrn": f"{group}:job",
                "title": "Engineer",
                "companyName": "Analytical Engine",
                "*company": company,
                "dateRange": {"start": {"year": start, "month": 1}},
            },
            {"entityUrn": f"{urn}:skills", "*elements": [], "paging": {"total": 0}},
        ],
    }


_POLICY_MUTUAL = {
    "data": {
        "elements": [
            {
                "*miniProfile": "urn:li:fs_miniProfile:ACoAA-grace",
                "distance": {"value": "DISTANCE_1"},
            }
        ],
        "paging": {"total": 1},
    },
    "included": [
        {
            "entityUrn": "urn:li:fs_miniProfile:ACoAA-grace",
            "firstName": "Grace",
            "lastName": "Hopper",
            "publicIdentifier": "grace-hopper",
        }
    ],
}


async def _profile_views_scenario() -> dict[str, Any]:
    """Record what `get_profile_views` does to the page.

    With `full=False`: one evaluate and no navigation. The full list is read
    by opening the analytics page in the server's own browser and is covered by
    the reader's unit tests; the routine this serves had been opening the
    member's own browser for it.
    """
    recorder = TraceRecorder("get_profile_views__baseline", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    mini, card = "urn:li:fs_miniProfile:ACoAA-ada", "urn:li:fs_card:1"
    payload = {
        "included": [
            {
                "$type": "com.linkedin.voyager.identity.me.WvmpCard",
                "value": {
                    "insightCards": [
                        {
                            "objectUrn": "urn:li:wvmp:summary",
                            "value": {
                                "numViews": 12,
                                "timeFrame": "LAST_90_DAYS",
                                "numViewsChangeInPercentage": 0,
                                "*cards": [card],
                            },
                        }
                    ]
                },
            },
            {
                "entityUrn": card,
                "value": {
                    "viewer": {
                        "profile": {
                            "*miniProfile": mini,
                            "distance": {"value": "DISTANCE_2"},
                        }
                    },
                    "viewedAt": 1_700_000_000_000,
                },
            },
            {
                "entityUrn": mini,
                "firstName": "Ada",
                "lastName": "Lovelace",
                "publicIdentifier": "ada-lovelace",
                "dashEntityUrn": "urn:li:fsd_profile:ACoAA-ada",
            },
        ]
    }
    page.script("evaluate:voyager_conversations_fetch", {"body": json.dumps(payload)})
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("get_profile_views", "views"):
            result = await extractor.get_profile_views(full=False)
    page.assert_clean()
    return recorder.trace(
        {"method": "get_profile_views", "arguments": {"full": False}}, result
    )


async def _recruiter_views_scenario() -> dict[str, Any]:
    """Record what `get_recruiter_views` does to the page.

    Posts to the page's own paging action and nothing else: no navigation,
    no click. The page's headers are taken from a cache seeded here; taking
    them opens the analytics page once per browser session, which the
    reader's unit tests cover. Two windows: one row, then an empty answer,
    which is the end of the list. The row has no "Viewed 1d ago": its time is
    computed from the wall clock and would make this fixture drift daily.
    """
    from linkedin_mcp_server.voyager import profile_views as views_module

    recorder = TraceRecorder("get_recruiter_views__baseline", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    item = json.dumps({"threadlineDecoration": None, "key": "k"})
    row = [
        "$",
        "div",
        None,
        {
            "children": [
                ["$", "img", None, {"a11yText": "Acme"}],
                ["$", "p", None, {"children": ["Recruiter at Acme"]}],
                {"url": "https://www.linkedin.com/company/1001/insights/"},
            ]
        },
    ]
    stream = "0:" + json.dumps(["$", "div", None, {"children": [[item, row]]}])
    page.script(
        "evaluate:voyager_stream_post",
        {"status": 200, "text": stream},
        {"status": 200, "text": ""},
    )
    extractor = _extractor(page)
    views_module._HEADER_CACHE = (page, {"x-li-track": "{}"})
    try:
        async with boundaries(recorder, clock):
            with recorder.context("get_recruiter_views", "recruiters"):
                result = await extractor.get_recruiter_views(days=7)
    finally:
        views_module.forget_cached_headers()
    page.assert_clean()
    return recorder.trace(
        {"method": "get_recruiter_views", "arguments": {"days": 7}}, result
    )


async def _invite_person_scenario() -> dict[str, Any]:
    """Record what `invite_person` does to the page on a dry run.

    Two evaluates (the member, then the relationship) and nothing else: no
    navigation, no click, no write. The send itself is one POST and one more
    read, covered by the reader's unit tests; a trace that sent would pin a
    write into a fixture for no gain.
    """
    recorder = TraceRecorder("invite_person__dry_run", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    urn = "urn:li:fsd_profile:ACoAA-ada"
    profile = {
        "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
        "entityUrn": urn,
        "objectUrn": "urn:li:member:4242",
        "firstName": "Ada",
        "lastName": "Lovelace",
        "publicIdentifier": "ada-lovelace",
    }
    relationship = {
        "$type": "com.linkedin.voyager.dash.relationships.MemberRelationship",
        "entityUrn": "urn:li:fsd_memberRelationship:ACoAA-ada",
        "memberRelationshipUnion": {
            "noConnection": {"invitationUnion": {"noInvitation": {}}}
        },
    }
    page.script(
        "evaluate:voyager_conversations_fetch",
        {"body": json.dumps({"data": {"*elements": [urn]}, "included": [profile]})},
        {"body": json.dumps({"included": [profile, relationship]})},
    )
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("invite_person", "connect"):
            result = await extractor.invite_person("ada-lovelace", dry_run=True)
    page.assert_clean()
    return recorder.trace(
        {
            "method": "invite_person",
            "arguments": {"linkedin_username": "ada-lovelace", "dry_run": True},
        },
        result,
    )


async def _find_jobs_scenario() -> dict[str, Any]:
    """Record what the API job search does to the page.

    One evaluate per page of results and nothing else: no navigation, and no
    write. The search page it replaces posts every query to the member's
    job-search history.
    """
    recorder = TraceRecorder("find_jobs__baseline", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    card = {
        "$type": "com.linkedin.voyager.dash.jobs.JobPostingCard",
        "entityUrn": "urn:li:fsd_jobPostingCard:(123,JOBS_SEARCH)",
        "jobPostingUrn": "urn:li:fsd_jobPosting:123",
        "jobPostingTitle": "Python Engineer",
        "primaryDescription": {"text": "Acme"},
    }
    payload = {
        "data": {
            "paging": {"total": 1},
            "elements": [{"jobCardUnion": {"*jobPostingCard": card["entityUrn"]}}],
        },
        "included": [card],
    }
    page.script("evaluate:voyager_conversations_fetch", {"body": json.dumps(payload)})
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("find_jobs", "search_results"):
            result = await extractor.find_jobs("python", max_pages=1)
    page.assert_clean()
    return recorder.trace(
        {"method": "find_jobs", "arguments": {"keywords": "python", "max_pages": 1}},
        result,
    )


async def _get_job_scenario() -> dict[str, Any]:
    """Record what reading one posting from the API does: one evaluate."""
    recorder = TraceRecorder("get_job__baseline", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    payload = {
        "data": {
            "jobPostingId": 123,
            "title": "Python Engineer",
            "formattedLocation": "Remote",
            "jobState": "LISTED",
        }
    }
    page.script("evaluate:voyager_conversations_fetch", {"body": json.dumps(payload)})
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("get_job", "job_posting"):
            result = await extractor.get_job("123")
    page.assert_clean()
    return recorder.trace({"method": "get_job", "arguments": {"job_id": "123"}}, result)


async def _jobs_tracker_scenario() -> dict[str, Any]:
    """Record what reading the jobs tracker does to the page.

    One POST and nothing else: the tracker route is prefetched, not opened.
    The client's headers come from a cache seeded here; taking them loads the
    feed once per browser session, which the reader's unit tests cover.
    """
    from linkedin_mcp_server.voyager import jobs as jobs_module

    recorder = TraceRecorder("saved_jobs__baseline", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    record = {
        "jobId": "123",
        "jobTitle": "Python Engineer",
        "companyName": "Acme",
        "currentStageKey": "Saved",
    }
    stream = "0:" + json.dumps(
        {"viewName": "opportunity-tracker-add-note", "payload": record},
        separators=(",", ":"),
    )
    page.script("evaluate:voyager_stream_post", {"status": 200, "text": stream})
    extractor = _extractor(page)
    jobs_module._PREFETCH_HEADERS = (page, {"x-li-track": "{}"})
    try:
        async with boundaries(recorder, clock):
            with recorder.context("saved_jobs", "saved_jobs"):
                result = await extractor.saved_jobs()
    finally:
        jobs_module.forget_prefetch_headers()
    page.assert_clean()
    return recorder.trace({"method": "saved_jobs", "arguments": {}}, result)


async def _people_search_scenario() -> dict[str, Any]:
    """Record what the API people search does to the page.

    Two evaluates and no navigation: a place name is resolved to a geo, then
    one page of results is read. The tool it replaces loads the results page.
    """
    recorder = TraceRecorder("find_people__baseline", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    result_urn = "urn:li:fsd_entityResultViewModel:(urn:li:fsd_profile:ACoAA-ada,SEARCH_SRP,DEFAULT)"
    geo = {
        "data": {
            "elements": [
                {
                    "trackingUrn": "urn:li:geo:101165590",
                    "title": {"text": "United Kingdom"},
                }
            ]
        }
    }
    results = {
        "data": {
            "elements": [{"items": [{"itemUnion": {"*entityResult": result_urn}}]}],
            "paging": {"total": 150},
        },
        "included": [
            {
                "entityUrn": result_urn,
                "title": {"text": "Ada Lovelace"},
                "primarySubtitle": {"text": "Engineer at Analytical Engine"},
                "secondarySubtitle": {"text": "London"},
                "navigationUrl": "https://www.linkedin.com/in/ada-lovelace?x=1",
                "entityCustomTrackingInfo": {"memberDistance": "DISTANCE_2"},
            }
        ],
    }
    page.script(
        "evaluate:voyager_conversations_fetch",
        {"body": json.dumps(geo)},
        {"body": json.dumps(results)},
    )
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("find_people", "search"):
            arguments = {"keywords": "engineer", "location": "United Kingdom"}
            result = await extractor.find_people("engineer", location="United Kingdom")
    page.assert_clean()
    return recorder.trace({"method": "find_people", "arguments": arguments}, result)


async def _person_extra_scenario(method: str) -> dict[str, Any]:
    """Record what the two paged person reads do to the page.

    Two evaluates each and no navigation: the member is resolved to an id,
    then one page is read.
    """
    recorder = TraceRecorder(f"{method}__baseline", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    resolved = {"data": {"*elements": ["urn:li:fsd_profile:ACoAA-ada"]}, "included": []}
    activity = "urn:li:activity:7457533446958170112"
    posts = {
        "data": {"*elements": ["urn:li:fs_updateV2:1"]},
        "included": [
            {
                "$type": "com.linkedin.voyager.feed.render.UpdateV2",
                "entityUrn": "urn:li:fs_updateV2:1",
                "updateMetadata": {"urn": activity},
                "actor": {"name": {"text": "Ada Lovelace"}},
                "commentary": {"text": {"text": "On the engine"}},
            }
        ],
    }
    page.script(
        "evaluate:voyager_conversations_fetch",
        {"body": json.dumps(resolved)},
        {
            "body": json.dumps(
                _POLICY_MUTUAL if method == "get_mutual_connections" else posts
            )
        },
    )
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context(method, "person"):
            arguments = {"linkedin_username": "ada"}
            result = await getattr(extractor, method)("ada")
    page.assert_clean()
    return recorder.trace({"method": method, "arguments": arguments}, result)


async def _person_scenario() -> dict[str, Any]:
    """Record what `get_person` does to the page.

    Seven evaluates and no navigation: the profile, how the signed-in member
    relates to it, its contact fields, its follower and connection counts, the
    connections the two share, who is signed in, and that member's own profile
    for the comparison. The tool beside it loads the profile page and one more
    page per section.
    """
    recorder = TraceRecorder("get_person__baseline", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    me, ada = "urn:li:fsd_profile:ACoAA-me", "urn:li:fsd_profile:ACoAA-ada"
    company = "urn:li:fsd_company:1"
    relationship = {
        "included": [
            {
                "$type": "com.linkedin.voyager.dash.relationships.MemberRelationship",
                "memberRelationshipUnion": {"*connection": "urn:li:fsd_connection:x"},
            }
        ]
    }
    page.script(
        "evaluate:voyager_conversations_fetch",
        {"body": json.dumps(_policy_profile(ada, "Ada", company, 2015))},
        {"body": json.dumps(relationship)},
        {"body": json.dumps({"included": []})},
        # Follower and connection counts: one more read, still no page.
        {"body": json.dumps({"included": []})},
        {"body": json.dumps(_POLICY_MUTUAL)},
        {"body": json.dumps({"included": [{"dashEntityUrn": me}]})},
        {"body": json.dumps(_policy_profile(me, "Taylor", company, 2013))},
    )
    extractor = _extractor(page)
    voyager_person.forget_my_profile()
    try:
        async with boundaries(recorder, clock):
            with recorder.context("get_person", "person"):
                arguments = {"linkedin_username": "ada"}
                result = await extractor.get_person("ada")
    finally:
        voyager_person.forget_my_profile()
    page.assert_clean()
    return recorder.trace({"method": "get_person", "arguments": arguments}, result)


async def _my_person_scenario() -> dict[str, Any]:
    """Record what reading one's own profile from the API does.

    Five evaluates and no navigation: who is signed in, that profile, the
    relationship (self), the contact fields and the network counts. The tool it replaces loads
    the profile page and one more page per section.
    """
    recorder = TraceRecorder("my_person__baseline", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    me = "urn:li:fsd_profile:ACoAA-me"
    relationship = {
        "included": [
            {
                "$type": "com.linkedin.voyager.dash.relationships.MemberRelationship",
                "memberRelationshipUnion": {"self": {}},
            }
        ]
    }
    page.script(
        "evaluate:voyager_conversations_fetch",
        {"body": json.dumps({"included": [{"dashEntityUrn": me}]})},
        {
            "body": json.dumps(
                _policy_profile(me, "Taylor", "urn:li:fsd_company:1", 2013)
            )
        },
        {"body": json.dumps(relationship)},
        {"body": json.dumps({"included": []})},
        # Follower and connection counts: one more read, still no page.
        {"body": json.dumps({"included": []})},
    )
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("my_person", "person"):
            result = await extractor.my_person()
    page.assert_clean()
    return recorder.trace({"method": "my_person", "arguments": {}}, result)


_POLICY_COMPANY = {
    "data": {"*elements": ["urn:li:fs_normalized_company:1001"]},
    "included": [
        {
            "entityUrn": "urn:li:fs_normalized_company:1001",
            "name": "Acme",
            "universalName": "acme",
        }
    ],
}
_POLICY_EMPTY = {"data": {"elements": []}, "included": []}


async def _api_read_scenario(
    method: str,
    arguments: dict[str, Any],
    *,
    fetches: tuple[dict[str, Any], ...] = (),
    streams: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Record what one API-backed read does to the page.

    Every tool below replaces one that loaded a LinkedIn page. Each is a
    fixed number of reads and nothing else: no navigation, no click. The
    stream reads post to LinkedIn's own component and paging actions with
    headers from a cache seeded here; taking those headers opens one page per
    browser session, which the readers' unit tests cover.
    """
    from linkedin_mcp_server.voyager import content as content_module
    from linkedin_mcp_server.voyager import profile_views as views_module

    recorder = TraceRecorder(f"{method}__baseline", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    if fetches:
        page.script(
            "evaluate:voyager_conversations_fetch",
            *({"body": json.dumps(payload)} for payload in fetches),
        )
    if streams:
        page.script(
            "evaluate:voyager_stream_post",
            *({"status": 200, "text": text} for text in streams),
        )
    extractor = _extractor(page)
    if streams:
        views_module._HEADER_CACHE = (page, {"x-li-track": "{}"})
    # A search carries a random id; pinned so the trace is reproducible.
    minted = content_module._search_id
    setattr(content_module, "_search_id", lambda: "policy-search-id")
    try:
        async with boundaries(recorder, clock):
            with recorder.context(method, "api"):
                result = await getattr(extractor, method)(**arguments)
    finally:
        setattr(content_module, "_search_id", minted)
        views_module.forget_cached_headers()
    page.assert_clean()
    return recorder.trace({"method": method, "arguments": arguments}, result)


async def _message_search_scenario() -> dict[str, Any]:
    """Record what `search_messages` does to the page.

    Two evaluates and no navigation. The tool it replaces typed into the
    search box and opened the first match, which marked it read.
    """
    recorder = TraceRecorder("search_messages__baseline", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    me = "urn:li:fsd_profile:ACoAA-me"
    ada = "urn:li:msg_messagingParticipant:urn:li:fsd_profile:ACoAA-ada"
    conversation = f"urn:li:msg_conversation:({me},2-policy-thread==)"
    payload = {
        "data": {
            "data": {
                "messengerConversationsBySearchCriteria": {
                    "metadata": {"nextCursor": None},
                    "*elements": [conversation],
                }
            }
        },
        "included": [
            {
                "$type": "com.linkedin.messenger.MessagingParticipant",
                "entityUrn": ada,
                "hostIdentityUrn": "urn:li:fsd_profile:ACoAA-ada",
                "participantType": {
                    "member": {
                        "firstName": {"text": "Ada"},
                        "lastName": {"text": "Lovelace"},
                    }
                },
            },
            {
                "$type": "com.linkedin.messenger.Conversation",
                "entityUrn": conversation,
                "conversationUrl": _MESSAGE_ROUTE,
                "*conversationParticipants": [ada],
                "lastActivityAt": 1_700_000_000_000,
            },
            {
                "$type": "com.linkedin.messenger.Message",
                "*conversation": conversation,
                "*sender": ada,
                "deliveredAt": 1_700_000_000_000,
                "body": {"text": "About the engine"},
            },
        ],
    }
    page.script(
        "evaluate:voyager_conversations_fetch",
        {"body": json.dumps({"included": [{"dashEntityUrn": me}]})},
        {"body": json.dumps(payload)},
    )
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("search_messages", "search"):
            arguments = {"keywords": "engine"}
            result = await extractor.search_messages("engine")
    page.assert_clean()
    return recorder.trace({"method": "search_messages", "arguments": arguments}, result)


async def _person_message_scenario(confirm_send: bool) -> dict[str, Any]:
    """Record what `message_person` does to the page.

    A dry run is two reads: who is signed in and who the recipient is. A
    confirmed message adds one write. No profile is opened and nothing is
    typed, where `send_message` navigates twice and drives a composer.
    """
    outcome = "sent" if confirm_send else "dry_run"
    recorder = TraceRecorder(f"message_person__{outcome}", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    me = "urn:li:fsd_profile:ACoAA-me"
    ada = "urn:li:fsd_profile:ACoAA-policy"
    answers: list[dict[str, Any]] = [
        {"body": json.dumps({"included": [{"dashEntityUrn": me}]})},
        {
            "body": json.dumps(
                {
                    "data": {"*elements": [ada]},
                    "included": [
                        {"entityUrn": ada, "firstName": "Ada", "lastName": "Lovelace"}
                    ],
                }
            )
        },
    ]
    if confirm_send:
        created = {
            "value": {
                "entityUrn": f"urn:li:msg_message:({me},2-new)",
                "conversationUrn": f"urn:li:msg_conversation:({me},2-policy-thread==)",
                "deliveredAt": 1_700_000_100_000,
            }
        }
        answers.append({"status": 200, "body": json.dumps(created)})
    page.script("evaluate:voyager_conversations_fetch", *answers)

    extractor = _extractor(page)
    with (
        patch.object(thread_reply, "_origin_token", return_value="policy-origin-token"),
        patch.object(thread_reply, "_tracking_id", return_value="policy-tracking-id"),
    ):
        async with boundaries(recorder, clock):
            with recorder.context("message_person", "message"):
                result = await extractor.message_person(
                    "ada-lovelace", "New text", confirm_send=confirm_send
                )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "message_person",
            "arguments": {"confirm_send": confirm_send, "outcome": outcome},
        },
        result,
    )


class _ScriptedRequest:
    """The one Request attribute discovery reads."""

    def __init__(self, url: str):
        self.url = url


async def _conversation_scenario(method: str) -> dict[str, Any]:
    name = f"{method}__baseline"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    if method != "search_conversations":
        scrolls = 3 if method == "get_conversation" else 1
        page.script("evaluate:scroll_main_region", *([True] * scrolls))
    page.script("evaluate:root_content", _root("Conversation content"))
    if method != "get_conversation":
        page.script(
            "wait_for_selector:conversation_rows",
            PlaywrightTimeoutError("no scripted rows"),
        )
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context(method, "conversation"):
            if method == "get_inbox":
                arguments = {"limit": 10}
                result = await extractor.get_inbox(limit=10)
            elif method == "get_conversation":
                arguments = {"thread_id": "2-abc"}
                result = await extractor.get_conversation(thread_id="2-abc")
            elif method == "search_conversations":
                arguments = {"keywords": "engine", "limit": 10}
                result = await extractor.search_conversations("engine", limit=10)
            else:
                raise AssertionError(method)
    page.assert_clean()
    return recorder.trace(
        {"method": method, "arguments": arguments},
        _complete_mapping_result(result, section_names=list(result["sections"])),
    )


async def _facade_contract_trace() -> dict[str, Any]:
    global _TOOL_SCHEMAS

    methods = {}
    for name in TOOL_FACADE_METHODS | COMPATIBILITY_METHODS:
        member = getattr(LinkedInExtractor, name)
        methods[name] = {
            "signature": str(inspect.signature(member)),
            "coroutine": inspect.iscoroutinefunction(member),
        }
    if _TOOL_SCHEMAS is None:
        tools = await create_mcp_server().list_tools()
        _TOOL_SCHEMAS = {
            tool.name: {
                "input": tool.parameters,
                "output": tool.output_schema,
            }
            for tool in sorted(tools, key=lambda item: item.name)
        }
    return {
        "schema_version": 1,
        "scenario": "facade_contract",
        "call": {"method": "LinkedInExtractor", "arguments": {"constructor": "Page"}},
        "events": [],
        "result": {
            "tool_methods": sorted(TOOL_FACADE_METHODS),
            "compatibility_methods": sorted(COMPATIBILITY_METHODS),
            "methods": methods,
            "tool_schemas": _TOOL_SCHEMAS,
        },
    }


TOOL_FACADE_METHODS = {
    "connect_with_person",
    "get_conversations",
    "get_invitations",
    "extract_feed",
    "extract_page",
    "get_company_employees",
    "get_conversation",
    "get_inbox",
    "get_my_profile",
    "get_saved_jobs",
    "get_sidebar_profiles",
    "scrape_company",
    "scrape_job",
    "scrape_person",
    "search_companies",
    "search_conversations",
    "search_jobs",
    "search_people",
    "search_posts",
    "send_message",
    "reply_to_thread",
    "get_thread",
    "search_messages",
    "message_person",
    "get_person",
    "get_mutual_connections",
    "get_person_posts",
    "find_people",
    "get_profile_views",
    "get_recruiter_views",
    "invite_person",
    "find_jobs",
    "get_job",
    "saved_jobs",
    "my_person",
    "company_record",
    "company_posts",
    "company_people",
    "find_companies",
    "find_posts",
    "home_feed",
    "sidebar_people",
}
COMPATIBILITY_METHODS = {"get_page_text", "click_button_by_text"}


async def build_policy_traces() -> dict[str, dict[str, Any]]:
    traces = {
        "facade-contract.json": await _facade_contract_trace(),
        "generic-ordinary.json": await _generic_capture_scenario(
            "extract_page__ordinary", "https://www.linkedin.com/in/ada-lovelace/"
        ),
        "generic-activity.json": await _generic_capture_scenario(
            "extract_page__activity",
            "https://www.linkedin.com/in/ada-lovelace/recent-activity/all/",
        ),
        "generic-search.json": await _generic_capture_scenario(
            "extract_page__search",
            "https://www.linkedin.com/search/results/people/?keywords=ada",
        ),
        "generic-company-people.json": await _generic_capture_scenario(
            "extract_page__company_people",
            "https://www.linkedin.com/company/analytical-engine/people/",
        ),
        "person-sections.json": await _person_sections_scenario(),
        "company-sections.json": await _company_sections_scenario(),
        "job-search.json": await _job_search_scenario(),
        "job-search-route-alias.json": await _job_search_scenario(
            "/jobs/search-results/"
        ),
        "job-search-metadata-upgrade.json": await _job_search_upgrade_scenario(),
        "saved-jobs.json": await _saved_jobs_scenario(),
        "feed-stale.json": await _feed_stale_scenario(),
        "feed-response-success.json": await _feed_response_scenario(body_failure=False),
        "feed-response-failure.json": await _feed_response_scenario(body_failure=True),
        "message-target-unavailable.json": await _message_target_scenario(
            "unavailable"
        ),
        "message-target-unresolved.json": await _message_target_scenario("unresolved"),
        "message-dry-run.json": await _messaging_dry_run_scenario(),
        "message-composer-occupied.json": await _occupied_message_scenario(
            restored_during_write=False
        ),
        "message-composer-restored.json": await _occupied_message_scenario(
            restored_during_write=True
        ),
        "message-pre-submit-cleanup.json": await _messaging_submission_scenario(
            "pre_submit_cleanup"
        ),
        "message-submit-rejected.json": await _messaging_submission_scenario(
            "submission_rejected"
        ),
        "message-submit-interrupted.json": await _messaging_submission_scenario(
            "submission_interrupted"
        ),
        "message-unconfirmed.json": await _messaging_submission_scenario("unconfirmed"),
        "message-sent.json": await _messaging_submission_scenario("sent"),
        "message-cancelled.json": await _messaging_cancellation_scenario(),
        "message-blank.json": await _invalid_message_scenario("   ", "blank"),
        "message-c0.json": await _invalid_message_scenario("line\nbreak", "c0"),
        "message-del.json": await _invalid_message_scenario("text\x7f", "del"),
        "connect.json": await _connect_scenario(),
        "get-my-profile.json": await _get_my_profile_scenario(),
        "sidebar-profiles.json": await _sidebar_scenario(),
        "company-employees.json": await _single_capture_facade_scenario(
            "get_company_employees"
        ),
        "scrape-job.json": await _single_capture_facade_scenario("scrape_job"),
        "scrape-job-error.json": await _single_capture_error_scenario(),
        "search-people.json": await _single_capture_facade_scenario("search_people"),
        "search-companies.json": await _single_capture_facade_scenario(
            "search_companies"
        ),
        "search-posts.json": await _single_capture_facade_scenario("search_posts"),
        "inbox.json": await _conversation_scenario("get_inbox"),
        "conversation.json": await _conversation_scenario("get_conversation"),
        "conversations-page.json": await _conversations_page_scenario(),
        "invitations.json": await _invitations_scenario(),
        "thread.json": await _thread_scenario(),
        "person.json": await _person_scenario(),
        "people-search.json": await _people_search_scenario(),
        "profile-views.json": await _profile_views_scenario(),
        "recruiter-views.json": await _recruiter_views_scenario(),
        "invite-person.json": await _invite_person_scenario(),
        "find-jobs.json": await _find_jobs_scenario(),
        "get-job.json": await _get_job_scenario(),
        "jobs-tracker.json": await _jobs_tracker_scenario(),
        "my-person.json": await _my_person_scenario(),
        "company-record.json": await _api_read_scenario(
            "company_record", {"company_name": "acme"}, fetches=(_POLICY_COMPANY,)
        ),
        "company-posts-api.json": await _api_read_scenario(
            "company_posts", {"company_name": "acme"}, fetches=(_POLICY_EMPTY,)
        ),
        "company-people.json": await _api_read_scenario(
            "company_people",
            {"company_name": "acme"},
            fetches=(_POLICY_COMPANY, _POLICY_EMPTY),
        ),
        "find-companies.json": await _api_read_scenario(
            "find_companies", {"keywords": "acme"}, fetches=(_POLICY_EMPTY,)
        ),
        "home-feed.json": await _api_read_scenario(
            "home_feed", {"num_posts": 5}, fetches=(_POLICY_EMPTY,)
        ),
        "find-posts.json": await _api_read_scenario(
            "find_posts", {"keywords": "hiring", "max_pages": 1}, streams=("",)
        ),
        "sidebar-people.json": await _api_read_scenario(
            "sidebar_people",
            {"linkedin_username": "ada-lovelace"},
            streams=(
                '0:{"url":"https://www.linkedin.com/in/grace-hopper"}',
                '0:{"url":"https://www.linkedin.com/in/alan-turing"}',
            ),
        ),
        "person-mutual.json": await _person_extra_scenario("get_mutual_connections"),
        "person-posts.json": await _person_extra_scenario("get_person_posts"),
        "message-search.json": await _message_search_scenario(),
        "person-message-dry-run.json": await _person_message_scenario(False),
        "person-message-sent.json": await _person_message_scenario(True),
        "thread-reply-dry-run.json": await _thread_reply_scenario("dry_run"),
        "thread-reply-rejected.json": await _thread_reply_scenario("rejected"),
        "thread-reply-sent.json": await _thread_reply_scenario("sent"),
        "search-conversations.json": await _conversation_scenario(
            "search_conversations"
        ),
    }
    return traces


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def policy_trace_diff(
    generated: dict[str, dict[str, Any]], trace_root: Path = TRACE_ROOT
) -> str:
    """Return one deterministic unified comparison against canonical traces."""

    generated_names = set(generated)
    fixture_names = {path.name for path in trace_root.glob("*.json")}
    chunks = [
        f"missing canonical trace: {trace_root / name}\n"
        for name in sorted(generated_names - fixture_names)
    ]
    chunks.extend(
        f"unexpected canonical trace: {trace_root / name}\n"
        for name in sorted(fixture_names - generated_names)
    )
    for name in sorted(generated_names & fixture_names):
        path = trace_root / name
        expected = path.read_text(encoding="utf-8")
        actual = canonical_json(generated[name])
        chunks.extend(
            unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=str(path),
                tofile=f"generated/{name}",
            )
        )
    return "".join(chunks)
