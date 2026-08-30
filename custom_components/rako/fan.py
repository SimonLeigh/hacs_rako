"""Platform for fan integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from python_rako import ChannelVentilation, RakoBridgeError, RoomVentilation
from python_rako.helpers import convert_to_scene

from homeassistant.components.fan import ATTR_PERCENTAGE, FanEntity, FanEntityFeature
from homeassistant.const import STATE_ON
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import ATTR_ESTIMATED
from .entity import RakoEntity
from .helpers import (
    LevelView,
    brightness_to_percentage,
    channel_level,
    percentage_to_brightness,
    room_level,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, State
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import RakoCoordinator
    from .model import RakoConfigEntry

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RakoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Rako ventilation entities for a config entry."""
    coordinator = entry.runtime_data.coordinator
    session = async_get_clientsession(hass)

    try:
        _, ventilation = await coordinator.bridge.discover_devices(session)
    except (RakoBridgeError, OSError, TimeoutError) as err:
        raise PlatformNotReady(f"Could not discover Rako ventilation: {err}") from err

    entities: list[RakoFan] = []
    for vent in ventilation:
        if isinstance(vent, ChannelVentilation):
            entities.append(RakoChannelFan(coordinator, vent))
        elif isinstance(vent, RoomVentilation):
            entities.append(RakoRoomFan(coordinator, vent))

    _LOGGER.debug("Adding %d Rako fan entities", len(entities))
    async_add_entities(entities)


class RakoFan(RakoEntity, FanEntity):
    """A Rako ventilation circuit or room, as a fan."""

    _attr_supported_features = FanEntityFeature.TURN_OFF | FanEntityFeature.TURN_ON

    def _level(self) -> LevelView:
        """Return the level to show, and whether it is an approximation."""
        raise NotImplementedError

    @property
    def percentage(self) -> int | None:
        """Fan speed as a percentage, or ``None`` when it is unknown."""
        return brightness_to_percentage(self._level().brightness)

    @property
    def is_on(self) -> bool | None:
        """Whether the fan is running; ``None`` reads as unknown, not off."""
        percentage = self.percentage
        if percentage is None:
            return None
        return percentage > 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Flag state the bridge has not actually reported."""
        return {ATTR_ESTIMATED: self._level().estimated}

    def _restored_level(self, last_state: State) -> int | None:
        if last_state.state != STATE_ON:
            return 0
        percentage = last_state.attributes.get(ATTR_PERCENTAGE)
        if percentage is None:
            return 255
        return percentage_to_brightness(int(percentage))

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Turn on the fan."""
        await self.async_set_percentage(100 if percentage is None else percentage)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the fan."""
        await self.async_set_percentage(0)


class RakoRoomFan(RakoFan):
    """A whole Rako ventilation room, where speed selects a scene."""

    def __init__(
        self, coordinator: RakoCoordinator, ventilation: RoomVentilation
    ) -> None:
        """Initialise a room fan."""
        super().__init__(
            coordinator,
            room_id=ventilation.room_id,
            channel_id=ventilation.channel_id,
            device_name=f"{ventilation.room_title} Fan",
            room_title=ventilation.room_title,
        )

    def _level(self) -> LevelView:
        return room_level(self.coordinator.data, self._room_id)

    async def async_set_percentage(self, percentage: int) -> None:
        """Select the scene closest to the requested speed."""
        scene = convert_to_scene(percentage_to_brightness(percentage))
        await self.coordinator.async_set_room_scene(self._room_id, scene)


class RakoChannelFan(RakoFan):
    """A single Rako ventilation circuit."""

    def __init__(
        self, coordinator: RakoCoordinator, ventilation: ChannelVentilation
    ) -> None:
        """Initialise a channel fan."""
        super().__init__(
            coordinator,
            room_id=ventilation.room_id,
            channel_id=ventilation.channel_id,
            device_name=f"{ventilation.room_title} - {ventilation.channel_name}",
            room_title=ventilation.room_title,
        )

    def _level(self) -> LevelView:
        return channel_level(self.coordinator.data, self._room_id, self._channel_id)

    async def async_set_percentage(self, percentage: int) -> None:
        """Drive the circuit to a speed."""
        await self.coordinator.async_set_channel_level(
            self._room_id, self._channel_id, percentage_to_brightness(percentage)
        )
