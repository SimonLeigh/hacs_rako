"""Pure helpers.py behaviour: review findings 1, 2 and 5.

Kept separate from the coordinator/entity tests: these functions are
deliberately free of Home Assistant and of the bridge, so the interesting
decisions are tested directly against real ``python_rako.state`` types rather
than through a full entry setup.
"""

from __future__ import annotations

import pytest
from python_rako import (
    ChannelStatusMessage,
    CommandType,
    LevelCache,
    LevelCacheItem,
    MessageOrigin,
    RoomChannel,
    SceneCache,
    SceneStatusMessage,
)
from python_rako.state import BridgeStateSnapshot, StateSource

from custom_components.rako.helpers import (
    apply_command_echo,
    brightness_to_percentage,
    has_scene_data,
    percentage_to_brightness,
)

ROOM = 6
CHANNEL = 1


def _snapshot() -> BridgeStateSnapshot:
    """A room in scene 1, whose level table gives channel 1 a level of 200."""
    scene_cache = SceneCache({ROOM: 1})
    level_cache = LevelCache()
    level_cache[RoomChannel(ROOM, CHANNEL)] = LevelCacheItem(
        0, ROOM, CHANNEL, {1: 200, 2: 150, 0: 0}
    )
    return BridgeStateSnapshot.from_caches(scene_cache, level_cache)


# -- finding 1: echo provenance -----------------------------------------------


def test_apply_command_echo_scene_echo_keeps_derived_channels_scene_derived() -> None:
    """A scene echo must not stamp COMMAND_ECHO onto the table's approximations."""
    echo = SceneStatusMessage(
        room=ROOM,
        channel=0,
        scene=1,
        command=CommandType.SET_SCENE,
        origin=MessageOrigin.CONTROL,
    )
    result = apply_command_echo(_snapshot(), echo)

    channel_state = result.channel_state(ROOM, CHANNEL)
    assert channel_state.source is StateSource.SCENE_DERIVED
    assert channel_state.is_estimated is True


def test_apply_command_echo_level_echo_carries_command_echo() -> None:
    """A level echo is a true reported value and must carry COMMAND_ECHO."""
    echo = ChannelStatusMessage(
        room=ROOM,
        channel=CHANNEL,
        brightness=90,
        command=CommandType.SET_LEVEL,
        origin=MessageOrigin.CONTROL,
    )
    result = apply_command_echo(_snapshot(), echo)

    channel_state = result.channel_state(ROOM, CHANNEL)
    assert channel_state.level == 90
    assert channel_state.source is StateSource.COMMAND_ECHO
    assert channel_state.is_estimated is False


# -- finding 2: empty-cache detection ------------------------------------------


def test_has_scene_data_false_for_an_unreachable_bridges_empty_cache() -> None:
    """Every room appears with scene=None: what the UDP fallback produces."""
    level_cache = LevelCache()
    level_cache[RoomChannel(ROOM, 0)] = LevelCacheItem(0, ROOM, 0, {1: 255})
    snapshot = BridgeStateSnapshot.from_caches(SceneCache(), level_cache)

    assert has_scene_data(snapshot) is False


def test_has_scene_data_true_once_any_room_has_a_real_scene() -> None:
    assert has_scene_data(_snapshot()) is True


def test_has_scene_data_false_for_a_wholly_empty_snapshot() -> None:
    assert has_scene_data(BridgeStateSnapshot.from_caches(SceneCache(), LevelCache())) is False


# -- finding 5: percentage scaling ---------------------------------------------


@pytest.mark.parametrize("level", [1, 2, 3])
def test_low_nonzero_levels_are_never_reported_as_zero_percent(level: int) -> None:
    """HA's ranged-value helper floors; a level above 0 must never read as 0%."""
    assert brightness_to_percentage(level) >= 1


def test_zero_level_is_zero_percent() -> None:
    assert brightness_to_percentage(0) == 0


def test_zero_percent_is_zero_level() -> None:
    assert percentage_to_brightness(0) == 0


def test_percentage_round_trip_never_crosses_zero_across_the_full_range() -> None:
    """Every non-zero percentage round-trips through a non-zero level.

    Every 1-100% asks for a real, non-zero level, and that level reads back
    as a non-zero percentage -- a running fan can never look stopped.
    """
    for percentage in range(1, 101):
        level = percentage_to_brightness(percentage)
        assert level >= 1
        assert brightness_to_percentage(level) >= 1


def test_percentage_to_brightness_rounds_up_so_one_percent_turns_a_circuit_on() -> None:
    assert percentage_to_brightness(1) >= 1
