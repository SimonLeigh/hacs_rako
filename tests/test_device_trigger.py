"""Device triggers for the bridge device (WP-2.6).

Triggers hang off the *bridge* device, not per-room devices -- a keypad's
buttons are freely mapped to rooms (BRIDGE_BEHAVIOUR.md fact 16), so only the
room id inside each event can be trusted, and ``room``/``channel`` are filters
on the trigger config rather than a device identity.
"""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import (
    async_get_device_automations,
    async_mock_service,
)
from python_rako import (
    ChannelStatusMessage,
    CommandType,
    MessageOrigin,
    SceneStatusMessage,
)
from python_rako.protocol import LevelToggleMessage

from custom_components.rako.const import ATTR_CHANNEL, ATTR_ROOM, DOMAIN
from custom_components.rako.events import COMMAND_NAMES
from homeassistant.components.automation import DOMAIN as AUTOMATION_DOMAIN
from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component

from .conftest import TEST_MAC

UNTRACKED_ROOM = 158


def _bridge_device_id(hass) -> str:
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(identifiers={(DOMAIN, TEST_MAC)})
    assert device is not None
    return device.id


async def test_bridge_device_lists_every_command_as_a_trigger(hass, rako_integration) -> None:
    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, _bridge_device_id(hass)
    )
    trigger_types = {trigger[CONF_TYPE] for trigger in triggers}
    assert trigger_types == set(COMMAND_NAMES)
    assert all(trigger[CONF_DOMAIN] == DOMAIN for trigger in triggers)


async def test_a_light_device_offers_no_triggers(hass, rako_integration) -> None:
    """Light/fan devices carry no events of their own; only the bridge does."""
    entity_registry_entry = hass.states.get("light.kitchen")
    assert entity_registry_entry is not None
    device_registry = dr.async_get(hass)
    # The kitchen room light's device is not the bridge device.
    kitchen_device = next(
        device
        for device in device_registry.devices.values()
        if device.name == "Kitchen"
    )
    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, kitchen_device.id
    )
    # The light domain offers its own generic device triggers (turned_on,
    # turned_off, ...) for any light entity; only Rako's own are asserted here.
    rako_triggers = [trigger for trigger in triggers if trigger[CONF_DOMAIN] == DOMAIN]
    assert rako_triggers == []


async def test_trigger_fires_on_a_matching_event(hass, rako_integration) -> None:
    device_id = _bridge_device_id(hass)
    calls = async_mock_service(hass, "test", "automation")

    assert await async_setup_component(
        hass,
        AUTOMATION_DOMAIN,
        {
            AUTOMATION_DOMAIN: [
                {
                    "trigger": {
                        CONF_PLATFORM: "device",
                        CONF_DOMAIN: DOMAIN,
                        CONF_DEVICE_ID: device_id,
                        CONF_TYPE: "level_toggle",
                    },
                    "action": {
                        "service": "test.automation",
                        "data_template": {"room": "{{ trigger.event.data.room }}"},
                    },
                }
            ]
        },
    )

    rako_integration.listener.emit(
        LevelToggleMessage(
            room=UNTRACKED_ROOM,
            channel=0,
            command=CommandType.LEVEL_TOGGLE,
            data=(128, 255, 1),
            level=255,
            is_on=True,
        )
    )
    await hass.async_block_till_done()

    assert len(calls) == 1
    assert calls[0].data["room"] == UNTRACKED_ROOM


async def test_trigger_room_filter_is_respected(hass, rako_integration) -> None:
    device_id = _bridge_device_id(hass)
    calls = async_mock_service(hass, "test", "automation")

    assert await async_setup_component(
        hass,
        AUTOMATION_DOMAIN,
        {
            AUTOMATION_DOMAIN: [
                {
                    "trigger": {
                        CONF_PLATFORM: "device",
                        CONF_DOMAIN: DOMAIN,
                        CONF_DEVICE_ID: device_id,
                        CONF_TYPE: "set_scene",
                        ATTR_ROOM: 9,
                    },
                    "action": {"service": "test.automation"},
                }
            ]
        },
    )

    def _scene(room: int) -> SceneStatusMessage:
        return SceneStatusMessage(
            room=room, channel=0, scene=1, command=CommandType.SET_SCENE,
            data=(1, 1), origin=MessageOrigin.CONTROL,
        )

    rako_integration.listener.emit(_scene(6))  # different room: must not fire
    await hass.async_block_till_done()
    assert len(calls) == 0

    rako_integration.listener.emit(_scene(9))  # matching room: must fire
    await hass.async_block_till_done()
    assert len(calls) == 1


async def test_trigger_channel_filter_is_respected(hass, rako_integration) -> None:
    device_id = _bridge_device_id(hass)
    calls = async_mock_service(hass, "test", "automation")

    assert await async_setup_component(
        hass,
        AUTOMATION_DOMAIN,
        {
            AUTOMATION_DOMAIN: [
                {
                    "trigger": {
                        CONF_PLATFORM: "device",
                        CONF_DOMAIN: DOMAIN,
                        CONF_DEVICE_ID: device_id,
                        CONF_TYPE: "set_level",
                        ATTR_CHANNEL: 2,
                    },
                    "action": {"service": "test.automation"},
                }
            ]
        },
    )

    def _level(channel: int) -> ChannelStatusMessage:
        return ChannelStatusMessage(
            room=7, channel=channel, brightness=100, command=CommandType.SET_LEVEL, data=(1, 100)
        )

    rako_integration.listener.emit(_level(1))
    await hass.async_block_till_done()
    assert len(calls) == 0

    rako_integration.listener.emit(_level(2))
    await hass.async_block_till_done()
    assert len(calls) == 1


async def test_validate_trigger_config_rejects_a_non_bridge_device(hass, rako_integration) -> None:
    device_registry = dr.async_get(hass)
    non_bridge_device = next(
        device
        for device in device_registry.devices.values()
        if device.name == "Kitchen"
    )
    calls = async_mock_service(hass, "test", "automation")

    # A device trigger config pointed at a non-bridge device must be rejected
    # at automation setup rather than silently doing nothing.
    result = await async_setup_component(
        hass,
        AUTOMATION_DOMAIN,
        {
            AUTOMATION_DOMAIN: [
                {
                    "trigger": {
                        CONF_PLATFORM: "device",
                        CONF_DOMAIN: DOMAIN,
                        CONF_DEVICE_ID: non_bridge_device.id,
                        CONF_TYPE: "set_scene",
                    },
                    "action": {"service": "test.automation"},
                }
            ]
        },
    )
    assert result is True  # component sets up; the individual automation fails to attach
    assert len(calls) == 0
