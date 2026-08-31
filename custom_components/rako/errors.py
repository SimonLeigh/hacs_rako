"""Exception groups the integration expects to survive.

One definition, imported everywhere, so a widened catch cannot drift out of
sync between the coordinator and the platforms.
"""

from __future__ import annotations

from xml.parsers.expat import ExpatError

from python_rako import RakoBridgeError

#: Talking to the bridge: its own errors, plus the network underneath it.
BRIDGE_ERRORS: tuple[type[Exception], ...] = (RakoBridgeError, OSError, TimeoutError)

#: Discovery additionally parses ``rako.xml``.  A bridge that answers with a
#: login page, a captive portal or a truncated document produces a parse error
#: or a missing key rather than a network error, and that must not escape the
#: "could not set up yet" handling and kill the config entry outright.
DISCOVERY_ERRORS: tuple[type[Exception], ...] = (
    *BRIDGE_ERRORS,
    ExpatError,
    KeyError,
    TypeError,
    ValueError,
)
