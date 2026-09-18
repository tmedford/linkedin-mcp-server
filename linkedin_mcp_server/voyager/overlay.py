"""Serve our API-reading tools in place of the page-scraping ones.

Installed once, after upstream has registered its own tools. For each tool we
supersede it removes upstream's registration and registers ours under a name of
our own, so a caller is offered one way to do the job rather than two that
disagree.

**Superseding is done by not serving a tool, never by editing it.** Upstream's
implementation is left exactly as written and still imports, still has its
tests, and comes back the moment an entry is dropped from :data:`SUPERSEDED`.
That is what keeps the nightly upstream merge clean
(``tests/test_fork_divergence_is_additive.py``), and it is why this lives in a
package upstream does not have instead of as edits inside theirs.

**Removal is verified and loud.** A tool that is not there to remove means
upstream renamed or dropped it, which is precisely when a silent pass is
dangerous: the overlay would look installed, our tool would be served next to a
survivor under another name, and the two would disagree about the mailbox with
nothing reporting it. So a missing tool raises here, at startup, naming the
tool and what to do about it, rather than at some later call.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import Context, FastMCP

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error

logger = logging.getLogger(__name__)


class OverlayError(RuntimeError):
    """The overlay could not be installed as written, so the server stops."""


#: Upstream tool -> the tool of ours that replaces it, and why.
#:
#: ``get_inbox`` scrapes the rendered sidebar. It sees roughly the first 16
#: rows, which is a floor rather than an answer, and it recovers each thread id
#: by click-visiting the row, so asking it a question about the mailbox costs
#: unread state. ``get_conversations`` reads the API the web client itself
#: calls: any page of the mailbox, one request, nothing clicked.
SUPERSEDED: dict[str, str] = {
    "get_inbox": "get_conversations",
}


def _remove_one(mcp: FastMCP, name: str) -> None:
    """Remove a registered tool across the fastmcp versions this runs on.

    ``FastMCP.remove_tool`` is deprecated in favour of
    ``local_provider.remove_tool`` and warns on every call. Preferring the
    provider keeps the deprecation out of the server's startup path while still
    working if the attribute is not there, which matters because upstream pins
    fastmcp loosely and this fork follows whatever that resolves to.
    """
    provider = getattr(mcp, "local_provider", None)
    remove = getattr(provider, "remove_tool", None) if provider else None
    if remove is None:
        remove = mcp.remove_tool
    remove(name)


def _remove_superseded(mcp: FastMCP) -> None:
    """Drop upstream's registrations for everything we replace."""
    for upstream_name, ours in SUPERSEDED.items():
        try:
            _remove_one(mcp, upstream_name)
        except Exception as exc:  # fastmcp raises NotFoundError, not a subclass
            raise OverlayError(
                f"Cannot remove upstream's {upstream_name!r}, which "
                f"{ours!r} is registered to replace: {exc}. Upstream has "
                f"renamed or dropped it, so this fork is superseding a tool "
                f"that no longer exists. Update SUPERSEDED in "
                f"linkedin_mcp_server/voyager/overlay.py rather than letting "
                f"both tools be served."
            ) from exc
        logger.info("Superseded upstream %s with %s", upstream_name, ours)


def install_voyager_overlay(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Remove the tools we supersede, then register ours in their place."""
    _remove_superseded(mcp)

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Conversations",
        # Genuinely read-only, unlike the get_inbox it replaces: this reads
        # LinkedIn's own conversations API and never clicks a row, so no thread
        # is marked read.
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"messaging", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_conversations(
        ctx: Context,
        cursor: str | None = None,
        category: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Read ONE page of conversations (up to 25) from LinkedIn's messaging API.

        Use this for anything about the mailbox as a whole: which threads are
        unanswered, who has gone quiet, reconciling against an external record.
        It reads the API the web client itself calls, so it reaches any page of
        the mailbox and alters nothing.

        To read more, call again with `cursor` set to the `next_cursor` you were
        given. Paging is recency-first, so page one is the most recent
        conversations. Reconnect work ("who have I fallen out of touch with")
        means paging backwards until `last_activity_iso` is old enough; that is
        deliberately the caller's loop, since only the caller knows when to stop.

        Each conversation carries thread_urn, participants, last_activity_iso,
        read, unread_count, last_message_text and awaiting_my_reply, so
        "have I replied to everyone" is a field rather than an inference.

        Args:
            ctx: FastMCP context for progress reporting
            cursor: next_cursor from a previous call. OMIT for the first page.
            category: SERVER-SIDE filter, the only one LinkedIn honours. One of
                INBOX, PRIMARY_INBOX, ARCHIVE, INMAIL, STARRED, SPAM. These
                reach any point in time in a single call. An unknown value is
                rejected rather than passed through, because the API answers one
                with an empty page that would read as "you have none".
                **Omitting it is not "no filter"**: the query the messaging page
                issues already carries a category, so leaving this unset reads
                whichever mailbox that page was showing, in practice
                PRIMARY_INBOX. Pass one explicitly to be sure which you get.

        Returns:
            Dict with url and sections (the standard scraping-tool shape), plus
            conversations, count, page_size, next_cursor, at_end and
            zero_reason. `conversations` is the structured answer; `sections`
            carries the same page as readable text for generic consumers.

            **at_end is measured, not inferred**: True means the server returned
            FEWER than page_size, so there is no more. False means a full page,
            so there is more. **None means an empty page, which proves nothing
            either way and must never be read as the end.**

            zero_reason explains an empty page: "after-cursor" (the page after
            the last one) or "empty-page" (nothing came back for what was
            asked). Neither is evidence of the end, which is why at_end is None
            there. A dead session or a rejected request is a non-200 and raises,
            so an empty page that reaches you really is an empty result.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_conversations"
            )
            logger.info("Reading conversations page (cursor=%s)", bool(cursor))

            await ctx.report_progress(
                progress=0, total=100, message="Reading conversations"
            )

            result = await extractor.get_conversations(
                cursor=cursor,
                category=category,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_conversations")
        except Exception as e:
            raise_tool_error(e, "get_conversations")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Invitations",
        # Reads the relationships API. Nothing is clicked and no invitation is
        # accepted, ignored or withdrawn -- this only reports what is pending.
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"invitations", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_invitations(
        ctx: Context,
        direction: str = "received",
        start: int = 0,
        count: int = 50,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Read ONE page of the invitation manager from LinkedIn's own API.

        Nothing upstream covers this surface, so the alternative is driving a
        browser to the invitation manager and reading a lazy-loading list out
        of the DOM. This asks the endpoint that page calls: no rendering, no
        scrolling, and no dependence on markup.

        Args:
            ctx: FastMCP context for progress reporting
            direction: "received" for invitations sent TO you, "sent" for ones
                you sent. They are different endpoints rather than two values
                of one filter, and they carry the counterparty under different
                keys, so pick the board you actually mean.
            start: 0-based offset into the board. Paging is the caller's loop,
                because only the caller knows when to stop.
            count: how many to ask for. `at_end` is measured against this.

        Returns:
            Dict with url and sections (the standard scraping-tool shape), plus
            invitations, count, page_size, start, direction, at_end and
            zero_reason.

            Each invitation carries name, headline, profile_slug, state,
            sent_at_iso, and both `has_note` and the raw `has_note_flag`.
            **`customMessage` is a boolean flag, not the note** -- the text is
            in `note`, and both are reported so a disagreement is visible
            rather than silently resolved.

            **at_end is measured, not inferred**: True means fewer came back
            than were asked for, so there is no more. False means a full page.
            **None means an empty page, which proves nothing either way.**

            **The board's own `paging.total` is deliberately not consulted.** It
            has read 0 against a full board on every run it was checked, so the
            only honest count is how many rows actually parsed.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_invitations"
            )
            logger.info("Reading %s invitations (start=%s)", direction, start)

            await ctx.report_progress(
                progress=0, total=100, message="Reading invitations"
            )

            result = await extractor.get_invitations(
                direction=direction,
                start=start,
                count=count,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_invitations")
        except Exception as e:
            raise_tool_error(e, "get_invitations")  # NoReturn
