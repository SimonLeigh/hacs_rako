"""Every decoded status broadcast becomes a ``rako_event`` (WP-2.6).

``build_event_data`` is pure (message in, dict out -- see events.py's own
docstring), so most of this module tests it directly; a couple of tests go
through the coordinator and the event bus to prove events fire even for a
room/channel nothing in Home Assistant tracks (BRIDGE_BEHAVIOUR.md facts 3,
15, 16 -- a keypad mapped to an untracked room must still drive automations).
"""

from __future__ import annotations

from python_rako import (
    ChannelStatusMessage,
    CommandType,
    MessageOrigin,
    SceneStatusMessage,
)
from python_rako.const import FadeDirection
from python_rako.protocol import (
    Custom232Message,
    FadeMessage,
    HolidayMessage,
    IdentMessage,
    LevelToggleMessage,
    StopFadeMessage,
    StoreMessage,
    UnknownStatusMessage,
)

from custom_components.rako.const import EVENT_RAKO
from custom_components.rako.events import COMMAND_NAMES, build_event_data, command_name

from .conftest import TEST_MAC

#: A room/channel this fixture never gives an entity to -- a keypad's scene
#: buttons mapped to a room Home Assistant otherwise knows nothing about, per
#: BRIDGE_BEHAVIOUR.md's room-158 observation.
UNTRACKED_ROOM = 158


def test_command_names_cover_every_command_type_plus_unknown() -> None:
    assert "set_scene" in COMMAND_NAMES
    assert "level_toggle" in COMMAND_NAMES
    assert "unknown" in COMMAND_NAMES
    assert tuple(sorted(COMMAND_NAMES)) == COMMAND_NAMES  # deterministic device-trigger order


def test_command_name_for_unmodelled_instruction_is_unknown() -> None:
    message = UnknownStatusMessage(room=1, channel=0, command=99, data=(1, 2, 3))
    assert command_name(message) == "unknown"


def test_scene_message_event_data() -> None:
    message = SceneStatusMessage(
        room=6,
        channel=0,
        scene=2,
        command=CommandType.SET_SCENE,
        data=(1, 2),
        origin=MessageOrigin.CONTROL,
    )
    data = build_event_data(message, bridge_mac=TEST_MAC)

    assert data["bridge_mac"] == TEST_MAC
    assert data["room"] == 6
    assert data["channel"] == 0
    assert data["command"] == "set_scene"
    assert data["command_code"] == CommandType.SET_SCENE.value
    assert data["data"] == [1, 2]
    assert data["origin"] == "control"
    assert data["scene"] == 2
    assert "level" not in data


def test_channel_message_event_data() -> None:
    message = ChannelStatusMessage(
        room=7, channel=2, brightness=129, command=CommandType.SET_LEVEL, data=(1, 129)
    )
    data = build_event_data(message, bridge_mac=TEST_MAC)
    assert data["command"] == "set_level"
    assert data["level"] == 129
    assert "scene" not in data


def test_level_toggle_0x33_event_data_when_turned_on() -> None:
    """The undocumented cmd 0x33, empirically [flags, level, on/off] (fact 3)."""
    message = LevelToggleMessage(
        room=UNTRACKED_ROOM,
        channel=0,
        command=CommandType.LEVEL_TOGGLE,
        data=(128, 255, 1),
        level=255,
        is_on=True,
    )
    data = build_event_data(message, bridge_mac=TEST_MAC)
    assert data["command"] == "level_toggle"
    assert data["is_on"] is True
    assert data["level"] == 255


def test_level_toggle_0x33_event_data_when_turned_off() -> None:
    message = LevelToggleMessage(
        room=UNTRACKED_ROOM,
        channel=0,
        command=CommandType.LEVEL_TOGGLE,
        data=(128, 255, 0),
        level=255,
        is_on=False,
    )
    data = build_event_data(message, bridge_mac=TEST_MAC)
    assert data["is_on"] is False
    assert data["level"] == 0  # effective_level: off means 0 regardless of `level`


def test_fade_message_event_data_carries_direction() -> None:
    message = FadeMessage(
        room=9,
        channel=0,
        command=CommandType.FADE,
        data=(128, 0, 0, 0, 0),
        direction=FadeDirection.UP,
    )
    data = build_event_data(message, bridge_mac=TEST_MAC)
    assert data["command"] == "fade"
    assert data["direction"] == "up"


def test_stop_fade_message_event_data() -> None:
    message = StopFadeMessage(room=9, channel=0, command=CommandType.STOP_FADING, data=())
    data = build_event_data(message, bridge_mac=TEST_MAC)
    assert data["command"] == "stop_fading"
    assert "direction" not in data


def test_store_message_event_data() -> None:
    message = StoreMessage(room=1, channel=0, command=CommandType.STORE, data=())
    data = build_event_data(message, bridge_mac=TEST_MAC)
    assert data["command"] == "store"


def test_ident_message_event_data() -> None:
    message = IdentMessage(room=1, channel=1, command=CommandType.IDENT, data=())
    data = build_event_data(message, bridge_mac=TEST_MAC)
    assert data["command"] == "ident"


def test_custom_232_message_event_data() -> None:
    message = Custom232Message(
        room=1, channel=0, command=CommandType.CUSTOM_232, data=(1, 42), string_id=42
    )
    data = build_event_data(message, bridge_mac=TEST_MAC)
    assert data["command"] == "custom_232"
    assert data["string_id"] == 42


def test_holiday_message_event_data() -> None:
    message = HolidayMessage(room=1, channel=0, command=CommandType.HOLIDAY, data=(1,), mode=1)
    data = build_event_data(message, bridge_mac=TEST_MAC)
    assert data["command"] == "holiday"
    assert data["mode"] == 1


def test_sensor_origin_is_distinguishable_from_control(hass) -> None:
    """BRIDGE_BEHAVIOUR.md fact 17: PIR retriggers must be tellable from keypad presses."""
    sensor_message = SceneStatusMessage(
        room=145, channel=0, scene=1, command=CommandType.SET_SCENE, data=(9, 1),
        origin=MessageOrigin.SENSOR,
    )
    control_message = SceneStatusMessage(
        room=9, channel=0, scene=1, command=CommandType.SET_SCENE, data=(1, 1),
        origin=MessageOrigin.CONTROL,
    )
    assert build_event_data(sensor_message, bridge_mac=TEST_MAC)["origin"] == "sensor"
    assert build_event_data(control_message, bridge_mac=TEST_MAC)["origin"] == "control"


def test_unknown_instruction_still_fires_with_its_numeric_code() -> None:
    """An instruction the decoder cannot name is still delivered, never dropped."""
    message = UnknownStatusMessage(
        room=UNTRACKED_ROOM, channel=0, command=99, data=(1, 2, 3), origin=MessageOrigin.UNKNOWN
    )
    data = build_event_data(message, bridge_mac=TEST_MAC)
    assert data["command"] == "unknown"
    assert data["command_code"] == 99
    assert data["data"] == [1, 2, 3]


def test_device_id_included_only_when_given() -> None:
    message = StoreMessage(room=1, channel=0, command=CommandType.STORE, data=())
    without = build_event_data(message, bridge_mac=TEST_MAC)
    assert "device_id" not in without

    with_device = build_event_data(message, bridge_mac=TEST_MAC, device_id="device-123")
    assert with_device["device_id"] == "device-123"


async def test_event_fires_on_the_bus_for_a_room_with_no_entity(
    hass, rako_integration
) -> None:
    """A keypad mapped to a room Home Assistant tracks nothing for still fires."""
    events = []
    hass.bus.async_listen(EVENT_RAKO, lambda event: events.append(event.data))

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

    assert len(events) == 1
    assert events[0]["room"] == UNTRACKED_ROOM
    assert events[0]["command"] == "level_toggle"
    assert events[0]["device_id"] == rako_integration.coordinator.bridge_device_id
