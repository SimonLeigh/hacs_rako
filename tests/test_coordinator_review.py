"""Regression tests for the code-review fixes on top of the coordinator.

One test module per round of findings would fragment the story, so these sit
together: each covers one numbered finding from the PR #32 review (commits
10e223e, bd8651a, 46cc90f), cross-referenced in each test's docstring.
"""

from __future__ import annotations

from datetime import timedelta
import logging

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from python_rako import (
    ChannelStatusMessage,
    CommandType,
    MessageOrigin,
    RakoBridgeError,
    SceneCache,
)
from python_rako.const import RAKO_BRIDGE_DEFAULT_PORT

from custom_components.rako import coordinator as coordinator_module
from custom_components.rako.const import DEFAULT_POLL_INTERVAL, DOMAIN
from custom_components.rako.coordinator import RakoCoordinator
from homeassistant.components.light import ATTR_BRIGHTNESS
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_MAC, CONF_NAME, CONF_PORT
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .conftest import (
    CHANNEL_KITCHEN_CEILING,
    ROOM_KITCHEN,
    TEST_HOST,
    TEST_MAC,
    TEST_NAME,
)
from .fakes import FakeBridge, FakeStatusListener

_LOGGER_NAME = "custom_components.rako"


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


# -- finding 1: room-scene echo provenance (integration level) ---------------


async def test_room_scene_echo_keeps_derived_channels_scene_derived_and_estimated(
    hass, rako_integration
) -> None:
    """A scene echo must not make the *channels* it implies outrank a real level.

    The room's own brightness (the scene selection itself) is a true fact the
    bridge just confirmed, and has always read as non-estimated. What finding 1
    is about is the channel the scene implies: that is still the level table's
    approximation, and marking it COMMAND_ECHO would let it survive
    reconciliation forever and outrank a real broadcast.
    """
    coordinator = rako_integration.coordinator
    await coordinator.async_set_room_scene(ROOM_KITCHEN, 1)  # scene 1 -> brightness 255

    room_state = hass.states.get("light.kitchen")
    assert room_state.attributes[ATTR_BRIGHTNESS] == 255
    assert room_state.attributes["estimated"] is False

    channel_state = hass.states.get("light.kitchen_ceiling")
    assert channel_state.attributes[ATTR_BRIGHTNESS] == 200  # scene 1's table level
    assert channel_state.attributes["estimated"] is True

    # A later real level broadcast for that channel still outranks the
    # scene-derived approximation.
    rako_integration.listener.emit(_level_broadcast(ROOM_KITCHEN, CHANNEL_KITCHEN_CEILING, 40))
    await hass.async_block_till_done()

    channel_state = hass.states.get("light.kitchen_ceiling")
    assert channel_state.attributes[ATTR_BRIGHTNESS] == 40
    assert channel_state.attributes["estimated"] is False


# -- finding 2: empty scene cache from a poll ---------------------------------


async def test_poll_empty_scene_cache_after_startup_keeps_tracked_state(
    hass, rako_integration, caplog: pytest.LogCaptureFixture
) -> None:
    """Once there is data to lose, an empty read is a failure, not news."""
    entity_id = "light.kitchen_ceiling"
    before = hass.states.get(entity_id).attributes[ATTR_BRIGHTNESS]

    rako_integration.bridge.scene_cache = SceneCache()  # what an unreachable bridge yields
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        await _advance_past_poll_interval(hass)

    state = hass.states.get(entity_id)
    assert state.attributes[ATTR_BRIGHTNESS] == before
    assert rako_integration.coordinator.last_update_success is True
    assert any("empty scene cache" in record.message for record in caplog.records)


async def test_poll_empty_scene_cache_before_first_snapshot_is_accepted_with_warning(
    hass, mock_config_entry, monkeypatch, created_bridges, created_listeners,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing to lose yet: an empty first read is the honest starting point."""

    def factory(*args, **kwargs):
        bridge = FakeBridge(*args, **kwargs)  # scene_cache/level_cache stay empty
        created_bridges.append(bridge)
        return bridge

    def listener_factory(*args, **kwargs):
        listener = FakeStatusListener(*args, **kwargs)
        created_listeners.append(listener)
        return listener

    monkeypatch.setattr(coordinator_module, "Bridge", factory)
    monkeypatch.setattr(coordinator_module, "StatusListener", listener_factory)

    mock_config_entry.add_to_hass(hass)
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    coordinator = mock_config_entry.runtime_data.coordinator
    assert coordinator.data is not None
    assert coordinator.last_update_success is True
    assert any("empty scene cache" in record.message for record in caplog.records)


# -- finding 3: the poll has its own timer ------------------------------------


async def test_continuous_push_traffic_does_not_delay_the_reconcile_poll(
    hass, rako_integration
) -> None:
    """A steady poll cadence, not the base class's reschedule-on-every-update.

    update_interval=None + async_track_time_interval, not the base class's
    reschedule-on-every-update, which busy occupancy sensors would starve.
    """
    initial_polls = rako_integration.bridge.get_state_snapshot_calls
    start = dt_util.utcnow()

    for seconds in range(20, 401, 20):
        rako_integration.listener.emit(
            _level_broadcast(ROOM_KITCHEN, CHANNEL_KITCHEN_CEILING, 30 + seconds % 50)
        )
        async_fire_time_changed(hass, start + timedelta(seconds=seconds))
        await hass.async_block_till_done()

    assert rako_integration.bridge.get_state_snapshot_calls > initial_polls


# -- finding 4: listener port; unproven push path degrades verification ------


async def test_listener_binds_the_default_port_regardless_of_the_entry_port(
    hass, patch_bridge, patch_listener, created_bridges, created_listeners
) -> None:
    """Status broadcasts always arrive on 9761, whatever port commands use."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_MAC,
        data={
            CONF_HOST: TEST_HOST,
            CONF_PORT: 44818,  # deliberately not the default
            CONF_MAC: TEST_MAC,
            CONF_NAME: TEST_NAME,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert created_bridges[-1].port == 44818  # commands still go to the configured port
    assert created_listeners[-1].port == RAKO_BRIDGE_DEFAULT_PORT


async def test_unproven_push_path_degrades_commands_to_unverified_with_one_warning(
    hass, mock_config_entry, patch_bridge, patch_listener, created_bridges,
    created_listeners, caplog: pytest.LogCaptureFixture,
) -> None:
    """A listener that has heard nothing must not fail every command outright."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    bridge = created_bridges[-1]
    assert created_listeners[-1].health.messages_received == 0

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        await hass.services.async_call(
            "light", "turn_on", {"entity_id": "light.room_a", ATTR_BRIGHTNESS: 64}, blocking=True
        )
        await hass.services.async_call(
            "light", "turn_on", {"entity_id": "light.room_a", ATTR_BRIGHTNESS: 128}, blocking=True
        )

    assert bridge.command_verify == [False, False]
    unverified_warnings = [r for r in caplog.records if "unverified" in r.message]
    assert len(unverified_warnings) == 1

    # No echo is possible unverified, so nothing but a broadcast or a poll can
    # move the state -- the command alone must not.
    assert hass.states.get("light.room_a").attributes[ATTR_BRIGHTNESS] == 192


# -- finding 6: broadcasts before the first snapshot --------------------------


async def test_broadcast_before_first_snapshot_is_buffered_and_applied(
    hass, mock_config_entry, patch_bridge, patch_listener
) -> None:
    """A level broadcast cannot be recovered later, so hold it rather than drop it."""
    mock_config_entry.add_to_hass(hass)
    mock_config_entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)

    coordinator = RakoCoordinator(hass, mock_config_entry, bridge_device_id="test-bridge")
    coordinator._unsubscribe_listener = coordinator.listener.subscribe(
        coordinator._handle_status_message
    )
    await coordinator.listener.start()
    coordinator.bridge.attach_listener(coordinator.listener)

    coordinator.listener.emit(_level_broadcast(ROOM_KITCHEN, CHANNEL_KITCHEN_CEILING, 77))
    assert coordinator.data is None  # confirms it really did arrive before the snapshot

    await coordinator.async_config_entry_first_refresh()
    coordinator._async_apply_pending_messages()

    assert coordinator.data.channel_level(ROOM_KITCHEN, CHANNEL_KITCHEN_CEILING) == 77

    await coordinator.async_shutdown()


# -- finding 8: overall command timeout ---------------------------------------


async def test_command_exceeding_the_overall_cap_raises_cleanly(
    hass, rako_integration, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged bridge cannot hold a service call open indefinitely."""
    monkeypatch.setattr(coordinator_module, "COMMAND_TIMEOUT", 0.05)
    rako_integration.bridge.hang_forever = True

    with pytest.raises(HomeAssistantError, match="did not complete"):
        await rako_integration.coordinator.async_set_channel_level(
            ROOM_KITCHEN, CHANNEL_KITCHEN_CEILING, 200
        )


# -- finding 9: a socket rebinding is not evidence -----------------------------


async def test_listener_rebind_alone_does_not_restore_availability(
    hass, mock_config_entry, patch_bridge, patch_listener, created_bridges, created_listeners
) -> None:
    """Only a received message or a successful poll counts as evidence.

    Deliberately not the ``rako_integration`` fixture: it primes
    ``messages_received`` to a realistic steady-state baseline (see its
    docstring), which would make the push path already proven and defeat the
    point of this test. This one needs a listener that has genuinely heard
    nothing.
    """
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    bridge = created_bridges[-1]
    listener = created_listeners[-1]
    entity_id = "light.kitchen"

    listener.set_health(is_running=False)
    bridge.snapshot_error = RakoBridgeError("bridge unreachable")
    await _advance_past_poll_interval(hass)
    assert hass.states.get(entity_id).state == "unavailable"

    # The socket rebinds, but nothing has actually been heard or polled yet.
    listener.set_health(is_running=True)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unavailable"

    # Now a message actually arrives: that is evidence.
    listener.emit(_level_broadcast(ROOM_KITCHEN, CHANNEL_KITCHEN_CEILING, 5))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state != "unavailable"
