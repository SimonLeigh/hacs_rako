"""The push/poll hybrid coordinator (MODERNISATION_PLAN.md 3.2/3.2a).

Room 1 ("Kitchen") channel 1 ("Ceiling") is the fixture's stand-in for the
BRIDGE_BEHAVIOUR.md fact-9 scenario: scene 1's level table entry for that
channel is 200, so a poll that finds the room still in scene 1 must never
overwrite a level a broadcast actually reported.
"""

from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import async_fire_time_changed
from python_rako import (
    ChannelStatusMessage,
    CommandType,
    MessageOrigin,
    RakoBridgeError,
    SceneStatusMessage,
)
from python_rako.protocol import StoreMessage, UnknownStatusMessage
from python_rako.state import StateSource

from custom_components.rako import coordinator as coordinator_module
from custom_components.rako.const import DEFAULT_POLL_INTERVAL
from homeassistant.util import dt as dt_util

from .conftest import CHANNEL_KITCHEN_CEILING, ROOM_KITCHEN
from .fakes import FakeBridge, FakeStatusListener


def _level_broadcast(room: int, channel: int, brightness: int) -> ChannelStatusMessage:
    return ChannelStatusMessage(
        room=room,
        channel=channel,
        brightness=brightness,
        command=CommandType.LEVEL_SET_LEGACY,
        data=(brightness, brightness),
        origin=MessageOrigin.CONTROL,
    )


async def _advance_past_poll_interval(hass) -> None:
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=DEFAULT_POLL_INTERVAL + 1)
    )
    await hass.async_block_till_done()


async def test_push_message_updates_entity_state(hass, rako_integration) -> None:
    entity_id = "light.kitchen_ceiling"
    assert hass.states.get(entity_id).attributes["brightness"] == 200  # scene-derived

    rako_integration.listener.emit(_level_broadcast(ROOM_KITCHEN, CHANNEL_KITCHEN_CEILING, 129))
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state.attributes["brightness"] == 129
    assert state.attributes["estimated"] is False


async def test_poll_reconcile_does_not_clobber_a_fresher_push_level(
    hass, rako_integration
) -> None:
    """BRIDGE_BEHAVIOUR.md fact 9: a poll must not overwrite a broadcast level.

    The room stays in the same scene the whole time (only the channel level
    changed, exactly like an app slider drag), so the cache read on poll
    "agrees" with what the coordinator already tracks and must not re-derive
    the channel from the level table.
    """
    entity_id = "light.kitchen_ceiling"
    rako_integration.listener.emit(_level_broadcast(ROOM_KITCHEN, CHANNEL_KITCHEN_CEILING, 129))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).attributes["brightness"] == 129

    # The bridge's scene cache still says room 1 = scene 1 (the level table
    # believes channel 1 is 200 in that scene) -- a naive poll would revert.
    await _advance_past_poll_interval(hass)

    state = hass.states.get(entity_id)
    assert state.attributes["brightness"] == 129
    assert rako_integration.bridge.get_state_snapshot_calls >= 2


async def test_poll_corrects_a_missed_scene_change(hass, rako_integration) -> None:
    """A scene change the push path never delivered is picked up on the next poll."""
    entity_id = "light.kitchen_ceiling"
    assert hass.states.get(entity_id).attributes["brightness"] == 200

    # Simulate a missed broadcast: the bridge's cache now says scene 3, but we
    # were never told, so the push path alone would stay wrong forever.
    rako_integration.bridge.scene_cache[1] = 3
    await _advance_past_poll_interval(hass)

    state = hass.states.get(entity_id)
    assert state.attributes["brightness"] == 100  # scene 3's level for channel 1
    assert hass.states.get("light.kitchen").attributes["brightness"] == 128  # convert_to_scene(3)


async def test_store_broadcast_triggers_a_level_table_refresh(hass, rako_integration) -> None:
    rako_integration.listener.emit(
        StoreMessage(room=ROOM_KITCHEN, channel=0, command=CommandType.STORE, data=())
    )
    # The refresh runs as a config-entry background task (coordinator.py
    # _async_schedule_level_table_refresh), which plain async_block_till_done
    # does not wait for.
    await hass.async_block_till_done(wait_background_tasks=True)

    assert rako_integration.bridge.refresh_level_table_calls == 1
    assert rako_integration.coordinator.data.level_table_stale is False


async def test_availability_listener_down_poll_ok_stays_available(
    hass, rako_integration
) -> None:
    entity_id = "light.kitchen"
    assert hass.states.get(entity_id).state != "unavailable"

    rako_integration.listener.set_health(is_running=False, last_error="socket closed")
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state != "unavailable"
    assert rako_integration.coordinator.last_update_success is True


async def test_availability_both_paths_down_goes_unavailable(hass, rako_integration) -> None:
    entity_id = "light.kitchen"

    rako_integration.listener.set_health(is_running=False, last_error="socket closed")
    rako_integration.bridge.snapshot_error = RakoBridgeError("bridge unreachable")
    await _advance_past_poll_interval(hass)

    assert hass.states.get(entity_id).state == "unavailable"
    assert rako_integration.coordinator.last_update_success is False


async def test_availability_recovers_once_both_paths_are_healthy_again(
    hass, rako_integration
) -> None:
    entity_id = "light.kitchen"
    rako_integration.listener.set_health(is_running=False)
    rako_integration.bridge.snapshot_error = RakoBridgeError("bridge unreachable")
    await _advance_past_poll_interval(hass)
    assert hass.states.get(entity_id).state == "unavailable"

    rako_integration.bridge.snapshot_error = None
    rako_integration.listener.set_health(is_running=True, restart_count=1)
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state != "unavailable"
    assert rako_integration.coordinator.last_update_success is True


async def test_poll_failure_with_healthy_listener_keeps_the_pushed_state(
    hass, rako_integration
) -> None:
    """A lone failed poll is not itself a reason to go unavailable (push still works)."""
    entity_id = "light.kitchen_ceiling"
    rako_integration.listener.emit(_level_broadcast(ROOM_KITCHEN, CHANNEL_KITCHEN_CEILING, 77))
    await hass.async_block_till_done()

    rako_integration.bridge.snapshot_error = RakoBridgeError("scenes.htm timed out")
    await _advance_past_poll_interval(hass)

    state = hass.states.get(entity_id)
    assert state.state != "unavailable"
    assert state.attributes["brightness"] == 77
    assert rako_integration.coordinator.last_update_success is True


async def test_pir_storm_identical_scene_applies_do_not_wedge_state(
    hass, rako_integration
) -> None:
    """Repeated identical SET_SCENE broadcasts (fact 17/18) must not corrupt state."""
    entity_id = "light.kitchen"
    message = SceneStatusMessage(
        room=ROOM_KITCHEN,
        channel=0,
        scene=1,
        command=CommandType.SET_SCENE,
        data=(9, 1),  # flags=9: sensor origin (BRIDGE_BEHAVIOUR.md fact 17)
        origin=MessageOrigin.SENSOR,
    )
    for _ in range(5):
        rako_integration.listener.emit(message)
        await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state.state == "on"
    assert state.attributes["brightness"] == 255
    assert rako_integration.coordinator.last_update_success is True


async def test_poll_first_refresh_failure_raises_update_failed(
    hass, mock_config_entry, monkeypatch, created_bridges, created_listeners
) -> None:
    """No prior data and a failing poll must surface as a genuine failure."""

    def failing_factory(*args, **kwargs):
        bridge = FakeBridge(*args, **kwargs)
        bridge.snapshot_error = RakoBridgeError("no reply")
        created_bridges.append(bridge)
        return bridge

    def listener_factory(*args, **kwargs):
        listener = FakeStatusListener(*args, **kwargs)
        created_listeners.append(listener)
        return listener

    monkeypatch.setattr(coordinator_module, "Bridge", failing_factory)
    monkeypatch.setattr(coordinator_module, "StatusListener", listener_factory)

    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert not hasattr(mock_config_entry, "runtime_data")  # never assigned: setup failed first
    assert created_bridges[0].closed is True


async def test_command_echo_updates_state_via_state_source_command_echo(
    hass, rako_integration
) -> None:
    coordinator = rako_integration.coordinator
    await coordinator.async_set_channel_level(ROOM_KITCHEN, CHANNEL_KITCHEN_CEILING, 42)

    channel_state = coordinator.data.channel_state(ROOM_KITCHEN, CHANNEL_KITCHEN_CEILING)
    assert channel_state.level == 42
    assert channel_state.source is StateSource.COMMAND_ECHO
    assert channel_state.is_estimated is False


async def test_unmodelled_instruction_is_applied_harmlessly_and_still_fires(
    hass, rako_integration
) -> None:
    """§3.2a-2: an instruction the decoder cannot name is never silently dropped."""
    events = []
    hass.bus.async_listen("rako_event", lambda event: events.append(event.data))

    rako_integration.listener.emit(
        UnknownStatusMessage(room=ROOM_KITCHEN, channel=0, command=99, data=(1, 2, 3))
    )
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0]["command"] == "unknown"
    assert events[0]["command_code"] == 99
    # No state-carrying fields on this message, so the snapshot is unchanged
    # but the coordinator must not have raised or gone unavailable.
    assert rako_integration.coordinator.last_update_success is True
