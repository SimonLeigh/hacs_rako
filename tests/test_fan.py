"""The fan platform: room-as-scene and channel-as-level, as a percentage.

BUG FOUND IN THE REWRITE (reported, not fixed here -- see WP-2.4 report):
``RakoFan._attr_supported_features`` (custom_components/rako/fan.py) only
declares ``FanEntityFeature.TURN_OFF | FanEntityFeature.TURN_ON``, never
``FanEntityFeature.SET_SPEED``. Home Assistant's fan platform gates both the
``fan.set_percentage`` service *and* the ``percentage``/``percentage_step``
state attributes on ``SET_SPEED`` being declared
(homeassistant/components/fan/__init__.py, ``async_setup`` and
``state_attributes``), even though the entity fully implements
``percentage``/``async_set_percentage``. In practice: the ``percentage``
attribute never appears on the fan's state at all, calling
``fan.set_percentage`` raises ``ServiceNotSupported``, and speed can only be
driven by passing ``percentage=`` to ``fan.turn_on`` (whose service
registration only requires ``TURN_ON``). ``test_set_percentage_service_is_not_supported_bug``
below pins this down; the rest of this module drives percentage through
``fan.turn_on`` and reads the entity directly, since that is what actually
works today.
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

from homeassistant.components.fan import ATTR_PERCENTAGE, DATA_COMPONENT
from homeassistant.exceptions import HomeAssistantError, ServiceNotSupported

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


def _entity(hass, entity_id: str):
    return hass.data[DATA_COMPONENT].get_entity(entity_id)


async def test_room_fan_percentage_reflects_its_scene(hass, rako_integration) -> None:
    """Bathroom starts in scene 1 -> brightness 255 -> 100%."""
    assert hass.states.get("fan.bathroom_fan").state == "on"
    assert _entity(hass, "fan.bathroom_fan").percentage == 100


async def test_room_fan_turn_on_with_percentage_selects_the_closest_scene(
    hass, rako_integration
) -> None:
    await hass.services.async_call(
        "fan",
        "turn_on",
        {"entity_id": "fan.bathroom_fan", ATTR_PERCENTAGE: 25},
        blocking=True,
    )

    assert ("scene", ROOM_BATHROOM, 4) in rako_integration.bridge.commands
    assert _entity(hass, "fan.bathroom_fan").percentage == 25


async def test_room_fan_turn_off_is_percentage_zero(hass, rako_integration) -> None:
    await hass.services.async_call(
        "fan", "turn_off", {"entity_id": "fan.bathroom_fan"}, blocking=True
    )

    assert ("scene", ROOM_BATHROOM, 0) in rako_integration.bridge.commands
    entity = _entity(hass, "fan.bathroom_fan")
    assert hass.states.get("fan.bathroom_fan").state == "off"
    assert entity.percentage == 0


async def test_room_fan_turn_on_without_percentage_goes_to_full(hass, rako_integration) -> None:
    await hass.services.async_call(
        "fan", "turn_on", {"entity_id": "fan.bathroom_fan"}, blocking=True
    )

    assert ("scene", ROOM_BATHROOM, 1) in rako_integration.bridge.commands


async def test_channel_fan_state_follows_the_echoed_level(hass, rako_integration) -> None:
    rako_integration.bridge.echoes[(ROOM_UTILITY, CHANNEL_UTILITY_EXTRACT)] = _level_broadcast(
        ROOM_UTILITY, CHANNEL_UTILITY_EXTRACT, 0
    )

    await hass.services.async_call(
        "fan",
        "turn_on",
        {"entity_id": "fan.utility_extract", ATTR_PERCENTAGE: 60},
        blocking=True,
    )

    assert (
        "level",
        ROOM_UTILITY,
        CHANNEL_UTILITY_EXTRACT,
        153,  # percentage_to_brightness(60)
    ) in rako_integration.bridge.commands
    entity = _entity(hass, "fan.utility_extract")
    assert entity.percentage == 0
    assert hass.states.get("fan.utility_extract").state == "off"


async def test_channel_fan_command_error_raises_and_leaves_state_untouched(
    hass, rako_integration
) -> None:
    entity_id = "fan.utility_extract"
    before = _entity(hass, entity_id).percentage
    rako_integration.bridge.echoes[(ROOM_UTILITY, CHANNEL_UTILITY_EXTRACT)] = RakoCommandError(
        "no echo after retry"
    )

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "fan",
            "turn_on",
            {"entity_id": entity_id, ATTR_PERCENTAGE: 80},
            blocking=True,
        )

    assert _entity(hass, entity_id).percentage == before


async def test_channel_fan_zero_percent_is_a_real_level_not_unknown(
    hass, rako_integration
) -> None:
    rako_integration.listener.emit(_level_broadcast(ROOM_UTILITY, CHANNEL_UTILITY_EXTRACT, 0))
    await hass.async_block_till_done()

    entity = _entity(hass, "fan.utility_extract")
    assert hass.states.get("fan.utility_extract").state == "off"
    assert entity.percentage == 0
    assert entity.extra_state_attributes["estimated"] is False


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

    entity = _entity(hass, "fan.utility_extract")
    assert entity.percentage is None
    assert entity.is_on is None
    assert hass.states.get("fan.utility_extract").state == "unknown"


async def test_set_percentage_service_is_not_supported_bug(hass, rako_integration) -> None:
    """Pins the missing-``SET_SPEED`` bug documented at the top of this module."""
    with pytest.raises(ServiceNotSupported):
        await hass.services.async_call(
            "fan",
            "set_percentage",
            {"entity_id": "fan.bathroom_fan", ATTR_PERCENTAGE: 50},
            blocking=True,
        )
    assert ATTR_PERCENTAGE not in hass.states.get("fan.bathroom_fan").attributes
