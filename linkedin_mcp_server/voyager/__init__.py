"""Tools that read LinkedIn's own API instead of the page it renders.

Everything this fork adds lives here, in one package upstream does not have, so
the divergence is a directory rather than a scatter of edits through theirs.
That is not tidiness. The fork only stays mergeable while it never deletes a
line upstream wrote (``tests/test_fork_divergence_is_additive.py`` enforces it),
and code in its own package cannot collide with a file upstream is editing.

**Why API rather than the rendered page.** A scraper reads what LinkedIn chose
to paint, which is a floor and not an answer: ``get_inbox`` sees roughly the
first 16 sidebar rows, and it recovers each thread id by click-visiting the row,
which marks that thread read. Asking it "have I replied to everyone" costs
unread state and still cannot see past the fold. The API the web client itself
calls answers the same question in one request, reaches any page of the
mailbox, and clicks nothing.

**How it attaches.** :mod:`linkedin_mcp_server.voyager.overlay` runs after
upstream has registered its own tools, removes the ones ours supersede, and
registers ours in their place. Upstream's implementations are left exactly as
written -- superseding a tool is done by *not serving it*, never by editing it.
"""
