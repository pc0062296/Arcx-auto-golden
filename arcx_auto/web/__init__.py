"""L4 - local web UI.

**Standard library only.** This is a single user, loopback bound, read only
dashboard rather than a service, so http.server is enough and nothing needs to
be installed on an air gapped network.

Read only is a hard property: the UI renders the state.json the daemon writes.
It never touches a run folder and computes nothing of its own. Write actions
(Phase 3) will be posted to the daemon through commands/.
"""

from arcx_auto.web.server import WebOptions, serve

__all__ = ["WebOptions", "serve"]
