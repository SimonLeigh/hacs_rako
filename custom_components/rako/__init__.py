"""The Rako integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.const import CONF_MAC, CONF_NAME, Platform
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, MANUFACTURER
from .coordinator import RakoCoordinator
from .model import RakoRuntimeData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .model import RakoConfigEntry

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.FAN, Platform.LIGHT]


async def async_setup_entry(hass: HomeAssistant, entry: RakoConfigEntry) -> bool:
    """Set up Rako from a config entry."""
    device_registry = dr.async_get(hass)
    bridge_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, entry.data[CONF_MAC])},
        identifiers={(DOMAIN, entry.data[CONF_MAC])},
        manufacturer=MANUFACTURER,
        name=entry.data[CONF_NAME],
    )

    coordinator = RakoCoordinator(hass, entry, bridge_device_id=bridge_device.id)
    try:
        await coordinator.async_setup()
    except Exception:
        # The listener may already be bound to UDP 9761; never leave it holding
        # the port when setup fails and Home Assistant retries.
        await coordinator.async_shutdown()
        raise

    entry.runtime_data = RakoRuntimeData(coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RakoConfigEntry) -> bool:
    """Unload a config entry.

    The coordinator registers its own shutdown with the entry, which stops the
    listener and releases the bridge's sockets once the platforms are gone.
    """
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: RakoConfigEntry) -> None:
    """Reload the entry after its options changed."""
    await hass.config_entries.async_reload(entry.entry_id)
