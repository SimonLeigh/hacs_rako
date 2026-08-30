"""Device triggers for Rako keypads, sensors and app activity.

Every status broadcast is fired as a ``rako_event`` (see :mod:`.events`), and
these triggers are the UI-friendly face of that: pick the bridge, pick the
instruction, and optionally narrow to one room or channel.

Triggers hang off the *bridge* device rather than off per-room devices.  A Rako
keypad's buttons are freely mapped -- Phase 0 found one keypad driving both its
own room and two others (``BRIDGE_BEHAVIOUR.md`` fact 16) -- so a keypad is not
a device Home Assistant can meaningfully model, and only the room id inside each
message can be trusted.  ``room``/``channel`` are therefore filters on the
event, not a device identity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol

from homeassistant.components.device_automation import (
    DEVICE_TRIGGER_BASE_SCHEMA,
    InvalidDeviceAutomationConfig,
)
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_MAC,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .const import ATTR_CHANNEL, ATTR_COMMAND, ATTR_ROOM, DOMAIN, EVENT_RAKO
from .events import COMMAND_NAMES

if TYPE_CHECKING:
    from homeassistant.core import CALLBACK_TYPE, HomeAssistant
    from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
    from homeassistant.helpers.typing import ConfigType

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(COMMAND_NAMES),
        vol.Optional(ATTR_ROOM): cv.positive_int,
        vol.Optional(ATTR_CHANNEL): cv.positive_int,
    }
)


def _bridge_mac(hass: HomeAssistant, device: dr.DeviceEntry) -> str | None:
    """Return the MAC of the bridge ``device`` represents, if it is one.

    Light and fan entities each get their own device under the bridge; those
    carry no events, so they offer no triggers.
    """
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            continue
        mac = entry.data.get(CONF_MAC)
        if mac is not None and (DOMAIN, mac) in device.identifiers:
            return str(mac)
    return None


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, str]]:
    """List the triggers a Rako bridge device offers."""
    device_registry = dr.async_get(hass)
    device = device_registry.async_get(device_id)
    if device is None or _bridge_mac(hass, device) is None:
        return []

    return [
        {
            CONF_DEVICE_ID: device_id,
            CONF_DOMAIN: DOMAIN,
            CONF_PLATFORM: "device",
            CONF_TYPE: command,
        }
        for command in COMMAND_NAMES
    ]


async def async_validate_trigger_config(
    hass: HomeAssistant, config: ConfigType
) -> ConfigType:
    """Validate a device trigger config."""
    config = TRIGGER_SCHEMA(config)

    device_registry = dr.async_get(hass)
    device = device_registry.async_get(config[CONF_DEVICE_ID])
    if device is None:
        raise InvalidDeviceAutomationConfig(
            f"Rako trigger device {config[CONF_DEVICE_ID]} not found"
        )
    if _bridge_mac(hass, device) is None:
        raise InvalidDeviceAutomationConfig(
            f"Device {config[CONF_DEVICE_ID]} is not a Rako bridge; Rako "
            "triggers fire on the bridge device"
        )
    return config


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach a trigger to the ``rako_event`` bus event."""
    event_data: dict[str, str | int] = {
        CONF_DEVICE_ID: config[CONF_DEVICE_ID],
        ATTR_COMMAND: config[CONF_TYPE],
    }
    if (room := config.get(ATTR_ROOM)) is not None:
        event_data[ATTR_ROOM] = room
    if (channel := config.get(ATTR_CHANNEL)) is not None:
        event_data[ATTR_CHANNEL] = channel

    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: EVENT_RAKO,
            event_trigger.CONF_EVENT_DATA: event_data,
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
