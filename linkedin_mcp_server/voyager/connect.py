"""Send a connection request through LinkedIn's own action, not its page.

Upstream's ``connect_with_person`` opens the profile, classifies its buttons,
and drives the ``/preload/custom-invite/`` page. This sends the request the way
LinkedIn's own Connect button does, and reads the relationship before and after
from the API. Measured on 2026-10-02, one account:

- **The state is structural.** ``identity/dash/profiles`` with the
  ``WebTopCardCore`` decoration carries a ``MemberRelationship`` whose union is
  ``self``, ``connection`` or ``noConnection``; ``noConnection`` holds an
  ``invitationUnion`` that was ``noInvitation`` for a member never invited. No
  label is read, so nothing here depends on the account's language.
- **The send is a server action.** A Connect button in the server-rendered
  viewer list carries ``ServerRequest`` ``addaAddConnection``, whose payload is
  the invitee's member id, profile URN, names and profile URL, plus keys
  naming client-side state. Every value is derivable from the profile read,
  except a picture payload that only draws the post-send confirmation and is
  not sent here. It is POSTed to ``rsc-action/actions/server-request`` inside
  the envelope the page uses for every server action, observed on three other
  actions the same day.
- **On the profile page, Connect does not send.** It navigates to
  ``/preload/custom-invite/`` (the route upstream drives) and loads a drawer.
  The one-shot action exists only where LinkedIn renders a list of people, so
  this sends it as the viewer list does: with that page's headers, its screen,
  and ``clientContext: "Wvmp"``, the only value of it observed.
- **Success is read back, not inferred.** The answer is a component stream;
  what counts is the relationship afterwards. A send the server accepted but
  that left ``noInvitation`` in place is reported as unconfirmed.

**A note uses the other client's call.** The ``addaAddConnection`` action has
no note field. The custom-invite dialog (``/preload/custom-invite/``) belongs
to LinkedIn's older web client, and its Send calls
``voyagerRelationshipsDashMemberRelationships?action=verifyQuotaAndCreateV2``,
read from that client's own bundle: the body is
``{invitee: {inviteeUnion: {memberProfile: <profile URN>}}, customMessage,
trackingId}``, and its ``recipe`` becomes ``decorationId=
...InvitationCreationResultWithInvitee-3``. Checked with a request that cannot
succeed, inviting oneself: that body answered 500 where a malformed one
answered 400, so the shape is the one the server reads. The dialog's counter
read ``0/300`` on a Premium account; LinkedIn allows free accounts fewer
characters and fewer notes, which it refuses at send time.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    LinkedInOperationError,
    RateLimitError,
)
from linkedin_mcp_server.voyager.profile_views import (
    _POST_STREAM_JS,
    VoyagerProfileViews,
)
from linkedin_mcp_server.voyager.thread_reply import _tracking_id

logger = logging.getLogger(__name__)

_PROFILES = "https://www.linkedin.com/voyager/api/identity/dash/profiles"
_TOP_CARD = "com.linkedin.voyager.dash.deco.identity.profile.WebTopCardCore-19"
_ACTION = "com.linkedin.sdui.requests.mynetwork.addaAddConnection"
_SERVER_REQUEST = (
    "https://www.linkedin.com/flagship-web/rsc-action/actions/server-request"
    f"?sduiid={_ACTION}"
)
#: The screen the viewer list belongs to, whose headers are sent with it.
_SCREEN = "com.linkedin.sdui.flagshipnav.premium.wvmp.WVMP"
_MEMBER_PREFIX = "urn:li:member:"
_CREATE_WITH_NOTE = (
    "https://www.linkedin.com/voyager/api/voyagerRelationshipsDashMemberRelationships"
    "?action=verifyQuotaAndCreateV2&decorationId="
    "com.linkedin.voyager.dash.deco.relationships.InvitationCreationResultWithInvitee-3"
)
#: The note box's limit, as the dialog counts it (``0/300``) on Premium.
NOTE_LIMIT = 300


def parse_relationship(payload: dict[str, Any], urn: str) -> dict[str, Any]:
    """The invitee's profile fields and where the signed-in member stands.

    ``state`` is one of ``self``, ``connected``, ``not_invited``,
    ``invited_by_me``, ``invited_by_them`` or ``unknown``, each from which
    union member is present, never from text.
    """
    profile: dict[str, Any] = {}
    relationship: dict[str, Any] | None = None
    by_urn = {
        entity.get("entityUrn"): entity
        for entity in payload.get("included") or []
        if isinstance(entity, dict)
    }
    for entity in payload.get("included") or []:
        kind = str(entity.get("$type", ""))
        if kind.endswith(".Profile") and entity.get("entityUrn") == urn:
            profile = entity
        elif kind.endswith(".MemberRelationship") and str(
            entity.get("entityUrn", "")
        ).endswith(urn.rsplit(":", 1)[-1]):
            relationship = entity
    union = (relationship or {}).get("memberRelationshipUnion") or {}
    state = "unknown"
    detail: str | None = None
    message: str | None = None
    if "self" in union:
        state = "self"
    elif "connection" in union or "*connection" in union:
        state = "connected"
    elif "noConnection" in union:
        invitation = (union["noConnection"] or {}).get("invitationUnion") or {}
        if "noInvitation" in invitation:
            state = "not_invited"
        elif invitation:
            # Measured after a send: ``*invitation`` names an Invitation
            # entity in ``included``, whose ``inviteeMember`` is the person
            # invited. Invitee is them: the signed-in member sent it. An inline
            # object (not yet observed) is read the same way.
            detail = sorted(k for k in invitation if not k.startswith("$"))[0]
            inner = invitation.get(detail)
            if isinstance(inner, str):
                inner = by_urn.get(inner)
            invitee = (inner or {}).get("inviteeMember")
            note = (inner or {}).get("message")
            message = note.get("text") if isinstance(note, dict) else note
            if invitee:
                state = "invited_by_me" if invitee == urn else "invited_by_them"
    elif union:
        detail = sorted(k for k in union if not k.startswith("$"))[0]
    object_urn = str(profile.get("objectUrn") or "")
    return {
        "urn": urn,
        "member_id": object_urn[len(_MEMBER_PREFIX) :]
        if object_urn.startswith(_MEMBER_PREFIX)
        else None,
        "first_name": profile.get("firstName"),
        "last_name": profile.get("lastName"),
        "public_identifier": profile.get("publicIdentifier"),
        "state": state,
        "state_detail": detail,
        "invitation_message": message,
    }


def connect_payload(person: dict[str, Any]) -> dict[str, Any]:
    """The ``addaAddConnection`` arguments, as the viewer list renders them."""
    member_id = person["member_id"]
    vanity = person["public_identifier"]
    return {
        "inviteeUrn": {"memberId": member_id},
        "nonIterableProfileId": person["urn"],
        "renderMode": "IconAndText",
        "firstName": person.get("first_name") or "",
        "lastName": person.get("last_name") or "",
        "isDisabled": {"key": f"connect-button-disabled-{vanity}", "namespace": None},
        "connectionState": {
            "key": f"state:invitation:{_MEMBER_PREFIX}{member_id}",
            "namespace": None,
        },
        "origin": "InvitationOrigin_UNKNOWN",
        "clientContext": "Wvmp",
        "profileCanonicalUrl": f"https://www.linkedin.com/in/{vanity}",
        "firstFiveInviteCount": {"key": "guidedFlowNumSentInvites", "namespace": ""},
        "guidedFlowUrlandProfileList": {
            "key": "guidedFlowUrlAndPictureList",
            "namespace": "guidedFlowUrlAndPictureListNameSpace",
        },
        "postActionSentConfigs": [],
    }


def connect_body(payload: dict[str, Any]) -> str:
    """The server-action envelope the page wraps every server request in."""
    arguments = {
        "$type": "proto.sdui.actions.requests.RequestedArguments",
        "requestedStateKeys": [
            {
                "key": {"value": {"$case": "id", "id": "guidedFlowNumSentInvites"}},
                "namespace": "",
            },
            {
                "key": {"value": {"$case": "id", "id": "guidedFlowUrlAndPictureList"}},
                "namespace": "guidedFlowUrlAndPictureListNameSpace",
            },
        ],
        "payload": payload,
        "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
    }
    return json.dumps(
        {
            "requestId": _ACTION,
            "serverRequest": {
                "requestId": _ACTION,
                "requestedArguments": arguments,
                "isApfcEnabled": False,
                "isStreaming": False,
                "rumPageKey": "",
                "maxRetries": 0,
                "backOffMultiplier": 0,
                "maxSeconds": 0,
            },
            "states": [],
            "requestedArguments": {
                **arguments,
                "states": [],
                "screenId": _SCREEN,
                "knownTemplateIds": [],
            },
        }
    )


_STATUS = {
    "self": (
        "connect_unavailable",
        "Cannot send a connection request to your own profile.",
    ),
    "connected": ("already_connected", "You are already connected with this profile."),
    "invited_by_me": (
        "pending",
        "A connection request to this profile is already pending.",
    ),
}


class VoyagerConnect(VoyagerProfileViews):
    """Send a connection request with LinkedIn's own server action."""

    surface = "connect"

    def __init__(self, session: Any, navigator: Any, *, connection: Any = None):
        super().__init__(session, navigator)
        # Upstream's page-driven connection helper, used for ONE thing:
        # accepting an incoming invitation, which no API call has been
        # measured for. Never for sending.
        self._connection = connection

    async def _accept_incoming(self, username: str, result: Any) -> dict[str, Any]:
        """Accept an incoming invitation, and do nothing else.

        Upstream's ``connect_with_person`` accepts an incoming invitation,
        but it decides what to do from a fresh read of the page: if the
        invitation was withdrawn in between, it finds a connectable profile
        and sends a new request. This uses only its accept steps, re-checks
        on the page that the incoming request is still there, and clicks
        nothing otherwise. Success is read back from the API.
        """
        from linkedin_mcp_server.linkedin import connection as upstream

        if self._connection is None:
            return result(
                "invitation_received",
                "This member has already invited you, and accepting is not "
                "available here. Nothing was sent.",
            )
        await self._connection._read_main_profile(username)
        signals = await self._connection._read_action_signals(username)
        if upstream.detect_connection_state(signals) != "incoming_request":
            return result(
                "invitation_gone",
                "The incoming invitation was no longer on the profile when it "
                "was about to be accepted. Nothing was clicked or sent.",
            )
        if not await self._connection._click_incoming_accept():
            return result(
                "send_failed",
                "Could not find or click the Accept button. Nothing was sent.",
            )
        # LinkedIn propagates an accept asynchronously (upstream measured an
        # immediate re-read still showing the old state), so one settle retry.
        after: dict[str, Any] = {"state": None}
        for attempt in range(2):
            if attempt:
                await self._session.delay(3.0)
            try:
                after = await self._relationship(username)
            except Exception as exc:  # the click happened; never an error
                logger.warning(
                    "Accept for %s clicked; read-back failed: %s", username, exc
                )
                break
            if after["state"] == "connected":
                return result(
                    "accepted",
                    "Accepted their invitation.",
                    relationship_after="connected",
                )
        return result(
            "send_unconfirmed",
            "Accept was clicked but the relationship did not read back as "
            "connected. Check the profile before trying again.",
            relationship_after=after["state"],
        )

    async def _relationship(self, identifier: str) -> dict[str, Any]:
        member = await self._resolve_member(identifier)
        payload = await self._fetch(
            f"{_PROFILES}?q=memberIdentity"
            f"&memberIdentity={quote(identifier, safe='')}&decorationId={_TOP_CARD}"
        )
        return parse_relationship(payload, member["urn"])

    async def connect_with_person(
        self,
        linkedin_username: str,
        *,
        note: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Send a request, with or without a note, unless the relationship
        rules it out.

        ``invited_by_them`` is accepted (see ``_accept_incoming``), or on a
        dry run reported as ``invitation_received``. Nothing is ever sent to
        someone who invited the signed-in member.
        """
        from linkedin_mcp_server.linkedin.identifiers import (
            normalize_person_identifier,
            person_profile_url,
        )

        note = (note or "").strip() or None
        if note is not None and len(note) > NOTE_LIMIT:
            raise LinkedInOperationError(
                f"The note is {len(note)} characters; LinkedIn's invitation "
                f"note holds {NOTE_LIMIT}. Shorten it by {len(note) - NOTE_LIMIT}."
            )
        username = normalize_person_identifier(linkedin_username)
        url = person_profile_url(username, "/")
        before = await self._relationship(username)

        def result(status: str, message: str, **extra: Any) -> dict[str, Any]:
            return {
                "url": url,
                "status": status,
                "message": message,
                "note_sent": False,
                "relationship_before": before["state"],
                **extra,
            }

        if before["state"] in _STATUS:
            return result(*_STATUS[before["state"]])
        if before["state"] == "invited_by_them":
            if dry_run:
                return result(
                    "invitation_received",
                    "This member has already invited you. A real call accepts "
                    "their invitation. Nothing was done.",
                )
            return await self._accept_incoming(username, result)
        if before["state"] != "not_invited":
            return result(
                "connect_unavailable",
                "LinkedIn reports a relationship this tool does not send to: "
                f"{before['state']} ({before['state_detail']}).",
            )
        if not before["member_id"] or not before["public_identifier"]:
            raise LinkedInOperationError(
                f"Voyager {self.surface} profile for {username!r} has no member "
                "id or public identifier, which the request is built from."
            )
        if note is not None:
            return await self._send_with_note(username, before, note, dry_run, result)
        payload = connect_payload(before)
        if dry_run:
            return result(
                "dry_run",
                "Nothing was sent. This is the request that would be.",
                request=payload,
            )

        answer = await self._session.page.evaluate(
            _POST_STREAM_JS,
            {
                "url": _SERVER_REQUEST,
                "headers": await self._page_headers(),
                "body": connect_body(payload),
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
            return result(
                "send_failed", f"LinkedIn answered the request with HTTP {status}."
            )

        return await self._confirm(username, result)

    async def _send_with_note(
        self,
        username: str,
        before: dict[str, Any],
        note: str,
        dry_run: bool,
        result: Any,
    ) -> dict[str, Any]:
        """The custom-invite dialog's own call, with the note as its message."""
        body = {
            "invitee": {"inviteeUnion": {"memberProfile": before["urn"]}},
            "customMessage": note,
            "trackingId": _tracking_id(),
        }
        if dry_run:
            return result(
                "dry_run",
                "Nothing was sent. This is the request that would be.",
                request={key: body[key] for key in ("invitee", "customMessage")},
            )
        status, text = await self._post(_CREATE_WITH_NOTE, body)
        if status in (401, 403):
            raise AuthenticationError(
                f"Voyager {self.surface} request rejected: HTTP {status}"
            )
        if status == 429:
            raise RateLimitError(f"Voyager {self.surface} rate limited: HTTP {status}")
        if status not in (200, 201):
            # LinkedIn's refusal is passed on as it came: a note quota or a
            # length it will not take are both refused here, and only its own
            # words say which.
            return result(
                "send_failed",
                f"LinkedIn refused the invitation with HTTP {status}.",
                response_excerpt=text[:500],
            )
        return await self._confirm(username, result, note=note)

    async def _confirm(
        self, username: str, result: Any, *, note: str | None = None
    ) -> dict[str, Any]:
        """Read the relationship back: what counts is whether it changed."""
        await self._session.delay(1.5)
        try:
            after = await self._relationship(username)
        except Exception as exc:  # the send happened; never report it as an error
            # Learned on the first live send: the read-back raised on a shape
            # not yet seen, the tool reported a plain error, and the request
            # had in fact gone out. An error after a send invites a retry and
            # a second invitation, so this says what is known instead.
            logger.warning("Connect to %s sent; read-back failed: %s", username, exc)
            return result(
                "send_unconfirmed",
                "LinkedIn accepted the request, but reading the relationship "
                f"back failed ({type(exc).__name__}). Do not resend: check the "
                "sent invitations first.",
                relationship_after=None,
            )
        if after["state"] == "invited_by_me":
            if note is None:
                return result(
                    "pending",
                    "Connection request sent.",
                    relationship_after=after["state"],
                )
            # The invitation carries its note; matching it is what makes
            # note_sent a measurement rather than a hope.
            sent = after.get("invitation_message")
            return {
                **result(
                    "pending",
                    "Connection request sent with a note."
                    if sent == note
                    else "Connection request sent; its note could not be read back.",
                    relationship_after=after["state"],
                ),
                "note_sent": sent == note,
            }
        logger.warning(
            "Connect to %s answered 200 but the relationship is %s",
            username,
            after["state"],
        )
        return result(
            "send_unconfirmed",
            "LinkedIn accepted the request but the relationship did not change. "
            "Check the sent invitations before trying again.",
            relationship_after=after["state"],
        )
