"""Base entity for the Rako integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import RakoCoordinator
from .util import create_unique_id

if TYPE_CHECKING:
    from homeassistant.core import State


class RakoEntity(CoordinatorEntity[RakoCoordinator], RestoreEntity):
    """A view over one room or channel in the coordinator's snapshot.

    Entities hold no state of their own: everything they show is read from the
    snapshot when Home Assistant asks.  Availability comes from the coordinator,
    which reports failure only when both the push and the poll paths are down.
    """

    _attr_has_entity_name = True
    #: This is the only entity on its device, so it takes the device's name --
    #: which is the name the entity has always had.
    _attr_name = None

    def __init__(
        self,
        coordinator: RakoCoordinator,
        *,
        room_id: int,
        channel_id: int,
        device_name: str,
        room_title: str,
    ) -> None:
        """Register the entity against its own device under the bridge."""
        super().__init__(coordinator)
        self._room_id = room_id
        self._channel_id = channel_id
        # Unchanged from previous releases: existing entity registry entries
        # (and therefore entity_ids and automations) must survive the upgrade.
        self._attr_unique_id = create_unique_id(coordinator.mac, room_id, channel_id)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            name=device_name,
            manufacturer=MANUFACTURER,
            suggested_area=room_title,
            via_device=(DOMAIN, coordinator.mac),
        )

    async def async_added_to_hass(self) -> None:
        """Seed unknown state from what this entity showed before the restart.

        Only for channels the bridge has told us nothing about: a fade-
        controlled room is missing from the bridge's scene cache by design, and
        reading that absence as "off" is what used to make those rooms appear
        switched off after every restart.
        """
        await super().async_added_to_hass()
        if self.coordinator.data is None:
            return
        if (
            self.coordinator.data.channel_level(self._room_id, self._channel_id)
            is not None
        ):
            return
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return
        level = self._restored_level(last_state)
        if level is not None:
            self.coordinator.async_restore_channel_level(
                self._room_id, self._channel_id, level
            )

    def _restored_level(self, last_state: State) -> int | None:
        """Return the Rako level (0-255) implied by a restored HA state."""
        raise NotImplementedError
