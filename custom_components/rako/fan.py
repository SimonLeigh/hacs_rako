"""Platform for fan integration."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import python_rako
from python_rako.exceptions import RakoBridgeError
from python_rako.helpers import convert_to_brightness, convert_to_scene

from homeassistant.components.fan import (
    ATTR_PERCENTAGE,
    FanEntity,
    FanEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .util import create_unique_id

if TYPE_CHECKING:
    from .bridge import RakoBridge
    from .model import RakoDomainEntryData

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the config entry."""
    rako_domain_entry_data: RakoDomainEntryData = hass.data[DOMAIN][entry.unique_id]
    bridge = rako_domain_entry_data["rako_bridge_client"]

    hass_fans: list[Entity] = []
    session = async_get_clientsession(hass)

    try:
        _LOGGER.debug("Starting ventilation discovery for bridge %s", bridge.host)
        
        # Debug: Get the raw XML first to see what's being processed
        try:
            rako_xml = await bridge.get_rako_xml(session)
            _LOGGER.debug("Retrieved XML length: %d", len(rako_xml))
            _LOGGER.debug("XML first 200 chars: %s", repr(rako_xml[:200]))
            _LOGGER.debug("XML last 200 chars: %s", repr(rako_xml[-200:]))
        except Exception as xml_error:
            _LOGGER.error("Failed to get XML: %s", xml_error)
            raise
        
        # Now try the discovery
        async for ventilation in bridge.discover_ventilation(session):
            if isinstance(ventilation, python_rako.ChannelVentilation):
                hass_fan: RakoFan = RakoChannelFan(bridge, ventilation)
            elif isinstance(ventilation, python_rako.RoomVentilation):
                hass_fan = RakoRoomFan(bridge, ventilation)
            else:
                continue

            hass_fans.append(hass_fan)
            _LOGGER.debug("Added fan: %s", hass_fan.name)

    except Exception as e:
        _LOGGER.error("Error during ventilation discovery: %s", e)
        import traceback
        _LOGGER.error("Full traceback: %s", traceback.format_exc())
        # Continue without adding fan entities if discovery fails

    _LOGGER.info("Added %d fan entities", len(hass_fans))
    async_add_entities(hass_fans, True)


class RakoFan(FanEntity):
    """Representation of a Rako Fan."""

    def __init__(self, bridge: RakoBridge, ventilation: python_rako.Ventilation) -> None:
        """Initialize a RakoFan."""
        self.bridge = bridge
        self._ventilation = ventilation
        self._percentage = self._init_get_percentage_from_cache()
        self._available = True
        self._attr_supported_features = FanEntityFeature.SET_SPEED

    @property
    def name(self) -> str:
        """Return the display name of this fan."""
        raise NotImplementedError

    def _init_get_percentage_from_cache(self) -> int:
        raise NotImplementedError

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await self.bridge.register_for_state_updates(self)

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await self.bridge.deregister_for_state_updates(self)

    @property
    def unique_id(self) -> str:
        """Fan's unique ID."""
        return create_unique_id(
            self.bridge.mac, self._ventilation.room_id, self._ventilation.channel_id
        )

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._available

    @property
    def percentage(self) -> int | None:
        """Return the current speed percentage."""
        return self._percentage

    @percentage.setter
    def percentage(self, value: int) -> None:
        """Set the percentage. Used when state is updated outside Home Assistant."""
        self._percentage = value
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """Return true if fan is on."""
        return self.percentage is not None and self.percentage > 0

    @property
    def should_poll(self) -> bool:
        """Entity pushes its state to HA."""
        return False

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the fan."""
        await self.async_set_percentage(0)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this Rako Fan."""
        return {
            "identifiers": {(DOMAIN, self.unique_id)},
            "name": self.name,
            "manufacturer": "Rako",
            "suggested_area": self._ventilation.room_title,
            "via_device": (DOMAIN, self.bridge.mac),
        }


class RakoRoomFan(RakoFan):
    """Representation of a Rako Room Fan."""

    def __init__(self, bridge: RakoBridge, ventilation: python_rako.RoomVentilation) -> None:
        """Initialize a RakoRoomFan."""
        super().__init__(bridge, ventilation)
        self._ventilation: python_rako.RoomVentilation = ventilation

    def _init_get_percentage_from_cache(self) -> int:
        scene_of_room = self.bridge.scene_cache.get(self._ventilation.room_id, 0)
        brightness: int = convert_to_brightness(scene_of_room)
        # Convert brightness (0-255) to percentage (0-100)
        return int((brightness / 255) * 100) if brightness > 0 else 0

    @property
    def name(self) -> str:
        """Return the display name of this fan."""
        room_title: str = self._ventilation.room_title
        return f"{room_title} Fan"

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the speed percentage of the fan."""
        try:
            # Convert percentage (0-100) to brightness (0-255)
            brightness = int((percentage / 100) * 255) if percentage > 0 else 0
            scene = convert_to_scene(brightness)
            await asyncio.wait_for(
                self.bridge.set_room_scene(self._ventilation.room_id, scene), timeout=3.0
            )
            # Update local state immediately after successful command
            self._percentage = percentage
            self._available = True
            self.async_write_ha_state()

        except (RakoBridgeError, TimeoutError):
            if self._available:
                _LOGGER.exception("An error occurred while updating the Rako Fan")
            self._available = False
            self.async_write_ha_state()

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Turn on the fan."""
        if percentage is None:
            percentage = 100
        await self.async_set_percentage(percentage)


class RakoChannelFan(RakoFan):
    """Representation of a Rako Channel Fan."""

    def __init__(self, bridge: RakoBridge, ventilation: python_rako.ChannelVentilation) -> None:
        """Initialize a RakoChannelFan."""
        super().__init__(bridge, ventilation)
        self._ventilation: python_rako.ChannelVentilation = ventilation

    def _init_get_percentage_from_cache(self) -> int:
        scene_of_room = self.bridge.scene_cache.get(self._ventilation.room_id, 0)
        brightness: int = self.bridge.level_cache.get_channel_level(
            self._ventilation.room_channel, scene_of_room
        )
        # Convert brightness (0-255) to percentage (0-100)
        return int((brightness / 255) * 100) if brightness > 0 else 0

    @property
    def name(self) -> str:
        """Return the display name of this fan."""
        return f"{self._ventilation.room_title} - {self._ventilation.channel_name}"

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the speed percentage of the fan."""
        try:
            # Convert percentage (0-100) to brightness (0-255)
            brightness = int((percentage / 100) * 255) if percentage > 0 else 0
            await asyncio.wait_for(
                self.bridge.set_channel_brightness(
                    self._ventilation.room_id, self._ventilation.channel_id, brightness
                ),
                timeout=3.0,
            )
            # Update local state immediately after successful command
            self._percentage = percentage
            self._available = True
            self.async_write_ha_state()

        except (RakoBridgeError, TimeoutError):
            if self._available:
                _LOGGER.exception("An error occurred while updating the Rako Fan")
            self._available = False
            self.async_write_ha_state()

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Turn on the fan."""
        if percentage is None:
            percentage = 100
        await self.async_set_percentage(percentage)