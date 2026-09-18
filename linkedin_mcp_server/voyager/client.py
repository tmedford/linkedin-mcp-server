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
    def _by_urn(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Index ``included`` entities by their URN, for resolving pointers."""
        return {
            entity["entityUrn"]: entity
            for entity in payload.get("included") or []
            if isinstance(entity, dict) and entity.get("entityUrn")
        }
