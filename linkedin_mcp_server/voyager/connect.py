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

A note is not part of this action. With a note the call goes to upstream's
implementation, which types it into the custom-invite page.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    LinkedInScraperException,
    RateLimitError,
)
from linkedin_mcp_server.voyager.profile_views import (
    _POST_STREAM_JS,
    VoyagerProfileViews,
)

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

    async def _relationship(self, identifier: str) -> dict[str, Any]:
        member = await self._resolve_member(identifier)
        payload = await self._fetch(
            f"{_PROFILES}?q=memberIdentity"
            f"&memberIdentity={quote(identifier, safe='')}&decorationId={_TOP_CARD}"
        )
        return parse_relationship(payload, member["urn"])

    async def connect_with_person(
        self, linkedin_username: str, *, dry_run: bool = False
    ) -> dict[str, Any]:
        """Send a request without a note, unless the relationship rules it out.

        ``invited_by_them`` is returned to the caller rather than acted on:
        accepting is a different action and is not measured here.
        """
        from linkedin_mcp_server.scraping.identifiers import (
            normalize_person_identifier,
            person_profile_url,
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
        if before["state"] != "not_invited":
            return result(
                "connect_unavailable",
                "LinkedIn reports a relationship this tool does not send to: "
                f"{before['state']} ({before['state_detail']}).",
            )
        if not before["member_id"] or not before["public_identifier"]:
            raise LinkedInScraperException(
                f"Voyager {self.surface} profile for {username!r} has no member "
                "id or public identifier, which the request is built from."
            )
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
            return result(
                "pending",
                "Connection request sent.",
                relationship_after=after["state"],
            )
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
            response_excerpt=(answer.get("text") or "")[:500],
        )
