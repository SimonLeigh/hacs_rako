"""The fan platform: room-as-scene and channel-as-level, as a percentage.

BUG FOUND BY THIS SUITE, NOW FIXED (commit 68aefec, "Fan: declare SET_SPEED
so the percentage slider and fan.set_percentage work"): ``RakoFan`` used to
only declare ``FanEntityFeature.TURN_OFF | TURN_ON``, never ``SET_SPEED``.
Home Assistant's fan platform gates both the ``fan.set_percentage`` service
*and* the ``percentage``/``percentage_step`` state attributes on ``SET_SPEED``
being declared (homeassistant/components/fan/__init__.py, ``async_setup`` and
``state_attributes``), even though the entity always fully implemented
``percentage``/``async_set_percentage``. ``test_fan_declares_set_speed_and_its_attributes``
and ``test_set_percentage_service_now_works`` below pin the fixed behaviour
down; the rest of this module drives percentage through ``fan.set_percentage``
(the first-class route now that it works), with one test kept on
``fan.turn_on(percentage=...)`` since that is a distinct, real call pattern
(turning a fan on straight to a given speed) worth its own coverage.
"""

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

from homeassistant.components.fan import (
    ATTR_PERCENTAGE,
    ATTR_PERCENTAGE_STEP,
    FanEntityFeature,
)
from homeassistant.exceptions import HomeAssistantError

from .conftest import CHANNEL_UTILITY_EXTRACT, ROOM_BATHROOM, ROOM_UTILITY


def _level_broadcast(room: int, channel: int, brightness: int) -> ChannelStatusMessage:
    return ChannelStatusMessage(
        room=room,
        channel=channel,
        brightness=brightness,
        command=CommandType.SET_LEVEL,
        data=(1, brightness),
        origin=MessageOrigin.CONTROL,
    )


async def test_fan_declares_set_speed_and_its_attributes(hass, rako_integration) -> None:
    state = hass.states.get("fan.bathroom_fan")
    assert state.attributes["supported_features"] & FanEntityFeature.SET_SPEED
    assert state.attributes[ATTR_PERCENTAGE_STEP] == 1.0


async def test_room_fan_percentage_reflects_its_scene(hass, rako_integration) -> None:
    """Bathroom starts in scene 1 -> brightness 255 -> 100%."""
    state = hass.states.get("fan.bathroom_fan")
    assert state.state == "on"
    assert state.attributes[ATTR_PERCENTAGE] == 100


async def test_room_fan_set_percentage_selects_the_closest_scene(hass, rako_integration) -> None:
    await hass.services.async_call(
        "fan",
        "set_percentage",
        {"entity_id": "fan.bathroom_fan", ATTR_PERCENTAGE: 25},
        blocking=True,
    )

    assert ("scene", ROOM_BATHROOM, 4) in rako_integration.bridge.commands
    assert hass.states.get("fan.bathroom_fan").attributes[ATTR_PERCENTAGE] == 25


async def test_room_fan_turn_on_with_percentage_selects_the_closest_scene(
    hass, rako_integration
) -> None:
    """``fan.turn_on(percentage=...)`` is a separate, still-supported entry point."""
    await hass.services.async_call(
        "fan",
        "turn_on",
        {"entity_id": "fan.bathroom_fan", ATTR_PERCENTAGE: 25},
        blocking=True,
    )

    assert ("scene", ROOM_BATHROOM, 4) in rako_integration.bridge.commands
    assert hass.states.get("fan.bathroom_fan").attributes[ATTR_PERCENTAGE] == 25


async def test_room_fan_turn_off_is_percentage_zero(hass, rako_integration) -> None:
    await hass.services.async_call(
        "fan", "turn_off", {"entity_id": "fan.bathroom_fan"}, blocking=True
    )

    assert ("scene", ROOM_BATHROOM, 0) in rako_integration.bridge.commands
    state = hass.states.get("fan.bathroom_fan")
    assert state.state == "off"
    assert state.attributes[ATTR_PERCENTAGE] == 0


async def test_room_fan_turn_on_without_percentage_goes_to_full(hass, rako_integration) -> None:
    await hass.services.async_call(
        "fan", "turn_on", {"entity_id": "fan.bathroom_fan"}, blocking=True
    )

    assert ("scene", ROOM_BATHROOM, 1) in rako_integration.bridge.commands


async def test_channel_fan_state_follows_the_echoed_level(hass, rako_integration) -> None:
    """The paced/verified path: set 60%, but the bridge's echo says 0 -- state follows it."""
    rako_integration.bridge.echoes[(ROOM_UTILITY, CHANNEL_UTILITY_EXTRACT)] = _level_broadcast(
        ROOM_UTILITY, CHANNEL_UTILITY_EXTRACT, 0
    )

    await hass.services.async_call(
        "fan",
        "set_percentage",
        {"entity_id": "fan.utility_extract", ATTR_PERCENTAGE: 60},
        blocking=True,
    )

    assert (
        "level",
        ROOM_UTILITY,
        CHANNEL_UTILITY_EXTRACT,
        153,  # percentage_to_brightness(60)
    ) in rako_integration.bridge.commands
    state = hass.states.get("fan.utility_extract")
    assert state.attributes[ATTR_PERCENTAGE] == 0
    assert state.state == "off"


async def test_channel_fan_command_error_raises_and_leaves_state_untouched(
    hass, rako_integration
) -> None:
    entity_id = "fan.utility_extract"
    before = hass.states.get(entity_id).attributes[ATTR_PERCENTAGE]
    rako_integration.bridge.echoes[(ROOM_UTILITY, CHANNEL_UTILITY_EXTRACT)] = RakoCommandError(
        "no echo after retry"
    )

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "fan",
            "set_percentage",
            {"entity_id": entity_id, ATTR_PERCENTAGE: 80},
            blocking=True,
        )

    assert hass.states.get(entity_id).attributes[ATTR_PERCENTAGE] == before


async def test_channel_fan_zero_percent_is_a_real_level_not_unknown(
    hass, rako_integration
) -> None:
    rako_integration.listener.emit(_level_broadcast(ROOM_UTILITY, CHANNEL_UTILITY_EXTRACT, 0))
    await hass.async_block_till_done()

    state = hass.states.get("fan.utility_extract")
    assert state.state == "off"
    assert state.attributes[ATTR_PERCENTAGE] == 0
    assert state.attributes["estimated"] is False


async def test_channel_fan_unknown_level_is_on_none(hass, rako_integration) -> None:
    rako_integration.listener.emit(
        FadeMessage(
            room=ROOM_UTILITY,
            channel=CHANNEL_UTILITY_EXTRACT,
            command=CommandType.FADE,
            data=(128, 0, 0, 0, 0),
            direction=FadeDirection.UP,
        )
    )
    await hass.async_block_till_done()

    state = hass.states.get("fan.utility_extract")
    assert state.attributes[ATTR_PERCENTAGE] is None
    assert state.state == "unknown"
    assert state.attributes["estimated"] is True


async def test_set_percentage_service_now_works(hass, rako_integration) -> None:
    """Regression test for commit 68aefec: this used to raise ServiceNotSupported."""
    await hass.services.async_call(
        "fan",
        "set_percentage",
        {"entity_id": "fan.bathroom_fan", ATTR_PERCENTAGE: 50},
        blocking=True,
    )

    assert ("scene", ROOM_BATHROOM, 3) in rako_integration.bridge.commands
    state = hass.states.get("fan.bathroom_fan")
    assert state.attributes[ATTR_PERCENTAGE] == 50
    assert ATTR_PERCENTAGE_STEP in state.attributes
