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

from typing import TYPE_CHECKING, Any

from python_rako import ChannelStatusMessage, CommandType, SceneStatusMessage
from python_rako.protocol import (
    Custom232Message,
    FadeMessage,
    HolidayMessage,
    LevelToggleMessage,
)

from .const import (
    ATTR_BRIDGE_MAC,
    ATTR_CHANNEL,
    ATTR_COMMAND,
    ATTR_COMMAND_CODE,
    ATTR_DATA,
    ATTR_DIRECTION,
    ATTR_IS_ON,
    ATTR_LEVEL,
    ATTR_ORIGIN,
    ATTR_ROOM,
    ATTR_SCENE,
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

    if isinstance(message, SceneStatusMessage):
        event_data[ATTR_SCENE] = message.scene
    elif isinstance(message, ChannelStatusMessage):
        event_data[ATTR_LEVEL] = message.brightness
    elif isinstance(message, LevelToggleMessage):
        event_data[ATTR_LEVEL] = message.effective_level
        event_data[ATTR_IS_ON] = message.is_on
    elif isinstance(message, FadeMessage):
        event_data[ATTR_DIRECTION] = message.direction.value
    elif isinstance(message, Custom232Message):
        event_data["string_id"] = message.string_id
    elif isinstance(message, HolidayMessage):
        event_data["mode"] = message.mode

    return event_data
