"""Rako shared models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry

if TYPE_CHECKING:
    from python_rako import ChannelLight, ChannelVentilation, RoomLight, RoomVentilation

    from .coordinator import RakoCoordinator


@dataclass
class RakoRuntimeData:
    """Everything one config entry owns at runtime.

    Lives on ``entry.runtime_data``; the integration keeps nothing in
    ``hass.data``.  Discovery runs once during setup and both platforms read
    the result from here, so a bridge that answers ``rako.xml`` with a login
    page fails the entry once rather than each platform separately.
    """

    coordinator: RakoCoordinator
    lights: list[RoomLight | ChannelLight] = field(default_factory=list)
    ventilation: list[RoomVentilation | ChannelVentilation] = field(
        default_factory=list
    )


type RakoConfigEntry = ConfigEntry[RakoRuntimeData]
