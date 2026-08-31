"""Constants for the Rako integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "rako"

MANUFACTURER: Final = "Rako"

#: Event fired on the Home Assistant bus for every decoded status broadcast.
EVENT_RAKO: Final = "rako_event"

# -- options -----------------------------------------------------------------

#: Seconds between reconciliation polls of the bridge's scene cache.
CONF_POLL_INTERVAL: Final = "poll_interval"
#: Transport used to *send* commands. Status is always received over UDP.
CONF_TRANSPORT: Final = "transport"
#: Minimum seconds between two commands; the bridge silently drops commands
#: that arrive too close together (BRIDGE_BEHAVIOUR.md fact 12).
CONF_MIN_COMMAND_INTERVAL: Final = "min_command_interval"

TRANSPORT_UDP: Final = "udp"
TRANSPORT_HTTP: Final = "http"
TRANSPORTS: Final = [TRANSPORT_UDP, TRANSPORT_HTTP]

DEFAULT_TRANSPORT: Final = TRANSPORT_UDP
#: Five minutes bounds how long a missed broadcast can leave state diverged.
DEFAULT_POLL_INTERVAL: Final = 300
#: Below a minute the poll starts competing with the push path for no benefit.
MIN_POLL_INTERVAL: Final = 60
MAX_POLL_INTERVAL: Final = 3600

MIN_COMMAND_INTERVAL_FLOOR: Final = 0.1
MIN_COMMAND_INTERVAL_CEILING: Final = 10.0

# -- event / device-trigger payload keys --------------------------------------

ATTR_BRIDGE_MAC: Final = "bridge_mac"
ATTR_CHANNEL: Final = "channel"
ATTR_COMMAND: Final = "command"
ATTR_COMMAND_CODE: Final = "command_code"
ATTR_DATA: Final = "data"
ATTR_DIRECTION: Final = "direction"
ATTR_ESTIMATED: Final = "estimated"
ATTR_IS_ON: Final = "is_on"
ATTR_LEVEL: Final = "level"
ATTR_ORIGIN: Final = "origin"
ATTR_ROOM: Final = "room"
ATTR_SCENE: Final = "scene"

#: ``command`` value for a well-framed broadcast carrying an instruction the
#: library does not model. Fired anyway, so a keypad button mapped to nothing
#: Home Assistant tracks can still drive an automation.
COMMAND_UNKNOWN: Final = "unknown"
