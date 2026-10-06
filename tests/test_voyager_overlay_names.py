"""A replacement keeps the name, and the arguments, of the tool it replaces.

Each test here calls a served tool the way a caller of UPSTREAM's tool of that
name would, because that is the promise: swapping the implementation must not
break whoever was already calling it.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool

from linkedin_mcp_server.server import create_mcp_server
from linkedin_mcp_server.voyager import overlay

REPLACED_IN_PLACE = (
    "get_conversation",
    "search_conversations",
    "send_message",
    "get_person_profile",
    "connect_with_person",
    "search_jobs",
    "get_job_details",
    "get_saved_jobs",
    "get_my_profile",
    "get_company_profile",
    "get_company_posts",
    "get_company_employees",
    "search_companies",
    "search_posts",
    "get_feed",
    "get_sidebar_profiles",
)


async def _run(tool, *args, extractor, **kwargs):
    """Call a served tool with ``extractor`` answering its readiness call."""
    with patch(
        "linkedin_mcp_server.voyager.overlay.get_ready_extractor",
        AsyncMock(return_value=extractor),
    ):
        return await tool.fn(*args, **kwargs)


async def _tool(name: str) -> Any:
    tool = await create_mcp_server().get_tool(name)
    assert tool is not None, name
    return cast(FunctionTool, tool)


@pytest.mark.parametrize("name", REPLACED_IN_PLACE)
async def test_the_served_tool_of_that_name_is_this_forks(name):
    assert overlay.SUPERSEDED[name] == name
    tool = await _tool(name)
    assert tool.fn.__module__ == "linkedin_mcp_server.voyager.overlay"


@pytest.mark.parametrize(
    ("name", "upstream_arguments"),
    [
        ("get_conversation", {"linkedin_username", "thread_id", "index"}),
        ("search_conversations", {"keywords", "limit"}),
        (
            "send_message",
            {"linkedin_username", "message", "confirm_send", "profile_urn"},
        ),
        ("get_person_profile", {"linkedin_username", "sections", "max_scrolls"}),
        ("connect_with_person", {"linkedin_username", "note"}),
        (
            "search_jobs",
            {
                "keywords",
                "location",
                "max_pages",
                "date_posted",
                "job_type",
                "experience_level",
                "work_type",
                "easy_apply",
                "sort_by",
            },
        ),
        ("get_job_details", {"job_id"}),
        ("get_saved_jobs", {"max_pages"}),
        ("get_my_profile", {"sections", "max_scrolls"}),
        ("get_company_profile", {"company_name", "sections"}),
        ("get_company_posts", {"company_name"}),
        ("get_company_employees", {"company_name", "keywords"}),
        ("search_companies", {"keywords"}),
        ("search_posts", {"keywords", "date_posted", "max_pages"}),
        ("get_feed", {"num_posts"}),
        ("get_sidebar_profiles", {"linkedin_username"}),
    ],
)
async def test_every_argument_upstream_accepted_is_still_accepted(
    name, upstream_arguments
):
    properties = set((await _tool(name)).parameters["properties"])
    assert upstream_arguments <= properties, upstream_arguments - properties


@pytest.mark.parametrize(
    ("name", "required"),
    [
        ("get_conversation", []),
        ("search_conversations", ["keywords"]),
        ("send_message", ["linkedin_username", "message", "confirm_send"]),
        ("get_person_profile", ["linkedin_username"]),
        ("connect_with_person", ["linkedin_username"]),
        ("search_jobs", ["keywords"]),
        ("get_job_details", ["job_id"]),
        ("get_saved_jobs", []),
        ("get_my_profile", []),
        ("get_company_profile", ["company_name"]),
        ("get_company_posts", ["company_name"]),
        ("get_company_employees", ["company_name"]),
        ("search_companies", ["keywords"]),
        ("search_posts", ["keywords"]),
        ("get_feed", []),
        ("get_sidebar_profiles", ["linkedin_username"]),
    ],
)
async def test_nothing_new_became_required(name, required):
    assert (await _tool(name)).parameters.get("required", []) == required


async def test_get_conversation_by_username_and_index_reaches_the_reader(mock_context):
    extractor = MagicMock()
    extractor.get_thread = AsyncMock(return_value={"sections": {}})
    tool = await _tool("get_conversation")

    await _run(
        tool,
        mock_context,
        linkedin_username="ada-lovelace",
        index=1,
        extractor=extractor,
    )

    extractor.get_thread.assert_awaited_once_with(
        None, linkedin_username="ada-lovelace", index=1
    )


async def test_get_conversation_with_neither_argument_says_what_to_pass(mock_context):
    tool = await _tool("get_conversation")

    with pytest.raises(ToolError, match="linkedin_username or thread_id"):
        await _run(tool, mock_context, extractor=MagicMock())


async def test_search_conversations_accepts_limit_without_cutting_the_page(
    mock_context,
):
    extractor = MagicMock()
    extractor.search_messages = AsyncMock(return_value={"count": 20})
    tool = await _tool("search_conversations")

    result = await _run(tool, "engine", mock_context, limit=5, extractor=extractor)

    # Not applied: a page cut short while the cursor moves on skips the rest.
    assert result == {"count": 20}
    extractor.search_messages.assert_awaited_once_with("engine", cursor=None)


async def test_send_message_forwards_the_profile_urn(mock_context):
    extractor = MagicMock()
    extractor.message_person = AsyncMock(return_value={"status": "sent"})
    tool = await _tool("send_message")

    await _run(
        tool,
        "ada-lovelace",
        "hello",
        True,
        mock_context,
        profile_urn="ACoAAB",
        extractor=extractor,
    )

    extractor.message_person.assert_awaited_once_with(
        "ada-lovelace", "hello", confirm_send=True, profile_urn="ACoAAB"
    )


async def test_get_person_profile_reads_posts_only_when_asked(mock_context):
    def extractor() -> MagicMock:
        fake = MagicMock()
        fake.get_person = AsyncMock(
            side_effect=lambda *a, **k: {"sections": {"main_profile": "Ada"}}
        )
        fake.get_person_posts = AsyncMock(
            return_value={
                "sections": {"posts": "a post"},
                "posts": [{"text": "a post"}],
                "next_cursor": "tok",
            }
        )
        return fake

    tool = await _tool("get_person_profile")

    plain = extractor()
    result = await _run(
        tool,
        "ada-lovelace",
        mock_context,
        sections="experience,education",
        extractor=plain,
    )
    assert set(result["sections"]) == {"main_profile"}
    assert "unknown_sections" not in result
    plain.get_person_posts.assert_not_awaited()

    with_posts = extractor()
    result = await _run(
        tool,
        "ada-lovelace",
        mock_context,
        sections="posts, Bogus",
        max_scrolls=20,
        extractor=with_posts,
    )
    assert result["sections"]["posts"] == "a post"
    assert result["posts_next_cursor"] == "tok"
    assert result["unknown_sections"] == ["bogus"]
    with_posts.get_person_posts.assert_awaited_once_with("ada-lovelace", count=10)


async def test_connect_sends_through_the_api_with_or_without_a_note(mock_context):
    extractor = MagicMock()
    extractor.invite_person = AsyncMock(return_value={"status": "pending"})
    extractor.connect_with_person = AsyncMock()
    tool = await _tool("connect_with_person")

    await _run(
        tool, linkedin_username="ada-lovelace", ctx=mock_context, extractor=extractor
    )
    await _run(
        tool,
        linkedin_username="ada-lovelace",
        ctx=mock_context,
        note="Hi Ada",
        extractor=extractor,
    )

    assert extractor.invite_person.await_args_list[0].kwargs == {
        "note": None,
        "dry_run": False,
    }
    assert extractor.invite_person.await_args_list[1].kwargs == {
        "note": "Hi Ada",
        "dry_run": False,
    }
    # Upstream's page-driven flow is no longer reached for either.
    extractor.connect_with_person.assert_not_called()


async def test_connect_never_reaches_upstreams_full_flow(mock_context):
    # An incoming invitation is accepted inside invite_person, accept-only.
    # Upstream's whole flow could send a new request if the invitation had
    # gone in between, so the tool must never call it, dry run or not.
    extractor = MagicMock()
    extractor.invite_person = AsyncMock(return_value={"status": "accepted"})
    extractor.connect_with_person = AsyncMock()
    tool = await _tool("connect_with_person")

    for dry_run in (False, True):
        await _run(
            tool,
            linkedin_username="ada-lovelace",
            ctx=mock_context,
            dry_run=dry_run,
            extractor=extractor,
        )

    extractor.connect_with_person.assert_not_called()


async def test_my_posts_are_read_by_id_when_the_profile_has_no_public_identifier(
    mock_context,
):
    fake = MagicMock()
    fake.my_person = AsyncMock(
        return_value={
            "sections": {"main_profile": "Me"},
            "identity": {"profile_urn": "urn:li:fsd_profile:ACoAA-me"},
        }
    )
    fake.get_person_posts = AsyncMock(
        return_value={"sections": {"posts": "p"}, "posts": [], "next_cursor": None}
    )
    tool = await _tool("get_my_profile")

    result = await _run(tool, mock_context, sections="posts", extractor=fake)

    assert result["sections"]["posts"] == "p"
    fake.get_person_posts.assert_awaited_once_with("ACoAA-me", count=10)


@pytest.mark.parametrize(
    "name", [overlay.UPSTREAM_TOOLS_ENV, "LINKEDIN_MCP_DIFFERENTIAL_CI"]
)
async def test_the_upstream_tools_switch_leaves_the_overlay_out(monkeypatch, name):
    """Upstream's differential rows need upstream's tools, exactly as written."""
    monkeypatch.setenv(name, "1")
    names = {tool.name for tool in await create_mcp_server().list_tools()}

    assert "get_inbox" in names
    assert not {"get_conversations", "get_invitations", "reply_to_thread"} & names


async def test_only_the_value_one_turns_the_overlay_off(monkeypatch):
    monkeypatch.setenv(overlay.UPSTREAM_TOOLS_ENV, "0")
    names = {tool.name for tool in await create_mcp_server().list_tools()}

    assert "get_inbox" not in names
    assert "get_conversations" in names
