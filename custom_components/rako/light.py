"""Platform for light integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from python_rako import ChannelLight, RakoBridgeError, RoomLight
from python_rako.helpers import convert_to_scene

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.const import STATE_ON
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import ATTR_ESTIMATED
from .entity import RakoEntity
from .helpers import LevelView, channel_level, room_level

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
    """Set up the Rako lights for a config entry."""
    coordinator = entry.runtime_data.coordinator
    session = async_get_clientsession(hass)

    try:
        lights, _ = await coordinator.bridge.discover_devices(session)
    except (RakoBridgeError, OSError, TimeoutError) as err:
        raise PlatformNotReady(f"Could not discover Rako lights: {err}") from err

    entities: list[RakoLight] = []
    for light in lights:
        if isinstance(light, ChannelLight):
            entities.append(RakoChannelLight(coordinator, light))
        elif isinstance(light, RoomLight):
            entities.append(RakoRoomLight(coordinator, light))

    _LOGGER.debug("Adding %d Rako light entities", len(entities))
    async_add_entities(entities)


class RakoLight(RakoEntity, LightEntity):
    """A Rako circuit or room, as a dimmable light."""

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def _level(self) -> LevelView:
        """Return the level to show, and whether it is an approximation."""
        raise NotImplementedError

    @property
    def brightness(self) -> int | None:
        """Brightness of the light, or ``None`` when it is unknown."""
        return self._level().brightness

    @property
    def is_on(self) -> bool | None:
        """Whether the light is on; ``None`` reads as unknown, not off."""
        brightness = self._level().brightness
        if brightness is None:
            return None
        return brightness > 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Flag state the bridge has not actually reported.

        True after a fade (the bridge broadcasts no level when a fade stops),
        for a level derived from a scene, and for state restored across a
        restart.
        """
        return {ATTR_ESTIMATED: self._level().estimated}

    def _restored_level(self, last_state: State) -> int | None:
        if last_state.state != STATE_ON:
            return 0
        brightness = last_state.attributes.get(ATTR_BRIGHTNESS)
        return int(brightness) if brightness is not None else 255

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
        await self.async_turn_on(**{ATTR_BRIGHTNESS: 0})


class RakoRoomLight(RakoLight):
    """A whole Rako room, where brightness selects a scene."""

    def __init__(self, coordinator: RakoCoordinator, light: RoomLight) -> None:
        """Initialise a room light."""
        super().__init__(
            coordinator,
            room_id=light.room_id,
            channel_id=light.channel_id,
            device_name=light.room_title,
            room_title=light.room_title,
        )

    def _level(self) -> LevelView:
        return room_level(self.coordinator.data, self._room_id)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Select the scene closest to the requested brightness."""
        brightness = kwargs.get(ATTR_BRIGHTNESS, 255)
        await self.coordinator.async_set_room_scene(
            self._room_id, convert_to_scene(brightness)
        )


class RakoChannelLight(RakoLight):
    """A single Rako lighting circuit."""

    def __init__(self, coordinator: RakoCoordinator, light: ChannelLight) -> None:
        """Initialise a channel light."""
        super().__init__(
            coordinator,
            room_id=light.room_id,
            channel_id=light.channel_id,
            device_name=f"{light.room_title} - {light.channel_name}",
            room_title=light.room_title,
        )

    def _level(self) -> LevelView:
        return channel_level(self.coordinator.data, self._room_id, self._channel_id)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Drive the circuit to a level."""
        brightness = kwargs.get(ATTR_BRIGHTNESS, 255)
        await self.coordinator.async_set_channel_level(
            self._room_id, self._channel_id, brightness
        )
