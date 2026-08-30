"""The light platform: room-as-scene and channel-as-level brightness."""

from __future__ import annotations

import pytest
from python_rako import (
    ChannelStatusMessage,
    CommandType,
    MessageOrigin,
    RakoCommandError,
)
from python_rako.const import FadeDirection
from python_rako.protocol import FadeMessage

from homeassistant.components.light import ATTR_BRIGHTNESS
from homeassistant.exceptions import HomeAssistantError

from .conftest import CHANNEL_KITCHEN_CEILING, ROOM_A, ROOM_KITCHEN


def _level_broadcast(room: int, channel: int, brightness: int) -> ChannelStatusMessage:
    return ChannelStatusMessage(
        room=room,
        channel=channel,
        brightness=brightness,
        command=CommandType.SET_LEVEL,
        data=(1, brightness),
        origin=MessageOrigin.CONTROL,
    )


async def test_room_light_brightness_reflects_its_scene(hass, rako_integration) -> None:
    """Room A starts in scene 2, whose nominal brightness is 192 (fact: convert_to_brightness)."""
    state = hass.states.get("light.room_a")
    assert state.state == "on"
    assert state.attributes[ATTR_BRIGHTNESS] == 192
    assert state.attributes["estimated"] is False


async def test_room_light_brightness_follows_a_true_channel_0_broadcast(
    hass, rako_integration
) -> None:
    """A direct level broadcast for the room's own channel 0 outranks the scene."""
    rako_integration.listener.emit(_level_broadcast(ROOM_A, 0, 40))
    await hass.async_block_till_done()

    state = hass.states.get("light.room_a")
    assert state.attributes[ATTR_BRIGHTNESS] == 40
    assert state.attributes["estimated"] is False


async def test_room_light_turn_on_selects_the_closest_scene(hass, rako_integration) -> None:
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": "light.room_a", ATTR_BRIGHTNESS: 64},
        blocking=True,
    )

    assert ("scene", ROOM_A, 4) in rako_integration.bridge.commands
    state = hass.states.get("light.room_a")
    assert state.attributes[ATTR_BRIGHTNESS] == 64


async def test_room_light_turn_off_selects_scene_0(hass, rako_integration) -> None:
    await hass.services.async_call(
        "light", "turn_off", {"entity_id": "light.room_a"}, blocking=True
    )

    assert ("scene", ROOM_A, 0) in rako_integration.bridge.commands
    assert hass.states.get("light.room_a").state == "off"


async def test_channel_light_turn_on_state_follows_the_echo_not_the_request(
    hass, rako_integration
) -> None:
    """Set 200, but the bridge's echo says 129 -- HA must show 129."""
    rako_integration.bridge.echoes[(ROOM_KITCHEN, CHANNEL_KITCHEN_CEILING)] = (
        _level_broadcast(ROOM_KITCHEN, CHANNEL_KITCHEN_CEILING, 129)
    )

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": "light.kitchen_ceiling", ATTR_BRIGHTNESS: 200},
        blocking=True,
    )

    assert ("level", ROOM_KITCHEN, CHANNEL_KITCHEN_CEILING, 200) in rako_integration.bridge.commands
    state = hass.states.get("light.kitchen_ceiling")
    assert state.attributes[ATTR_BRIGHTNESS] == 129
    assert state.attributes["estimated"] is False


async def test_channel_light_command_error_raises_and_leaves_state_untouched(
    hass, rako_integration
) -> None:
    entity_id = "light.kitchen_ceiling"
    before = hass.states.get(entity_id).attributes[ATTR_BRIGHTNESS]
    rako_integration.bridge.echoes[(ROOM_KITCHEN, CHANNEL_KITCHEN_CEILING)] = RakoCommandError(
        "no echo after retry"
    )

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "light",
            "turn_on",
            {"entity_id": entity_id, ATTR_BRIGHTNESS: 250},
            blocking=True,
        )

    assert hass.states.get(entity_id).attributes[ATTR_BRIGHTNESS] == before


async def test_channel_light_unknown_level_reports_estimated_and_is_on_none(
    hass, rako_integration
) -> None:
    """After a fade, the channel's true level is unknowable (estimated, not off)."""
    rako_integration.listener.emit(
        FadeMessage(
            room=ROOM_KITCHEN,
            channel=CHANNEL_KITCHEN_CEILING,
            command=CommandType.FADE,
            data=(128, 0, 0, 0, 0),
            direction=FadeDirection.UP,
        )
    )
    await hass.async_block_till_done()

    state = hass.states.get("light.kitchen_ceiling")
    assert state.state == "unknown"
    assert state.attributes["estimated"] is True


async def test_channel_light_turn_off_sends_brightness_zero(hass, rako_integration) -> None:
    await hass.services.async_call(
        "light", "turn_off", {"entity_id": "light.kitchen_ceiling"}, blocking=True
    )

    assert (
        "level",
        ROOM_KITCHEN,
        CHANNEL_KITCHEN_CEILING,
        0,
    ) in rako_integration.bridge.commands
