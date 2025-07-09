"""Module representing a Rako Bridge."""

from __future__ import annotations

import asyncio
from asyncio import Task
import contextlib
import logging

from python_rako.bridge import Bridge
from python_rako.helpers import convert_to_brightness, get_dg_listener
from python_rako.model import ChannelStatusMessage, SceneStatusMessage, StatusMessage

from homeassistant.core import HomeAssistant

from custom_components.rako.fan import RakoFan

from .const import DOMAIN
from .light import RakoLight
from .model import RakoDomainEntryData
from .util import create_unique_id

_LOGGER = logging.getLogger(__name__)


class RakoBridge(Bridge):
    """Represents a Rako Bridge."""

    def __init__(
        self,
        host: str,
        port: int,
        name: str,
        mac: str,
        entry_id: str,
        hass: HomeAssistant,
    ) -> None:
        """Init subclass of python_rako Bridge."""
        super().__init__(host, port, name, mac)
        self.entry_id = entry_id
        self.hass = hass

    @property
    def _light_map(self) -> dict[str, RakoLight]:
        rako_domain_entry_data: RakoDomainEntryData = self.hass.data[DOMAIN][self.mac]
        return rako_domain_entry_data["rako_light_map"]

    @property
    def _fan_map(self) -> dict[str, RakoFan]:
        rako_domain_entry_data: RakoDomainEntryData = self.hass.data[DOMAIN][self.mac]
        return rako_domain_entry_data.get("rako_fan_map", {})

    @property
    def _entity_map(self) -> dict[str, any]:
        """Return combined map of all listening entities (lights and fans)."""
        entity_map = {}
        entity_map.update(self._light_map)
        entity_map.update(self._fan_map)
        return entity_map

    @property
    def _listener_task(self) -> Task | None:
        rako_domain_entry_data: RakoDomainEntryData = self.hass.data[DOMAIN][self.mac]
        return rako_domain_entry_data["rako_listener_task"]

    @_listener_task.setter
    def _listener_task(self, task: Task) -> None:
        rako_domain_entry_data: RakoDomainEntryData = self.hass.data[DOMAIN][self.mac]
        rako_domain_entry_data["rako_listener_task"] = task

    def get_listening_light(self, light_unique_id: str) -> RakoLight | None:
        """Return the Light, if listening."""
        light_map = self._light_map
        return light_map.get(light_unique_id)

    def get_listening_entity(self, entity_unique_id: str):
        """Return any listening entity (light or fan)."""
        entity_map = self._entity_map
        return entity_map.get(entity_unique_id)

    def _add_listening_light(self, light: RakoLight) -> None:
        light_map = self._light_map
        light_map[light.unique_id] = light

    def _remove_listening_light(self, light: RakoLight) -> None:
        light_map = self._light_map
        if light.unique_id in light_map:
            del light_map[light.unique_id]

    def _add_listening_fan(self, fan) -> None:
        fan_map = self._fan_map
        fan_map[fan.unique_id] = fan

    def _remove_listening_fan(self, fan) -> None:
        fan_map = self._fan_map
        if fan.unique_id in fan_map:
            del fan_map[fan.unique_id]

    async def listen_for_state_updates(self) -> None:
        """Background task to listen for state updates."""
        self._listener_task: Task = asyncio.create_task(
            listen_for_state_updates(self), name=f"rako_{self.mac}_listener_task"
        )

    async def stop_listening_for_state_updates(self) -> None:
        """Background task to stop listening for state updates."""
        if listener_task := self._listener_task:
            listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener_task

    async def register_for_state_updates(self, entity) -> None:
        """Register an entity to listen for state updates."""
        if hasattr(entity, "brightness"):  # Light entity
            self._add_listening_light(entity)
        else:  # Fan entity
            self._add_listening_fan(entity)

        if len(self._entity_map) == 1:
            await self.listen_for_state_updates()

    async def deregister_for_state_updates(self, entity) -> None:
        """Deregister an entity to listen for state updates."""
        if hasattr(entity, "brightness"):  # Light entity
            self._remove_listening_light(entity)
        else:  # Fan entity
            self._remove_listening_fan(entity)

        if not self._entity_map:
            await self.stop_listening_for_state_updates()


def _state_update(bridge: RakoBridge, status_message: StatusMessage) -> None:
    light_unique_id = create_unique_id(
        bridge.mac, status_message.room, status_message.channel
    )
    brightness = 0
    if isinstance(status_message, ChannelStatusMessage):
        brightness = status_message.brightness
    elif isinstance(status_message, SceneStatusMessage):
        for _channel, _brightness in bridge.level_cache.get_channel_levels(
            status_message.room, status_message.scene
        ):
            _msg = ChannelStatusMessage(status_message.room, _channel, _brightness)
            _state_update(bridge, _msg)
        brightness = convert_to_brightness(status_message.scene)

    # Update both lights and fans with the same unique_id pattern
    listening_entity = bridge.get_listening_entity(light_unique_id)
    if listening_entity:
        if hasattr(listening_entity, "brightness"):  # Light entity
            listening_entity.brightness = brightness
        elif hasattr(listening_entity, "percentage"):  # Fan entity
            # Convert brightness (0-255) to percentage (0-100)
            percentage = int((brightness / 255) * 100) if brightness > 0 else 0
            listening_entity.percentage = percentage
    else:
        _LOGGER.debug("Entity not listening: %s", status_message)


async def listen_for_state_updates(bridge: RakoBridge) -> None:
    """Listen for state updates worker method."""
    async with get_dg_listener(bridge.port) as listener:
        while True:
            message = await bridge.next_pushed_message(listener)
            if message and isinstance(message, StatusMessage):
                _state_update(bridge, message)
