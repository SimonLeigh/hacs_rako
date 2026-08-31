"""Rako status broadcasts as Home Assistant events.

Every Rako keypad press, occupancy-sensor trigger, app action and scheduled
change is broadcast by the bridge -- including presses on buttons that control
nothing Home Assistant knows about.  Phase 0 confirmed the coverage is complete
(``BRIDGE_BEHAVIOUR.md`` facts 3, 15 and 16), so those broadcasts are worth
surfacing as events in their own right: they let a Rako keypad drive automations
for non-Rako devices.

The functions here are deliberately pure -- a message in, a dict out -- so the
event schema can be tested without a bridge or a running Home Assistant.
"""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import TYPE_CHECKING, Any

from python_rako import CommandType, LevelToggleMessage

from .const import (
    ATTR_BRIDGE_MAC,
    ATTR_CHANNEL,
    ATTR_COMMAND,
    ATTR_COMMAND_CODE,
    ATTR_DATA,
    ATTR_LEVEL,
    ATTR_ORIGIN,
    ATTR_ROOM,
    COMMAND_UNKNOWN,
)

if TYPE_CHECKING:
    from python_rako import StatusMessage

#: Every command the decoder can name, as it appears in ``rako_event`` and in
#: the device-trigger list.  Derived from :class:`python_rako.CommandType` so a
#: command added to the library shows up here without further edits.
COMMAND_NAMES: tuple[str, ...] = (
    *sorted(command.name.lower() for command in CommandType),
    COMMAND_UNKNOWN,
)

#: Fields every status message carries; they are reported under their own keys
#: and must not be repeated by the payload walk below.
_COMMON_FIELDS = frozenset(
    {"room", "channel", "command", "data", "flags", "origin", "raw"}
)

#: Library field names that read better in an event payload.
_FIELD_ALIASES = {"brightness": ATTR_LEVEL}


def command_name(message: StatusMessage) -> str:
    """Name of the instruction that produced ``message``.

    Unrecognised instructions -- the bridge sends at least one the official
    table omits -- are reported as ``"unknown"``; their numeric code is carried
    separately in ``command_code`` so an automation can still match on it.
    """
    if isinstance(message.command, CommandType):
        return message.command.name.lower()
    return COMMAND_UNKNOWN


def build_event_data(
    message: StatusMessage, *, bridge_mac: str, device_id: str | None = None
) -> dict[str, Any]:
    """Build the ``rako_event`` payload for one status broadcast.

    ``origin`` distinguishes an occupancy sensor from a keypad, an app or us
    (fact 17); without it a PIR retrigger every few seconds looks exactly like
    somebody leaning on a button.

    Whatever a message type adds on top of the common fields -- a scene, a
    level, a fade direction -- is copied out by walking the dataclass, so a
    message type the library gains later arrives with its payload intact
    instead of silently losing it.
    """
    event_data: dict[str, Any] = {
        ATTR_BRIDGE_MAC: bridge_mac,
        ATTR_ROOM: message.room,
        ATTR_CHANNEL: message.channel,
        ATTR_COMMAND: command_name(message),
        ATTR_COMMAND_CODE: message.command_value,
        ATTR_DATA: list(message.data),
        ATTR_ORIGIN: message.origin.value,
    }
    if device_id is not None:
        # Device triggers match on this, the way deconz_event does.
        event_data["device_id"] = device_id

    for field in dataclasses.fields(message):
        if field.name in _COMMON_FIELDS:
            continue
        value = getattr(message, field.name)
        if isinstance(value, Enum):
            value = value.value
        event_data[_FIELD_ALIASES.get(field.name, field.name)] = value

    if isinstance(message, LevelToggleMessage):
        # This one carries the level it takes *when on* plus a separate on/off
        # flag; the level that ended up on the circuit is the useful one.
        event_data[ATTR_LEVEL] = message.effective_level

    return event_data
