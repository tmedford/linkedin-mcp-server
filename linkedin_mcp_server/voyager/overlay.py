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
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

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
                complete}. Positions carry title, company, company_urn, start,
                end, location and description, one entry per title held.
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
            (`matched_by: "urn"`); a name is used only when one side typed the
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
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Read who viewed YOUR profile: every viewer LinkedIn lists, newest first.

        Runs in the server's own browser, not yours. Takes a minute or two,
        because the full list is read by letting LinkedIn's page load it.

        Args:
            ctx: FastMCP context for progress reporting
            full: True (the default) reads the whole list. False asks only the
                quick JSON endpoint, which returns the six most recent viewers
                plus LinkedIn's highlighted groups, in a second or two.

        Returns:
            Dict with url and sections (the standard scraping-tool shape), plus:

            total_views, time_frame, change_percent: LinkedIn's own count for
                the period (e.g. 529 in LAST_90_DAYS) and its change.
            viewers: identified people, newest first. Each has name, headline,
                public_identifier and degree, plus viewed_text ("Viewed 1w
                ago") and viewed_at_iso. Pass public_identifier to
                get_person_profile.
            anonymous_viewers: private-mode viewers, as LinkedIn describes
                them ("Recruiter at DualEntry").
            aggregates: LinkedIn's roll-ups, such as "133 recruiters viewed
                your profile".
            groups: how LinkedIn grouped the highlights, with view counts.
            count, returned, complete.

            **Most view times are approximate.** LinkedIn's list says "1w ago",
            so viewed_at_iso is computed from that and the row carries
            viewed_at_approximate: true. Viewers that the JSON endpoint also
            returns have the exact time, and also referrer, pending_invite and
            notable_reason. `extra` holds anything else on the row, such as
            "2 mutual connections".

            complete is True when the list was read to its end, False when the
            walk was cut short, and None with full=False. **total_views counts
            views, not people**: one run returned 117 named and 95 private
            viewers against 529 views, the rest being repeat views and the
            recruiters LinkedIn only reports as a number.

            Recruiter views are a separate page and are not included.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_profile_views"
            )
            logger.info("Reading profile views")

            await ctx.report_progress(
                progress=0, total=100, message="Reading profile views"
            )

            result = await extractor.get_profile_views(full=full)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_profile_views")
        except Exception as e:
            raise_tool_error(e, "get_profile_views")  # NoReturn
