"""Config and options flows (WP-2.3): discovery, manual entry, reconfigure, options."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from python_rako import RakoBridgeError

from custom_components.rako import config_flow as config_flow_module
from custom_components.rako.const import (
    CONF_MIN_COMMAND_INTERVAL,
    CONF_POLL_INTERVAL,
    CONF_TRANSPORT,
    DOMAIN,
    TRANSPORT_HTTP,
)
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from .conftest import TEST_HOST, TEST_MAC, TEST_NAME, TEST_PORT
from .fakes import default_bridge_info


@pytest.fixture
def patch_discover_bridge(monkeypatch: pytest.MonkeyPatch):
    """A configurable stand-in for ``discover_bridge()``."""
    mock = AsyncMock(
        return_value={"host": TEST_HOST, "port": TEST_PORT, "name": TEST_NAME, "mac": TEST_MAC}
    )
    monkeypatch.setattr(config_flow_module, "discover_bridge", mock)
    return mock


@pytest.fixture
def patch_flow_bridge(monkeypatch: pytest.MonkeyPatch):
    """A configurable stand-in for the throwaway ``Bridge`` the flow probes with."""
    get_info = AsyncMock(return_value=default_bridge_info())
    close = AsyncMock()
    instance = SimpleNamespace(get_info=get_info, close=close)

    monkeypatch.setattr(config_flow_module, "Bridge", lambda *args, **kwargs: instance)
    return get_info


async def test_discovery_success_prefills_the_form(
    hass, patch_discover_bridge, patch_flow_bridge
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}
    prefilled = {field.schema: field.default() for field in result["data_schema"].schema}
    assert prefilled["mac"] == TEST_MAC
    assert prefilled["host"] == TEST_HOST


async def test_discovery_failure_shows_the_form_with_an_error(
    hass, patch_discover_bridge, patch_flow_bridge
) -> None:
    patch_discover_bridge.side_effect = RakoBridgeError("no reply")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_devices_found"}


async def test_manual_entry_creates_an_entry(
    hass, patch_discover_bridge, patch_flow_bridge, patch_bridge, patch_listener
) -> None:
    # Completing the flow with CREATE_ENTRY makes Home Assistant set the new
    # entry up for real once the test yields control (at latest, when the
    # ``hass`` fixture drains pending tasks during teardown). ``patch_bridge``
    # / ``patch_listener`` keep that real setup off the network, the same way
    # ``rako_integration`` does -- otherwise the coordinator's real
    # ``Bridge``/``StatusListener`` open a genuine socket against the
    # placeholder test host, which newer pytest-homeassistant-custom-component
    # releases correctly flag as a socket opened during the test.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"host": TEST_HOST, "port": TEST_PORT, "name": TEST_NAME, "mac": TEST_MAC},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["mac"] == TEST_MAC
    assert result["data"]["host"] == TEST_HOST


async def test_manual_entry_cannot_connect(hass, patch_discover_bridge, patch_flow_bridge) -> None:
    patch_flow_bridge.side_effect = RakoBridgeError("refused")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"host": TEST_HOST, "port": TEST_PORT, "name": TEST_NAME, "mac": TEST_MAC},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_duplicate_mac_aborts(
    hass, mock_config_entry, patch_discover_bridge, patch_flow_bridge
) -> None:
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"host": TEST_HOST, "port": TEST_PORT, "name": TEST_NAME, "mac": TEST_MAC},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure_updates_host_and_port(
    hass, mock_config_entry, patch_flow_bridge, patch_bridge, patch_listener
) -> None:
    # See the comment on test_manual_entry_creates_an_entry: a successful
    # reconfigure reloads the entry, which sets it up for real unless the
    # coordinator's Bridge/StatusListener are patched too.
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    new_host = "192.0.2.20"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": new_host, "port": TEST_PORT}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data["host"] == new_host
    assert mock_config_entry.data["mac"] == TEST_MAC  # unique_id anchor never changes


async def test_reconfigure_cannot_connect_keeps_the_old_data(
    hass, mock_config_entry, patch_flow_bridge
) -> None:
    mock_config_entry.add_to_hass(hass)
    patch_flow_bridge.side_effect = RakoBridgeError("refused")

    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.0.2.30", "port": TEST_PORT}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert mock_config_entry.data["host"] == TEST_HOST  # unchanged


async def test_options_flow_round_trip_reloads_the_entry(hass, rako_integration) -> None:
    entry = rako_integration.entry
    first_bridge = rako_integration.bridge

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_POLL_INTERVAL: 120,
            CONF_TRANSPORT: TRANSPORT_HTTP,
            CONF_MIN_COMMAND_INTERVAL: 2.0,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_POLL_INTERVAL] == 120
    assert entry.options[CONF_TRANSPORT] == TRANSPORT_HTTP
    assert entry.options[CONF_MIN_COMMAND_INTERVAL] == 2.0

    # The reload the update listener triggers (test_init.py covers this
    # directly) rebuilt the coordinator against the new options.
    assert first_bridge.closed is True
    assert entry.runtime_data.coordinator.bridge is not first_bridge
    assert entry.runtime_data.coordinator.bridge.min_command_interval == 2.0
