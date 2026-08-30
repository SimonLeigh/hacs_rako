"""Entry setup, unload, reload and the runtime_data/device plumbing (WP-2.1/2.2)."""

from __future__ import annotations

from python_rako import RakoBridgeError

from custom_components.rako import coordinator as coordinator_module
from custom_components.rako.const import CONF_POLL_INTERVAL, DOMAIN
from custom_components.rako.model import RakoRuntimeData
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr

from .conftest import TEST_MAC, TEST_NAME
from .fakes import FakeBridge, FakeStatusListener


async def test_setup_creates_runtime_data_and_starts_the_listener(hass, rako_integration) -> None:
    """Setup wires a typed ``runtime_data`` and starts the listener before the first poll."""
    entry = rako_integration.entry
    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, RakoRuntimeData)
    assert entry.runtime_data.coordinator is rako_integration.coordinator

    assert rako_integration.listener.start_calls == 1
    assert rako_integration.bridge.listener is rako_integration.listener
    assert rako_integration.bridge.get_state_snapshot_calls == 1
    assert rako_integration.coordinator.data is not None


async def test_setup_creates_the_bridge_device(hass, rako_integration) -> None:
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(identifiers={(DOMAIN, TEST_MAC)})
    assert device is not None
    assert device.manufacturer == "Rako"
    assert device.name == TEST_NAME
    assert (dr.CONNECTION_NETWORK_MAC, TEST_MAC) in device.connections


async def test_unload_stops_listener_and_closes_the_bridge(hass, rako_integration) -> None:
    entry = rako_integration.entry
    bridge = rako_integration.bridge
    listener = rako_integration.listener

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert listener.stop_calls == 1
    assert bridge.detached is True
    assert bridge.closed is True


async def test_reload_rebuilds_the_coordinator(hass, rako_integration, created_bridges) -> None:
    entry = rako_integration.entry
    first_bridge = rako_integration.bridge

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert first_bridge.closed is True
    assert created_bridges[-1] is not first_bridge
    assert entry.runtime_data.coordinator.bridge is created_bridges[-1]


async def test_options_update_triggers_a_reload(hass, rako_integration) -> None:
    """The update listener registered in __init__ reloads on an options change."""
    entry = rako_integration.entry
    first_bridge = rako_integration.bridge

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_POLL_INTERVAL: 120}
    )
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert first_bridge.closed is True
    assert entry.runtime_data.coordinator is not None


async def test_setup_failure_still_releases_the_bridge(
    hass, mock_config_entry, monkeypatch, created_bridges, created_listeners
) -> None:
    """A first-refresh failure must not leave the listener holding UDP 9761.

    ``__init__.async_setup_entry`` calls ``coordinator.async_shutdown()`` on any
    setup exception specifically so a retried setup never finds the port
    already bound by a listener from the failed attempt.
    """

    def failing_bridge_factory(*args, **kwargs):
        bridge = FakeBridge(*args, **kwargs)
        bridge.snapshot_error = RakoBridgeError("bridge unreachable")
        created_bridges.append(bridge)
        return bridge

    def listener_factory(*args, **kwargs):
        listener = FakeStatusListener(*args, **kwargs)
        created_listeners.append(listener)
        return listener

    monkeypatch.setattr(coordinator_module, "Bridge", failing_bridge_factory)
    monkeypatch.setattr(coordinator_module, "StatusListener", listener_factory)

    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert len(created_bridges) == 1
    bridge = created_bridges[0]
    listener = created_listeners[0]
    assert bridge.closed is True
    assert bridge.detached is True
    assert listener.stop_calls == 1
