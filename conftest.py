"""Fork-owned: run upstream's differential rows with upstream's tools.

Those rows start real servers against a synthetic origin that serves no API
endpoints, so this fork's overlay has to stay out of them. See
``UPSTREAM_TOOLS_ENV`` in ``linkedin_mcp_server/voyager/overlay.py``.

Keyed on the gate upstream's CI sets for exactly those steps, and done here
rather than in their workflow or their ``tests/differential/conftest.py``, so
it follows the rows wherever upstream moves them and edits no file they own.
The servers inherit this process's environment, which is how it reaches them.
"""

import os

if os.environ.get("LINKEDIN_MCP_DIFFERENTIAL_CI") == "1":
    os.environ.setdefault("VOYAGER_OVERLAY_DISABLED", "1")
