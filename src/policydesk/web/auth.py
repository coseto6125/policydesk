"""
The back office's shared secret, in a module neither pane owns.

`server` and `console` both need it, and while it lived in `server` the console had
to import back into the module that imports it. That cycle held only because the
console deferred its import to request time — and it broke the moment `server` ran as
`__main__`, because the deferred import then loaded the module a second time and
Sanic refused the duplicate app registration. The 500 was on the console's own route,
which named neither the cycle nor the entry point.
"""

import os
import secrets

# The back office reads every member's national ID, occupation and address. Without a
# token anyone who can reach the port reads all of it — an acceptance run connected
# straight to /ws/desk and pulled 17 cases with full personal data. A shared secret is
# the smallest thing that is still true; a real deployment puts staff behind SSO.
#
# A hardcoded fallback would be worse than none: a default that ships in the source is
# a password everyone already has, and it makes an unconfigured deployment look
# protected. When the variable is unset a fresh token is minted per boot and logged, so
# the operator has to read it out of the log to open the pane, and nobody else can
# guess it.
DESK_TOKEN = os.environ.get("POLICYDESK_DESK_TOKEN") or secrets.token_urlsafe(16)
