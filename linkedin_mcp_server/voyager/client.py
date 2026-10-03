"""One authenticated Voyager GET, and the guards every surface needs.

Shared by every reader in this package so a new surface inherits the parts that
were expensive to get right rather than reimplementing them slightly wrong.
What lives here is only what is genuinely common: issuing the request as the
logged-in page, typing the failures, and refusing to report a parse failure as
an empty result. Anything that differs per surface -- the endpoint, the payload
shape, what a row means -- stays in the reader that owns it.

**The request is issued from inside the page, not from Python.** The session
lives in the browser profile, and Voyager requires the ``csrf-token`` header to
carry the JSESSIONID cookie value. Reading the cookie in page context and
fetching from there is what makes the call authenticated at all; a request
built outside it arrives without the session and is refused.

**A missing csrf-token fails with HTTP 403, loudly, at the transport layer.**
That is worth stating because it is the negative control this whole package
leans on: the dangerous failure on these surfaces is not an error, it is a
plausible-looking zero, and a fault that cannot masquerade as an empty
collection is a fault you can trust yourself to notice.
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

logger = logging.getLogger(__name__)

#: The normalized representation. Rows come back as URN pointers with the
#: entities themselves in ``included``, which is why a reader resolves
#: references rather than reading nested objects.
_ACCEPT = "application/vnd.linkedin.normalized+json+2.1"

_ME = "https://www.linkedin.com/voyager/api/me"
_PROFILES = "https://www.linkedin.com/voyager/api/identity/dash/profiles"
_PROFILE_URN_PREFIX = "urn:li:fsd_profile:"

_FETCH_JS = (
    """async (target) => {
    const m = document.cookie.match(/JSESSIONID="?([^";]+)/);
    if (!m) return {error: 'no JSESSIONID cookie in page context'};
    const r = await fetch(target, {
        credentials: 'include',
        headers: {
            'csrf-token': m[1],
            'accept': '%s',
        },
    });
    if (r.status !== 200) return {error: 'HTTP ' + r.status, status: r.status};
    return {body: await r.text()};
}"""
    % _ACCEPT
)

#: One Voyager POST. Measured on 2026-10-02 against the messaging page: its own
#: writes are JSON bodies posted to ``...?action=<name>`` with the csrf-token
#: and ``x-restli-protocol-version`` headers. Unlike the GET, the status is
#: handed back rather than collapsed into an error, because for a write "the
#: server refused it" and "nobody knows whether it landed" are different
#: answers and only the caller can say which one a status means.
_POST_JS = """async ({url, body}) => {
    const m = document.cookie.match(/JSESSIONID="?([^";]+)/);
    if (!m) return {error: 'no JSESSIONID cookie in page context'};
    const r = await fetch(url, {
        method: 'POST',
        credentials: 'include',
        headers: {
            'csrf-token': m[1],
            'accept': 'application/json',
            'content-type': 'text/plain;charset=UTF-8',
            'x-restli-protocol-version': '2.0.0',
        },
        body: JSON.stringify(body),
    });
    return {status: r.status, body: await r.text()};
}"""


def person_identifier(profile_url: str | None, profile_urn: str | None) -> str | None:
    """What to pass as ``linkedin_username`` to every person tool.

    The vanity name when LinkedIn gave a profile URL with one, otherwise the
    profile id from the URN (``ACoAA...``). Measured on 2026-10-02: the
    profile finder resolves that id to the same member as the vanity name, so
    either chains into get_person_profile, send_message or connect_with_person.
    Messaging participants usually carry only the id.
    """
    import re

    match = re.search(r"/in/([^/?#]+)", profile_url or "")
    if match and not match.group(1).startswith("ACoAA"):
        return match.group(1)
    urn = profile_urn or ""
    if urn.startswith(_PROFILE_URN_PREFIX):
        return urn[len(_PROFILE_URN_PREFIX) :]
    return match.group(1) if match else None


def company_id(company_urn: str | None) -> str | None:
    """The numeric id that company filters take, from a company URN."""
    tail = str(company_urn or "").rsplit(":", 1)[-1]
    return tail if tail.isdigit() else None


class VoyagerReader:
    """Base for readers that call LinkedIn's own API from the logged-in page."""

    #: Named in every error this reader raises, so a failure says which surface
    #: it came from rather than leaving the caller to guess from a stack.
    surface = "voyager"

    def __init__(self, session: Any, navigator: Any):
        self._session = session
        self._navigator = navigator

    async def _fetch(self, url: str) -> dict[str, Any]:
        """Issue one Voyager GET from inside the authenticated page."""
        raw = await self._session.page.evaluate(_FETCH_JS, url)
        if not isinstance(raw, dict) or raw.get("error"):
            detail = (raw or {}).get("error", "unknown")
            status = (raw or {}).get("status")
            # Auth and rate-limit failures keep their own types so a caller can
            # tell "sign in again" and "slow down" apart from "this broke", and
            # so neither is retried as though it were transient noise.
            if status in (401, 403):
                raise AuthenticationError(
                    f"Voyager {self.surface} request rejected: {detail}"
                )
            if status == 429:
                raise RateLimitError(
                    f"Voyager {self.surface} request rate limited: {detail}"
                )
            raise LinkedInScraperException(
                f"Voyager {self.surface} request failed: {detail}"
            )
        return json.loads(raw["body"])

    async def _mailbox_urn(self) -> str:
        """The signed-in member's profile URN, which every conversation hangs off.

        Measured on 2026-10-02: ``/voyager/api/me`` carries it as
        ``included[].dashEntityUrn``.
        """
        payload = await self._fetch(_ME)
        urns = [
            entity.get("dashEntityUrn")
            for entity in payload.get("included") or []
            if isinstance(entity, dict)
        ]
        urns = [urn for urn in urns if isinstance(urn, str) and urn]
        # Exactly one, or a thread would be addressed from a guess.
        if len(urns) != 1 or not urns[0].startswith(_PROFILE_URN_PREFIX):
            raise LinkedInScraperException(
                f"Voyager {self.surface} could not identify the signed-in member: "
                f"expected one profile URN in /me, found {len(urns)}."
            )
        return urns[0]

    async def _resolve_member(self, identifier: str) -> dict[str, Any]:
        """One member's profile URN and name from a public identifier or id.

        Measured on 2026-10-02: ``identity/dash/profiles?q=memberIdentity``
        answers with exactly one profile URN at ``data['*elements']``. Exactly
        one or it raises: a best guess among several would address the wrong
        person.
        """
        payload = await self._fetch(
            f"{_PROFILES}?q=memberIdentity&memberIdentity={quote(identifier, safe='')}"
        )
        data = payload.get("data") or {}
        found = self._has_rows_key(data)
        urns = [
            urn
            for urn in (data.get("*elements") or [] if found else [])
            if isinstance(urn, str) and urn.startswith(_PROFILE_URN_PREFIX)
        ]
        self._refuse_unexplained_zero(
            rows=urns, payload=payload, path="data['*elements']", container_found=found
        )
        if len(urns) != 1:
            raise LinkedInScraperException(
                f"Voyager {self.surface} found {len(urns)} members for "
                f"{identifier!r}, not exactly one. Pass the /in/ public "
                "identifier exactly as a profile URL shows it."
            )
        entity = self._by_urn(payload).get(urns[0]) or {}
        name = " ".join(
            part for part in (entity.get("firstName"), entity.get("lastName")) if part
        )
        return {"urn": urns[0], "name": name or None}

    async def _post(self, url: str, body: dict[str, Any]) -> tuple[int, str]:
        """Issue one Voyager POST from inside the authenticated page.

        Returns the status and the raw response text. Only a request that
        provably never left raises here: without the session cookie nothing was
        sent. Every status the server did answer with is the caller's to judge.
        """
        raw = await self._session.page.evaluate(_POST_JS, {"url": url, "body": body})
        if not isinstance(raw, dict) or raw.get("error"):
            detail = (raw or {}).get("error", "unknown")
            raise AuthenticationError(
                f"Voyager {self.surface} request was not sent: {detail}"
            )
        return int(raw["status"]), str(raw.get("body") or "")

    def _refuse_unexplained_zero(
        self,
        *,
        rows: list[Any],
        payload: dict[str, Any],
        path: str,
        container_found: bool,
    ) -> None:
        """Raise when nothing parsed because the path missed, not because it was empty.

        **The failure this exists to stop is a wrong path, not an error.** On
        2026-09-18 a reader of the invitations board took ``data['*elements']``
        where the response nests it at ``data.data['*elements']``. Nothing threw.
        It returned a clean, well-formed zero, which on that surface reads as
        "the board is empty, there is nothing to do" -- and the board had six
        live rows on it the whole time. A typo would have closed a routine's
        entire surface inside a normal-looking report.

        **The discriminator is whether the container was FOUND, not whether the
        payload carried anything.** That distinction is the whole guard, and
        getting it wrong the first time made this fire on a genuinely empty
        board: an empty board still has a populated ``data`` object, so testing
        for data present turned "nothing pending" into an error on every run.
        A found-but-empty container is a real zero and is allowed to stand; a
        container that was never found, in a response that plainly carried
        something, is a shape change and must be louder than a zero.
        """
        if rows or container_found:
            return
        if not (payload.get("included") or payload.get("data")):
            return
        keys = sorted(payload.keys())
        raise LinkedInScraperException(
            f"Voyager {self.surface} payload changed shape: the container at "
            f"{path!r} was not found, but the response carried data "
            f"(top-level keys: {keys}). Refusing to report this as an empty "
            f"result, because an empty result here is indistinguishable from a "
            f"wrong path and is acted on as though the surface were clear."
        )

    @staticmethod
    def _has_rows_key(container: Any) -> bool:
        """Whether a collection container is there, populated or not.

        Measured on 2026-10-02: a populated collection carries ``*elements``
        (URN pointers into ``included``) and an EMPTY one carries ``elements:
        []`` instead. Looking for the first alone reads a real "no results" as
        a container that was never found, and the zero guard then raises on
        every empty answer.
        """
        return isinstance(container, dict) and (
            "*elements" in container or "elements" in container
        )

    @staticmethod
    def _by_urn(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Index ``included`` entities by their URN, for resolving pointers."""
        return {
            entity["entityUrn"]: entity
            for entity in payload.get("included") or []
            if isinstance(entity, dict) and entity.get("entityUrn")
        }
