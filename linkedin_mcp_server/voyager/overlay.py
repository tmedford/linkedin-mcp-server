"""Serve our API-reading tools in place of the page-scraping ones.

Installed once, after upstream has registered its own tools. For each tool we
supersede it removes upstream's registration and registers ours, under the SAME
name wherever ours can honour upstream's arguments, so a caller is offered one
way to do the job and does not have to learn a new name for it.

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
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    LinkedInScraperException,
)
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping.contracts import FilterValidationError
from linkedin_mcp_server.tools.person import StrList
from linkedin_mcp_server.voyager.person_message import refuse_an_invalid_person_message
from linkedin_mcp_server.voyager.thread_reply import refuse_an_invalid_reply

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
#:
#: ``get_conversation`` opens the thread and reads the rendered page, which
#: marks the thread read and returns one block of text with the page's chrome
#: in it. Ours issues the query the thread page itself loads from: each
#: message as a record, and the thread left unread. Lookup by username is kept,
#: done by searching messages for the member's name rather than by clicking
#: sidebar rows.
#:
#: ``search_conversations`` types into the messaging search box and reads the
#: render, which opens the first match and so marks it read. Ours issues the
#: keyword query that page loads its results from.
#:
#: ``send_message`` opens the recipient's profile, follows its Message action
#: to a composer and types. On 2026-10-02 that could not reach a first-degree
#: connection whose profile exposed no unambiguous Message action.
#: Ours resolves the member and posts to the messaging API, and was held
#: beside upstream's until its write had been sent live once.
#:
#: ``get_person_profile`` loads the profile page and one more page per section
#: and returns each as text. Ours reads the whole profile in one request as
#: records, with contact info, mutual connections and what the profile shares
#: with the signed-in member's own.
#:
#: ``search_people`` loads the results page and returns its text, ten at a
#: time. Ours asks the search service and returns each person as a record with
#: their identifier, up to fifty a page.
#:
#: **A replacement keeps the name it replaces.** Five of the six below are
#: registered under upstream's own tool name and accept upstream's arguments,
#: so nothing that calls the tool has to change when its implementation does.
#: ``get_inbox`` is the exception and predates the rule: its replacement pages
#: by cursor, which a ``limit`` argument cannot express, and callers moved to
#: ``get_conversations`` when it was introduced.
SUPERSEDED: dict[str, str] = {
    "get_inbox": "get_conversations",
    "get_conversation": "get_conversation",
    "search_conversations": "search_conversations",
    "send_message": "send_message",
    "get_person_profile": "get_person_profile",
    "search_people": "search_people",
    "connect_with_person": "connect_with_person",
    "search_jobs": "search_jobs",
    "get_job_details": "get_job_details",
    "get_saved_jobs": "get_saved_jobs",
    "get_my_profile": "get_my_profile",
    "get_company_profile": "get_company_profile",
    "get_company_posts": "get_company_posts",
    "get_company_employees": "get_company_employees",
    "search_companies": "search_companies",
    "search_posts": "search_posts",
    "get_feed": "get_feed",
    "get_sidebar_profiles": "get_sidebar_profiles",
}

#: The section names upstream's get_person_profile accepts.
_PERSON_SECTIONS = frozenset(
    {
        "experience",
        "education",
        "interests",
        "honors",
        "languages",
        "certifications",
        "skills",
        "projects",
        "contact_info",
        "posts",
    }
)


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

        `participants` is the names; `people` is the same people as records,
        each with public_identifier: pass that as linkedin_username to
        get_person_profile, send_message or connect_with_person.

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

            Each invitation carries public_identifier (pass it as
            linkedin_username to any person tool), name, headline, state,
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

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Conversation",
        # Reads the messaging API. The thread is never opened, so unlike the
        # upstream tool of this name it does not mark anything read.
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"messaging", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_conversation(
        ctx: Context,
        linkedin_username: str | None = None,
        thread_id: str | None = None,
        index: int = 0,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Read the recent messages of ONE LinkedIn messaging thread from the API.

        Provide either thread_id or linkedin_username. The thread is not
        opened, so it stays unread.

        Prefer thread_id when you have it: pass the `thread_url` a
        get_conversations or search_conversations row carries. By
        linkedin_username, the thread is found by searching messages for that
        member's name and keeping the conversations they are in; that finds
        the usual case and can miss a thread the search does not surface.

        Args:
            ctx: FastMCP context for progress reporting
            linkedin_username: The participant's /in/ public identifier or
                profile URL. Used only when thread_id is not given.
            thread_id: The thread to read: a `thread_url`, a
                `/messaging/thread/{id}/` reference, or the bare thread id.
            index: 0-based selector when the member is in several
                conversations with you. One-to-one threads come first, then
                group threads, each most recent first. Ignored when thread_id
                is provided.

        Returns:
            Dict with url and sections (conversation -> text), plus thread_id,
            thread_urn, participants, messages, count and query_id_renewed.

            `messages` is oldest first. Each carries message_urn, sender_name,
            from_me, delivered_at, delivered_at_iso, subject and text. from_me
            is None when the payload does not name the sender.

            **This is the recent tail of the thread, not its whole history**:
            at most the 20 most recent messages. `count` is how many came
            back, not how many exist.

            **`participants` is who has WRITTEN in the returned messages, not
            the thread's membership.** A group thread in which only you have
            written comes back with none. get_conversations carries the full
            membership of every thread.

            `query_id_renewed` is normally False. LinkedIn rotates the id of
            this query, and when the known one stops working the new one is
            learned by loading the messaging page once, the same page
            get_conversations loads. That call reports query_id_renewed: True.
        """
        if not linkedin_username and not thread_id:
            raise_tool_error(
                LinkedInScraperException(
                    "Provide at least one of linkedin_username or thread_id"
                ),
                "get_conversation",
            )
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_conversation"
            )
            logger.info(
                "Reading conversation: username=%s, thread=%s, index=%d",
                linkedin_username,
                bool(thread_id),
                index,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Loading conversation"
            )

            result = await extractor.get_thread(
                thread_id, linkedin_username=linkedin_username, index=index
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_conversation")
        except Exception as e:
            raise_tool_error(e, "get_conversation")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Reply to Thread",
        # A write through the messaging API. Unlike the two readers above it
        # sends, so it carries send_message's hints rather than theirs.
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"messaging", "actions"},
        exclude_args=["extractor"],
    )
    async def reply_to_thread(
        thread_id: str,
        message: str,
        confirm_send: bool,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Reply inside an EXISTING LinkedIn messaging thread, and nowhere else.

        Prefer this over send_message whenever the conversation already
        exists. send_message goes through the recipient's profile, which can
        be unavailable for an InMail or Open Profile contact and can open a
        separate DM instead of continuing the thread. This sends through
        LinkedIn's messaging API to the thread you name: no page is opened and
        nothing is typed, so a dry run does not mark the thread read.

        Args:
            thread_id: The thread to reply in. Pass the `thread_url` that
                get_conversations returned, a `/messaging/thread/{id}/`
                reference, or the bare thread id.
            message: Reply text. Line breaks (LF) are kept, so a greeting can
                sit on its own line. Other C0 control characters and DEL are
                rejected, including CR and tab.
            confirm_send: Must be True to send. False is a dry run: nothing is
                written, and the result shows what the thread currently holds
                so you can check it is the conversation you mean.
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, status, message, thread_id, recipient_selected,
            sent, and retry_safe.

            A dry run adds thread_readable, participants, last_message_text
            and last_message_at. thread_readable is True when the thread was
            read, False when it holds nothing for this account (status
            `thread_not_found`), and None when the preview itself was
            unavailable, which says nothing about the thread.

            `sent` is true only when LinkedIn answered the write with the
            message it created; message_urn and delivered_at come from that
            answer. It does not claim the recipient read it. `retry_safe` is
            false whenever the reply was or may have been delivered, and
            calling again while it is false can deliver the reply twice.
            `send_rejected` means LinkedIn refused the request and nothing was
            sent.
        """
        try:
            # Answered before a session is acquired, for send_message's reason:
            # acquiring one can spend a login attempt and come back as an
            # authentication error instead of the refusal the caller can act
            # on. Inside the `try` because an unusable thread_id raises.
            refusal = refuse_an_invalid_reply(thread_id, message)
            if refusal is not None:
                return refusal
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="reply_to_thread"
            )
            logger.info(
                "Replying to thread %s (confirm_send=%s)", thread_id, confirm_send
            )

            await ctx.report_progress(progress=0, total=100, message="Opening thread")

            result = await extractor.reply_to_thread(
                thread_id,
                message,
                confirm_send=confirm_send,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "reply_to_thread")
        except Exception as e:
            raise_tool_error(e, "reply_to_thread")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Conversations",
        # Reads the messaging API. Nothing is typed into the search box and no
        # result is opened, so no thread is marked read.
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"messaging", "scraping"},
        exclude_args=["extractor"],
    )
    async def search_conversations(
        keywords: str,
        ctx: Context,
        limit: int = 20,
        cursor: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Find conversations by keyword, ONE page (up to 20) from the messaging API.

        Searches the inbox, archive and spam together, the way the messaging
        page's own search does. No result is opened, so nothing is marked read.

        Args:
            keywords: The word or phrase to search for.
            ctx: FastMCP context for progress reporting
            limit: Kept for compatibility and NOT applied. A page is whatever
                LinkedIn returns for it, up to 20; cutting it shorter while
                the cursor moves on would silently skip the rest. Read
                `count` and page with `cursor`.
            cursor: next_cursor from a previous call. OMIT for the first page.

        Returns:
            Dict with url and sections (the standard scraping-tool shape), plus
            keywords, conversations, count, page_size, next_cursor, at_end,
            zero_reason and query_id_renewed.

            Each conversation has the same fields as a get_conversations row,
            including participants and thread_url; pass thread_url to
            get_conversation to read it. **The last_message_* fields are a message
            from that conversation, not necessarily the one that matched.**

            at_end is measured from the row count: True means fewer than
            page_size came back. **A next_cursor is not evidence of more** -- a
            two-match search was observed returning one. None means an empty
            page; zero_reason then says "no-matches" or "after-cursor".
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="search_conversations"
            )
            logger.info("Searching messages (cursor=%s)", bool(cursor))

            await ctx.report_progress(
                progress=0, total=100, message="Searching messages"
            )

            result = await extractor.search_messages(keywords, cursor=cursor)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "search_conversations")
        except Exception as e:
            raise_tool_error(e, "search_conversations")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Send Message",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"messaging", "actions"},
        exclude_args=["extractor"],
    )
    async def send_message(
        linkedin_username: str,
        message: str,
        confirm_send: bool,
        ctx: Context,
        profile_urn: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Send a message to a person through LinkedIn's messaging API.

        Use this to write to someone by their profile. If a one-to-one
        conversation with them already exists the message goes into it;
        otherwise a new one is opened. The result's thread_id says which
        conversation it landed in. When you already have the thread, prefer
        reply_to_thread, which is pinned to that exact conversation.

        Args:
            linkedin_username: The recipient's /in/ public identifier, or
                their profile URL. Several separated by commas address one
                group conversation with all of them.
            message: Message text. Line breaks (LF) are kept. Other C0 control
                characters and DEL are rejected, including CR and tab.
            confirm_send: Must be True to send. False is a dry run: the
                recipient is resolved and reported, and nothing is written.
            ctx: FastMCP context for progress reporting
            profile_urn: Optional. The member's profile URN (ACoAAB...) if
                you already know it; the send is refused unless it matches the
                member that linkedin_username resolves to. It never bypasses
                that lookup.

        Returns:
            Dict with url, status, message, recipient_selected, sent and
            retry_safe, plus recipients (urn and name of each) once resolved.
            With a single recipient, recipient_urn and recipient_name are set
            too.

            `sent` is true only when LinkedIn answered the write with the
            message it created. A sent result adds message_urn, delivered_at,
            thread_urn, thread_id and thread_url: the conversation the server
            says the message landed in, which is what reply_to_thread and
            get_conversation take. `retry_safe` is false whenever the message was or
            may have been delivered. `send_rejected` means LinkedIn refused the
            request and nothing was sent.
        """
        try:
            # Answered before a session is acquired; inside the `try` because
            # an unusable username raises.
            refusal = refuse_an_invalid_person_message(linkedin_username, message)
            if refusal is not None:
                return refusal
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="send_message"
            )
            logger.info(
                "Messaging %s (confirm_send=%s)", linkedin_username, confirm_send
            )

            await ctx.report_progress(
                progress=0, total=100, message="Resolving recipient"
            )

            result = await extractor.message_person(
                linkedin_username,
                message,
                confirm_send=confirm_send,
                profile_urn=profile_urn,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "send_message")
        except Exception as e:
            raise_tool_error(e, "send_message")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Person Profile",
        # Reads the profile API. No profile page is loaded.
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_person_profile(
        linkedin_username: str,
        ctx: Context,
        sections: str | None = None,
        max_scrolls: int | None = None,
        compare_to_me: bool = True,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Read a person's WHOLE profile from LinkedIn's API, and what you share.

        One request returns every section as structured records with ids and
        dates, rather than a page of text per section. Use it to understand
        who someone is and, above all, how the two of you are connected.

        Args:
            linkedin_username: The /in/ public identifier or a profile URL.
            ctx: FastMCP context for progress reporting
            sections: Comma-separated section names, kept for compatibility.
                Every profile section (experience, education, interests,
                honors, languages, certifications, skills, projects,
                contact_info) is ALWAYS returned, so naming them changes
                nothing. Only "posts" adds something: the member's ten most
                recent posts. Unrecognised names come back in
                unknown_sections.
            max_scrolls: Kept for compatibility and ignored. Nothing is
                scrolled; sections arrive whole from the API.
            compare_to_me: When true (the default), also reads your own profile
                once and returns `common_ground`. Pass false to skip it.

        Returns:
            Dict with url and sections (main_profile -> text, and posts when
            requested), plus:

            identity: name, headline, summary, public_identifier, profile_urn,
                location, industry.
            relationship: "connection" for a first-degree connection, "self"
                for your own profile, another LinkedIn label otherwise, or
                None when it could not be read.
            contact: what the member shares with you: email, phones, websites,
                twitter, messengers, address, birthday. Only fields they share
                appear. {} means the read worked and nothing is shared; None
                means the read failed.
            mutual_connections (omitted for your own profile): {items,
                returned, start, total, complete}. The people you are both
                connected to, which is who could introduce you. Each has name,
                headline, public_identifier, profile_urn and LinkedIn's own
                suggested_ask. Up to 40 are included; when `complete` is false
                call get_mutual_connections for the rest.
            positions, education, skills, certifications, honors, languages,
                organizations, volunteering, projects, publications, patents,
                courses, test_scores: each is {items, returned, total,
                complete}. Positions carry title, company, company_id, start,
                end, location and description, one entry per title held.
                company_id is what search_people(current_company=...),
                search_jobs(company_id=...) and get_profile_views(company_id=...)
                take.
            incomplete_sections: names of sections where the server returned
                fewer than it has. **Skills are capped at 20**, so skills is
                usually listed; every other section normally comes back whole.

            common_ground (omitted for your own profile):
                worked_together_by_employer: one line per employer you were
                    both at at the same time: company, start, end and months.
                    **Read this first.** It is the strongest signal.
                worked_together: the same, title by title, with both
                    people's entries and the overlapping span of each pair.
                companies: every shared employer, with both people's entries
                    and `overlap` (None if the dates never met).
                schools: shared schools, same shape.
                organizations, volunteering, certification_authorities,
                languages, skills: shared names. **A skill missing here is not
                evidence it is not shared**, since both lists are capped.
                same_location: whether both profiles name the same location.

            Employers and schools are matched by LinkedIn's id for them
            (`matched_by: "id"`); a name is used only when one side typed the
            place in free text (`matched_by: "name"`).

            Ask for "posts" in sections for the ten most recent, or call
            get_person_posts to page through them.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_person_profile"
            )
            logger.info("Reading person %s", linkedin_username)

            await ctx.report_progress(progress=0, total=100, message="Reading profile")

            result = await extractor.get_person(
                linkedin_username, compare_to_me=compare_to_me
            )

            requested = {
                name.strip().lower()
                for name in (sections or "").split(",")
                if name.strip()
            }
            unknown = sorted(requested - _PERSON_SECTIONS)
            if unknown:
                result["unknown_sections"] = unknown
            if "posts" in requested:
                posts = await extractor.get_person_posts(linkedin_username, count=10)
                result["sections"]["posts"] = posts["sections"]["posts"]
                result["posts"] = posts["posts"]
                result["posts_next_cursor"] = posts["next_cursor"]

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_person_profile")
        except Exception as e:
            raise_tool_error(e, "get_person_profile")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Mutual Connections",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_mutual_connections(
        linkedin_username: str,
        ctx: Context,
        start: int = 0,
        count: int = 40,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Read ONE page of the connections you share with a person.

        These are the people who could introduce you. get_person_profile already
        returns the first 40 with the total; use this to page through the rest,
        or when the mutual connections are all you need.

        Args:
            linkedin_username: The /in/ public identifier or a profile URL.
            ctx: FastMCP context for progress reporting
            start: 0-based offset. Paging is the caller's loop.
            count: how many to ask for.

        Returns:
            Dict with url and sections (the standard scraping-tool shape), plus
            mutual_connections, count, start, page_size, total and at_end.

            Each connection has name, headline, public_identifier, profile_urn,
            distance, and suggested_ask: the text LinkedIn itself pre-fills
            when you ask that person for an introduction.

            total is LinkedIn's own count of mutual connections. at_end is
            measured against it: True when this page reaches the total, False
            when there are more, and None for an empty page.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_mutual_connections"
            )
            logger.info("Reading mutual connections (start=%s)", start)

            await ctx.report_progress(
                progress=0, total=100, message="Reading mutual connections"
            )

            result = await extractor.get_mutual_connections(
                linkedin_username, start=start, count=count
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_mutual_connections")
        except Exception as e:
            raise_tool_error(e, "get_mutual_connections")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Person Posts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_person_posts(
        linkedin_username: str,
        ctx: Context,
        count: int = 10,
        cursor: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Read ONE page of a person's posts and reposts from LinkedIn's API.

        Use it to see what someone has been saying lately before you write to
        them.

        Args:
            linkedin_username: The /in/ public identifier or a profile URL.
            ctx: FastMCP context for progress reporting
            count: how many to ask for.
            cursor: next_cursor from a previous call. OMIT for the first page.
                This endpoint pages only by cursor; there is no offset.

        Returns:
            Dict with url and sections (the standard scraping-tool shape), plus
            posts, count, page_size, next_cursor and at_end.

            Each post has activity_urn, url, posted_at_iso, author, text, and
            likes, comments and shares where LinkedIn returned them.

            Three kinds of entry come back and they read differently:
            - their own post: `author` is them and `text` is what they wrote.
            - a reshare with their comment: `text` is their comment, and
              reshared_author and reshared_text are the original.
            - a plain repost: repost_header says so, and `author`, `text` and
              **posted_at_iso are the ORIGINAL's**, not the repost's.

            posted_at_iso is read from the activity id, which encodes its
            creation time. at_end is True when fewer than `count` came back,
            False for a full page, and None for an empty page.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_person_posts"
            )
            logger.info("Reading posts (cursor=%s)", bool(cursor))

            await ctx.report_progress(progress=0, total=100, message="Reading posts")

            result = await extractor.get_person_posts(
                linkedin_username, count=count, cursor=cursor
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_person_posts")
        except Exception as e:
            raise_tool_error(e, "get_person_posts")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Search People",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "search"},
        exclude_args=["extractor"],
    )
    async def search_people(
        keywords: str,
        ctx: Context,
        location: str | None = None,
        network: StrList | None = None,
        current_company: str | None = None,
        start: int = 0,
        count: int = 10,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Search for people on LinkedIn, ONE page from LinkedIn's search API.

        Each person comes back as a record with their public identifier, so a
        result can be passed straight to get_person_profile.

        Args:
            keywords: Search keywords (e.g., "software engineer", "recruiter").
            ctx: FastMCP context for progress reporting
            location: Optional place name (e.g., "New York", "Germany"), or a
                numeric LinkedIn geo id. A name is resolved to LinkedIn's best
                matching place, which is reported back as location_resolved
                with the runners-up in location_candidates. **Check it**: "New
                York" resolves to the state before the city. A name LinkedIn
                does not recognise as a place is refused, not ignored.
            network: Optional connection-degree filter. Each element is one of
                "F" (1st-degree), "S" (2nd-degree), "O" (3rd-degree and beyond).
                A single
                token ("F") or a comma-separated string ("F,S") is also
                accepted, for clients that cannot transmit an array.
            current_company: Optional current-employer filter, as the numeric
                company id (e.g. "1115" for SAP). A company name is refused,
                because LinkedIn ignores one and returns everyone.
            start: 0-based offset. Paging is the caller's loop.
            count: how many to ask for, 1 to 50. Defaults to 10.

        Returns:
            Dict with url, sections (search_results -> text) and references
            (the standard shape), plus people, count, start, page_size,
            total_reported and at_end.

            Each person has name, headline, location, public_identifier,
            profile_url, profile_urn, distance (LinkedIn's DISTANCE_1/2/3) and
            degree ("1st"/"2nd"/"3rd"), plus insight (such as mutual
            connections) and summary where LinkedIn returned them.

            at_end is True when fewer than `count` came back, False for a full
            page, None for an empty one. **total_reported is not a count of
            matches**: it read 150 for every query tried, so nothing should be
            concluded from it.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="search_people"
            )
            logger.info(
                "Searching people: keywords='%s', location='%s', network=%s, "
                "current_company='%s', start=%s",
                keywords,
                location,
                network,
                current_company,
                start,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Starting people search"
            )

            try:
                result = await extractor.find_people(
                    keywords,
                    location,
                    network=network,
                    current_company=current_company,
                    start=start,
                    count=count,
                )
            except FilterValidationError as e:
                # Carries the correction; surfaced whole rather than masked.
                raise ToolError(str(e)) from e

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except ToolError:
            raise
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "search_people")
        except Exception as e:
            raise_tool_error(e, "search_people")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Profile Views",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_profile_views(
        ctx: Context,
        full: bool = True,
        days: int | None = None,
        interesting: str | None = None,
        company_id: str | None = None,
        industry_id: str | None = None,
        geo_id: str | None = None,
        sort: str = "recent",
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Read who viewed YOUR profile: every viewer LinkedIn lists, newest first.

        Pages through LinkedIn's own viewer-list endpoint, 40 at a time. The
        default period takes about 15 seconds and a year about 40.

        Args:
            ctx: FastMCP context for progress reporting
            full: True (the default) reads the whole list. False asks only the
                quick JSON endpoint, which returns the six most recent viewers
                plus LinkedIn's highlighted groups, in a second or two.
            days: The period the list covers: 7, 14, 28, 90 or 365. OMIT for
                LinkedIn's default. Needs full=True. A longer period is a
                longer list and takes longer to read.
            interesting: Only LinkedIn's "interesting viewers" of one kind:
                "can_help_you_get_a_job", "senior_leader_in_your_industry",
                "senior_leader_with_your_job_function" or "has_verifications".
            company_id: Only viewers at this company, by LinkedIn's numeric
                company id (e.g. "229978"). A URN or a name is refused.
            industry_id: Only viewers in this industry, by numeric id.
            geo_id: Only viewers in this place, by numeric geo id (the
                geo_id search_people reports in location_resolved).

            sort: "recent" (default, newest first) or "relevant", LinkedIn's
                "Sort by most relevant", kept in LinkedIn's order. Needs
                full=True.

            The filters combine, and all need full=True. A private viewer's
            row carries company_id, industry_id and geo_id where LinkedIn
            shows them, which is where to find the ids to filter on.

        Returns:
            Dict with url and sections (the standard scraping-tool shape), plus:

            total_views, time_frame, change_percent: LinkedIn's own count for
                the period (e.g. 529 in LAST_90_DAYS) and its change.
            viewers: identified people, newest first. Each has name, headline,
                public_identifier and degree, plus viewed_text ("Viewed 1w
                ago") and viewed_at_iso. Pass public_identifier to
                get_person_profile.
            anonymous_viewers: private-mode viewers, as LinkedIn describes
                them ("Recruiter at DualEntry"). LinkedIn hides who they are
                but usually not where they work: each has title, and company
                and company_id when it names the employer, or industry_id and
                geo_id when it gives only an industry and place, or school
                when a school is all it names. search_url is
                LinkedIn's own search for people matching that description.
            aggregates: LinkedIn's roll-ups, such as "133 recruiters viewed
                your profile".
            groups: how LinkedIn grouped the highlights, with view counts.
            count, returned, complete, days, filters and period_applied.

            With a filter on, the result is the filtered list only; the
            highlighted groups are still reported but are unfiltered.

            **Check period_applied when you pass days.** False means a row
            came back older than the period allows, so LinkedIn ignored it.
            True means nothing contradicted the period asked for.

            **Most view times are approximate.** LinkedIn's list says "1w ago",
            so viewed_at_iso is computed from that and the row carries
            viewed_at_approximate: true. Viewers that the JSON endpoint also
            returns have the exact time, and also referrer, pending_invite and
            notable_reason. `extra` holds anything else on the row, such as
            "2 mutual connections".

            complete is True when the list was read to its end, False when the
            request limit cut it short, and None with full=False. **total_views counts
            views, not people**: one run returned 117 named and 95 private
            viewers against 529 views, the rest being repeat views and the
            recruiters LinkedIn only reports as a number.

            Recruiter views are a separate page: use get_recruiter_views.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_profile_views"
            )
            logger.info("Reading profile views")

            await ctx.report_progress(
                progress=0, total=100, message="Reading profile views"
            )

            result = await extractor.get_profile_views(
                full=full,
                days=days,
                interesting=interesting,
                company_id=company_id,
                industry_id=industry_id,
                geo_id=geo_id,
                sort=sort,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_profile_views")
        except Exception as e:
            raise_tool_error(e, "get_profile_views")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Recruiter Views",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "jobs", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_recruiter_views(
        ctx: Context,
        days: int | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Read which recruiters viewed YOUR profile, by company, newest first.

        This is LinkedIn's Premium "Recruiter insights" list, a separate page
        from get_profile_views. LinkedIn names the recruiter's company but not
        the recruiter. Use it for job sourcing: a recruiter who looked at you
        at a company with open roles is a warm lead, and LinkedIn flags where
        you "would be a top applicant".

        Args:
            ctx: FastMCP context for progress reporting
            days: The period: 7, 14, 28, 90 or 365. Omit for 90.

        Returns:
            Dict with url and sections (the standard scraping-tool shape), plus:

            recruiters: one entry per view, newest first. Each has description
                ("Recruiter at Rippling"), company and company_id, industry
                when LinkedIn shows it, viewed_text and an approximate
                viewed_at_iso, and insight: LinkedIn's note such as "You'd be a
                top applicant for 6 roles" or "Multiple recruiters from this
                company are engaging with your profile".
                has_jobs is True when LinkedIn offers that company's jobs:
                jobs_url opens them, and job_id is the role LinkedIn puts
                first. Pass company_id to search_jobs-style tools or to
                get_profile_views(company_id=...) to see who else from there
                looked. Without jobs, company_insights_url is given instead.
            aggregates: LinkedIn's rollups closing the list, such as "38 other
                recruiters", for views it does not itemise.
            count, with_jobs, complete (False only if the request limit cut
                the list short) and days.

            The same company appears once per view, so several rows for one
            company mean several recruiters or visits.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_recruiter_views"
            )
            logger.info("Reading recruiter views")

            await ctx.report_progress(
                progress=0, total=100, message="Reading recruiter views"
            )

            result = await extractor.get_recruiter_views(days=days)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_recruiter_views")
        except Exception as e:
            raise_tool_error(e, "get_recruiter_views")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Connect With Person",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"person", "actions"},
        exclude_args=["extractor"],
    )
    async def connect_with_person(
        linkedin_username: str,
        ctx: Context,
        note: str | None = None,
        dry_run: bool = False,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Send a LinkedIn connection request or accept an incoming one.

        A request is sent through LinkedIn's API and confirmed by reading the
        relationship back; no page is opened. Without a note it is the
        Connect action LinkedIn's own buttons send. With a note it is the
        call the custom-invite dialog makes, and the note is read back from
        the invitation to set note_sent.

        **Accepting is the one case still done on the page.** When the
        member has already invited you, their invitation is accepted, as
        upstream's tool of this name always has: the profile page is opened,
        re-checked to still show the incoming request, and Accept clicked;
        the result is read back from the API (status accepted). If the
        invitation is gone by then, nothing is clicked or sent (status
        invitation_gone). No API accept has been measured yet. A dry run
        reports invitation_received and does nothing.

        The tool is annotated with destructiveHint so MCP clients will
        prompt for user confirmation before execution.

        Args:
            linkedin_username: LinkedIn username (e.g., "stickerdaniel", "williamhgates"). A full profile URL is accepted too and is reduced to the username.
            ctx: FastMCP context for progress reporting
            note: Optional note to include with the invitation, up to 300
                characters (Premium; LinkedIn allows free accounts fewer and
                refuses the rest at send time).
            dry_run: True reads the relationship and returns the request
                that would be sent, without sending it.

        Returns:
            Dict with url, status, message, and note_sent.
            status is pending (sent, and confirmed by reading
            the relationship back, or already pending), already_connected,
            connect_unavailable, send_failed, send_unconfirmed (LinkedIn
            answered 200 but the relationship did not change: check sent
            invitations before retrying) or dry_run. relationship_before and
            relationship_after name the states read: not_invited,
            invited_by_me, invited_by_them, connected or self. note_sent is
            True only when
            the note read back from the invitation matches the one sent. A
            refused note (quota, length) is send_failed with LinkedIn's
            answer in response_excerpt.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="connect_with_person"
            )
            logger.info(
                "Connecting with person: %s (note=%s, dry_run=%s)",
                linkedin_username,
                note is not None,
                dry_run,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Sending connection request"
            )

            result = await extractor.invite_person(
                linkedin_username, note=note, dry_run=dry_run
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "connect_with_person")
        except Exception as e:
            raise_tool_error(e, "connect_with_person")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Jobs",
        # Reads the job-search API. Unlike the page, nothing is written to the
        # member's job-search history.
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"job", "search"},
        exclude_args=["extractor"],
    )
    async def search_jobs(
        keywords: str,
        ctx: Context,
        location: str | None = None,
        max_pages: Annotated[int, Field(ge=1, le=10)] = 3,
        date_posted: str | None = None,
        job_type: str | None = None,
        experience_level: str | None = None,
        work_type: str | None = None,
        easy_apply: bool = False,
        sort_by: str | None = None,
        company_id: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Search for jobs on LinkedIn, from LinkedIn's job-search API.

        Each job comes back as a record. Returns job_ids that can be passed to
        get_job_details for the full posting.

        Args:
            keywords: Search keywords (e.g., "vice president product"). May
                be "" when company_id is given, to list that company's jobs.
            ctx: FastMCP context for progress reporting
            location: Optional place name (e.g., "New York") or numeric geo
                id. A name is resolved to LinkedIn's best matching place and
                reported back as location_resolved, with the runners-up in
                location_candidates. A name LinkedIn does not know as a place
                is refused: for remote work use work_type="remote".
            max_pages: Pages of 25 to read (1-10, default 3).
            date_posted: past_hour, past_24_hours, past_week or past_month.
            job_type: Comma-separated: full_time, part_time, contract,
                temporary, volunteer, internship, other.
            experience_level: Comma-separated: internship, entry, associate,
                mid_senior, director, executive.
            work_type: Comma-separated: on_site, remote, hybrid.
            easy_apply: Only Easy Apply jobs (default false).
            sort_by: date or relevance.
            company_id: Only jobs at this company, by LinkedIn's numeric
                company id; several comma-separated. get_recruiter_views gives
                the id of every company whose recruiters viewed you, so this is
                how to pull the roles behind "You'd be a top applicant".
                keywords can then be broad (e.g. "product").

            An unknown filter value is refused, naming the accepted ones:
            LinkedIn ignores one and answers unfiltered.

        Returns:
            Dict with url, sections (search_results -> text) and job_ids (the
            standard shape), plus jobs, count, total and complete.

            Each job has job_id, title, company, company_id, location,
            listed_at_iso, easy_apply, promoted, url, and where LinkedIn shows
            them insight ("You'd be a top applicant", "21 connections work
            here") and detail (benefits or salary).

            total is LinkedIn's count of matches; it moves with every filter.
            complete is True when the last page was reached within max_pages.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="search_jobs"
            )
            logger.info(
                "Searching jobs: keywords='%s', location='%s', max_pages=%d",
                keywords,
                location,
                max_pages,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Starting job search"
            )

            result = await extractor.find_jobs(
                keywords,
                location,
                max_pages=max_pages,
                date_posted=date_posted,
                job_type=job_type,
                experience_level=experience_level,
                work_type=work_type,
                easy_apply=easy_apply,
                sort_by=sort_by,
                company_id=company_id,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "search_jobs")
        except Exception as e:
            raise_tool_error(e, "search_jobs")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Job Details",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"job", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_job_details(
        job_id: str,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get one job posting, whole, from LinkedIn's API.

        Args:
            job_id: LinkedIn job ID (e.g., "4252026496"), as search_jobs or
                get_recruiter_views return it, or from /jobs/view/<id>/.
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url and sections (job_posting -> text), plus job:
            job_id, title, company, company_id, company_url, company_size,
            location, workplace (on_site/remote/hybrid), employment_status,
            experience_level, industries, job_functions, listed_at_iso,
            original_listed_at_iso, expire_at_iso, closed_at_iso, job_state
            (LISTED, CLOSED...), applies, views, easy_apply, apply_url (the
            company's own site when it is not Easy Apply) and description.

            applies and views read 0 on every posting checked by someone other
            than its poster, so treat them as unknown rather than as none.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_job_details"
            )
            logger.info("Reading job: %s", job_id)

            await ctx.report_progress(progress=0, total=100, message="Reading job")

            result = await extractor.get_job(job_id)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_job_details")
        except Exception as e:
            raise_tool_error(e, "get_job_details")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Saved Jobs",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"job", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_saved_jobs(
        ctx: Context,
        max_pages: Annotated[int, Field(ge=1, le=10)] = 3,
        stage: str = "saved",
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        List the jobs in your LinkedIn jobs tracker, by stage.

        Saved jobs are the default. Returns job_ids that can be passed to
        get_job_details for the full posting, and each job as a record.

        Args:
            ctx: FastMCP context for progress reporting
            max_pages: Kept for compatibility and not used: a stage arrives in
                one answer.
            stage: Which tab of the tracker: "saved" (default), "draft" or
                "clicked_apply" (LinkedIn shows these two together as In
                Progress), "applied", "interview" or "archived".

        Returns:
            Dict with url, sections (saved_jobs -> text) and job_ids (the
            standard shape), plus jobs, count and stage.

            Each job has job_id, title, company, location, workplace
            (On-site / Remote / Hybrid as LinkedIn words it), listed_at_iso,
            original_listed_at_iso (earlier when the job was reposted), stage,
            verified, url, and note when you wrote one on it.

            Pass a job_id to get_job_details for the description, apply link
            and company_id; pass that company_id to search_people or
            search_jobs.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_saved_jobs"
            )
            logger.info("Reading jobs tracker (stage=%s)", stage)

            await ctx.report_progress(
                progress=0, total=100, message="Reading saved jobs"
            )

            result = await extractor.saved_jobs(max_pages=max_pages, stage=stage)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_saved_jobs")
        except Exception as e:
            raise_tool_error(e, "get_saved_jobs")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get My Profile",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_my_profile(
        ctx: Context,
        sections: str | None = None,
        max_scrolls: Annotated[int, Field(ge=1, le=50)] | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Read YOUR OWN LinkedIn profile, whole, from LinkedIn's API.

        The same record get_person_profile returns for anyone else, for the
        signed-in member: no username is needed.

        Args:
            ctx: FastMCP context for progress reporting
            sections: Comma-separated section names, kept for compatibility.
                Every profile section is ALWAYS returned, so naming them
                changes nothing. Only "posts" adds something: your ten most
                recent posts. Unrecognised names come back in
                unknown_sections.
            max_scrolls: Kept for compatibility and ignored.

        Returns:
            Dict with url (your real profile URL) and sections (main_profile
            -> text), plus identity (with public_identifier and profile_urn),
            contact, positions, education, skills, certifications and the
            other sections, each {items, returned, total, complete}, as in
            get_person_profile. relationship is "self"; there are no mutual
            connections or common_ground for your own profile.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_my_profile"
            )
            logger.info("Reading own profile")

            await ctx.report_progress(progress=0, total=100, message="Reading profile")

            result = await extractor.my_person()

            requested = {
                name.strip().lower()
                for name in (sections or "").split(",")
                if name.strip()
            }
            unknown = sorted(requested - _PERSON_SECTIONS)
            if unknown:
                result["unknown_sections"] = unknown
            if "posts" in requested:
                # A profile with no public identifier is still read by id.
                identity = result.get("identity") or {}
                own = (
                    identity.get("public_identifier")
                    or (identity.get("profile_urn") or "").rsplit(":", 1)[-1]
                )
                posts = await extractor.get_person_posts(own, count=10)
                result["sections"]["posts"] = posts["sections"]["posts"]
                result["posts"] = posts["posts"]
                result["posts_next_cursor"] = posts["next_cursor"]

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_my_profile")
        except Exception as e:
            raise_tool_error(e, "get_my_profile")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Company Profile",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"company", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_company_profile(
        company_name: str,
        ctx: Context,
        sections: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get a company's LinkedIn profile from LinkedIn's API.

        Args:
            company_name: LinkedIn company name from its URL (e.g., "docker",
                "anthropic"). A full company URL is accepted too.
            ctx: FastMCP context for progress reporting
            sections: Comma-separated extras: "posts" (its ten most recent
                posts) and "jobs" (its open jobs, first page). The company
                itself is always returned. Unrecognised names come back in
                unknown_sections.

        Returns:
            Dict with url and sections (about -> text, plus posts and jobs
            when asked), and:

            company: name, universal_name, company_id, tagline, description,
                website, industries, staff_count, staff_range, headquarters,
                founded_year, company_type, specialities, followers, url.
            company_id: the numeric id, also at the top level. Pass it to
                search_people(current_company=...), search_jobs(company_id=...)
                or get_profile_views(company_id=...).
            posts / jobs: records, when those sections were asked for, as
                get_company_posts and search_jobs return them.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_company_profile"
            )
            await ctx.report_progress(progress=0, total=100, message="Reading")

            result = await extractor.company_record(company_name)
            requested = {
                name.strip().lower()
                for name in (sections or "").split(",")
                if name.strip()
            }
            unknown = sorted(requested - {"posts", "jobs"})
            if unknown:
                result["unknown_sections"] = unknown
            if "posts" in requested:
                posts = await extractor.company_posts(company_name, count=10)
                result["sections"]["posts"] = posts["sections"]["posts"]
                result["posts"] = posts["posts"]
            if "jobs" in requested and result.get("company_id"):
                jobs = await extractor.find_jobs(
                    "", company_id=result["company_id"], max_pages=1
                )
                result["sections"]["jobs"] = jobs["sections"]["search_results"]
                result["jobs"] = jobs["jobs"]
                result["job_ids"] = jobs["job_ids"]
                result["jobs_total"] = jobs["total"]

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_company_profile")
        except Exception as e:
            raise_tool_error(e, "get_company_profile")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Company Posts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"company", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_company_posts(
        company_name: str,
        ctx: Context,
        count: int = 10,
        start: int = 0,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get ONE page of a company's posts from LinkedIn's API.

        Args:
            company_name: LinkedIn company name from its URL, or a company URL.
            ctx: FastMCP context for progress reporting
            count: how many to ask for, 1 to 50. Defaults to 10.
            start: 0-based offset. Paging is the caller's loop.

        Returns:
            Dict with url and sections (posts -> text), plus posts, count,
            start, page_size, total (LinkedIn's count) and at_end.

            Each post has url, posted_at_iso, author, text and, where LinkedIn
            returned them, likes, comments and shares. A repost carries
            repost_header and the original's author and text.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_company_posts"
            )
            await ctx.report_progress(progress=0, total=100, message="Reading")

            result = await extractor.company_posts(
                company_name, count=count, start=start
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_company_posts")
        except Exception as e:
            raise_tool_error(e, "get_company_posts")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Company Employees",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"company", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_company_employees(
        company_name: str,
        ctx: Context,
        keywords: str | None = None,
        start: int = 0,
        count: int = 12,
        schools: list[str] | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        List people at a company and its demographics, from LinkedIn's API.

        The demographics are what this tool adds over search_people: where
        employees live, where they studied, what they do, their skills and
        fields of study, each with a count, plus how many are 1st, 2nd and
        3rd degree to you.

        Args:
            company_name: LinkedIn company name from its URL, or a company URL.
            ctx: FastMCP context for progress reporting
            keywords: Optional filter by name, title or skill.
            start: 0-based offset. Paging is the caller's loop.
            count: how many people to ask for, 1 to 50. Defaults to 12.
            schools: Optional LinkedIn school ids. Keeps only people who
                studied at any of them, whatever their degree to you: the
                alumni of your schools at this company. An id comes from
                demographics.schools here or the people tab's facetSchool,
                not from a profile's education, which numbers schools
                differently.

        Returns:
            Dict with url, sections (employees -> text) and references (the
            standard shape), plus:

            people: records with name, headline, location, public_identifier
                (pass to get_person_profile), profile_urn, degree and insight.
            demographics: locations, schools, functions, skills,
                fields_of_study and degrees, each a list of {name, count, id}.
                A location id is a geo id search_people and get_profile_views
                take.
            company_id, count, start, page_size, total (LinkedIn's count of
            people there) and at_end.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_company_employees"
            )
            await ctx.report_progress(progress=0, total=100, message="Reading")

            result = await extractor.company_people(
                company_name,
                keywords=keywords,
                start=start,
                count=count,
                schools=schools,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_company_employees")
        except Exception as e:
            raise_tool_error(e, "get_company_employees")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Companies",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"company", "search"},
        exclude_args=["extractor"],
    )
    async def search_companies(
        keywords: str,
        ctx: Context,
        start: int = 0,
        count: int = 10,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Search for companies on LinkedIn, ONE page from LinkedIn's search API.

        Args:
            keywords: Search keywords (e.g., "fintech", "anthropic").
            ctx: FastMCP context for progress reporting
            start: 0-based offset. Paging is the caller's loop.
            count: how many to ask for, 1 to 50. Defaults to 10.

        Returns:
            Dict with url, sections (search_results -> text) and references
            (the standard shape), plus companies, count, start, page_size and
            at_end.

            Each company has name, company_id (what every company filter
            takes), universal_name (what get_company_profile takes), detail
            (industry and place), followers_text, summary and url.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="search_companies"
            )
            await ctx.report_progress(progress=0, total=100, message="Reading")

            result = await extractor.find_companies(keywords, start=start, count=count)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "search_companies")
        except Exception as e:
            raise_tool_error(e, "search_companies")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Posts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"post", "search"},
        exclude_args=["extractor"],
    )
    async def search_posts(
        keywords: str,
        ctx: Context,
        date_posted: str | None = None,
        max_pages: Annotated[int, Field(ge=1, le=10)] = 3,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Search LinkedIn posts globally by keyword, through LinkedIn's own
        results pager. Unlike the page, nothing is added to your search
        history.

        Use this to catch informal hiring posts ("we're hiring", "join our
        team") that often appear before a formal job listing exists.

        Args:
            keywords: Search keywords (e.g., "AI automation hiring").
            ctx: FastMCP context for progress reporting
            date_posted: Optional recency filter: "past-24h", "past-week" or
                "past-month" (the "past_24_hours" / "past_week" /
                "past_month" spellings are accepted too). Anything else is
                refused.
            max_pages: Pages of 10 posts to read (1-10, default 3).

        Returns:
            Dict with url, sections (search_results -> text) and references
            (the standard shape), plus posts, count and complete.

            Each post has author, text, url (its permalink), posted_at_iso
            (exact, from the post's id) and posted_text ("3d").
            author_public_identifier is set when the author is a member: pass
            it to get_person_profile. A company page's post has author_slug
            instead.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="search_posts"
            )
            await ctx.report_progress(progress=0, total=100, message="Reading")

            try:
                result = await extractor.find_posts(
                    keywords, date_posted=date_posted, max_pages=max_pages
                )
            except FilterValidationError as e:
                raise ToolError(str(e)) from e

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "search_posts")
        except Exception as e:
            raise_tool_error(e, "search_posts")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Feed",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"feed", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_feed(
        ctx: Context,
        num_posts: Annotated[int, Field(ge=1, le=50)] = 10,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get posts from your LinkedIn home feed, from LinkedIn's API.

        Args:
            ctx: FastMCP context for progress reporting
            num_posts: How many feed entries to ask for (1-50, default 10).
                Promoted entries are dropped, so fewer can come back.

        Returns:
            Dict with url, sections (feed -> text) and references["feed"]
            (every entry kind "feed_post", relative url), plus posts and count.

            Each post has url, posted_at_iso, author, text and, where LinkedIn
            returned them, likes, comments and shares. repost_header says why
            it is in your feed when someone you follow liked or reposted it
            ("Luan Lam likes this").
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_feed"
            )
            await ctx.report_progress(progress=0, total=100, message="Reading")

            result = await extractor.home_feed(num_posts=num_posts)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_feed")
        except Exception as e:
            raise_tool_error(e, "get_feed")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Sidebar Profiles",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_sidebar_profiles(
        linkedin_username: str,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get the profiles LinkedIn suggests beside a person's profile.

        Reads the two sidebar sections through the requests the profile page
        makes for them, without opening the page.

        Args:
            linkedin_username: The /in/ public identifier or a profile URL.
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url and sidebar_profiles mapping section key to a list of
            /in/username/ paths: "more_profiles_for_you" and
            "people_you_may_know". Only sections LinkedIn returned people for
            are included.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_sidebar_profiles"
            )
            await ctx.report_progress(progress=0, total=100, message="Reading")

            result = await extractor.sidebar_people(linkedin_username)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_sidebar_profiles")
        except Exception as e:
            raise_tool_error(e, "get_sidebar_profiles")  # NoReturn
