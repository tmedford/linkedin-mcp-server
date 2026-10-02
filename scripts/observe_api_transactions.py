"""Run VoyagerDiscovery against one or more surfaces and print the findings.

Deliberately a script, not an MCP tool. The frozen facade contracts require a
policy trace for every tool-facing method, and this is a throwaway whose whole
job is to tell us which endpoints to build real tools on.

It owns the browser profile while it runs, so stop the MCP daemon first:

    pkill -f "linkedin_mcp_server.daemon_owner"; pkill -f "mcp-server-linkedin"
    uv run python scripts/observe_api_transactions.py profile-views

The daemon respawns on the next MCP call.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

_DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

from linkedin_mcp_server.core.browser import BrowserManager
from linkedin_mcp_server.scraping.session import ScrapingSession
from linkedin_mcp_server.voyager.discovery import SURFACES, VoyagerDiscovery


async def main(surfaces: list[str]) -> int:
    unknown = [s for s in surfaces if s not in SURFACES]
    if unknown:
        print(f"unknown surface(s): {unknown}; known: {sorted(SURFACES)}")
        return 2

    # The profile refuses to open under an older browser than last wrote it,
    # so use the same Chrome the daemon is configured with rather than the
    # bundled Chrome for Testing.
    chrome = os.environ.get("CHROME_PATH", _DEFAULT_CHROME)
    browser = BrowserManager(headless=True, executable_path=chrome)
    await browser.start()
    try:
        discovery = VoyagerDiscovery(ScrapingSession(browser.page))
        for surface in surfaces:
            print(f"\n{'=' * 70}\n{surface}\n{'=' * 70}")
            try:
                result = await discovery.observe(surface)
            except Exception as exc:  # noqa: BLE001 - a surface failing must not
                # take the others down; the point is to compare them.
                print(f"  FAILED: {type(exc).__name__}: {exc}")
                continue
            flag = "OK" if result["landed_on_target"] else "*** NOT THE TARGET PAGE ***"
            print(f"  landed: {result['landed_url']}  [{flag}]")
            print(f"  title : {result['landed_title']}")
            print(
                f"  voyager calls: {result['total']}"
                f"  (rest={result['rest_count']} graphql={result['graphql_count']})"
            )
            for kind in ("rest", "graphql"):
                for entry in result[kind]:
                    line = (
                        f"    [{entry['kind']:7}] {entry['method']:4} "
                        f"{entry['status']} {entry['path']}"
                    )
                    if entry.get("query_id"):
                        line += f"\n                queryId={entry['query_id']}"
                    line += f"\n                params={entry['param_keys']}"
                    print(line)
            with open(f"/tmp/discovery-{surface}.json", "w") as fh:
                json.dump(result, fh, indent=2)
    finally:
        await browser.close()
    return 0


if __name__ == "__main__":
    args = sys.argv[1:] or sorted(SURFACES)
    raise SystemExit(asyncio.run(main(args)))
