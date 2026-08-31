"""Startup state for fade-controlled rooms (BRIDGE_BEHAVIOUR.md facts 2-4).

A fade button deletes its room from the bridge's scene cache, so a room like
``ROOM_CONSERVATORY`` -- present in the level table but never in the scene
cache in these fixtures -- is exactly what a fade-controlled room looks like
at startup. Reading that absence as "off" (the old behaviour) is the bug this
restore path fixes: it seeds the entity from Home Assistant's own last-known
state instead, flagged ``estimated``, until the bridge actually reports
something.

``mock_restore_cache`` must be primed *before* the config entry is set up, so
these tests build the entry manually rather than using the ``rako_integration``
fixture (which sets up immediately).
"""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import mock_restore_cache
from python_rako import ChannelStatusMessage, CommandType, MessageOrigin

from homeassistant.components.light import ATTR_BRIGHTNESS
from homeassistant.const import STATE_ON
from homeassistant.core import State

from .conftest import ROOM_CONSERVATORY

ENTITY_ID = "light.conservatory"


async def _setup(hass, mock_config_entry, created_bridges, created_listeners):
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return created_bridges[-1], created_listeners[-1]


async def test_fade_controlled_room_restores_its_last_known_state(
    hass, mock_config_entry, patch_bridge, patch_listener, created_bridges, created_listeners
) -> None:
    mock_restore_cache(
        hass,
        [_last_state(state=STATE_ON, brightness=180)],
    )

    await _setup(hass, mock_config_entry, created_bridges, created_listeners)

    state = hass.states.get(ENTITY_ID)
    assert state.state == "on"
    assert state.attributes[ATTR_BRIGHTNESS] == 180
    assert state.attributes["estimated"] is True


async def test_fade_controlled_room_restores_off(
    hass, mock_config_entry, patch_bridge, patch_listener, created_bridges, created_listeners
) -> None:
    mock_restore_cache(hass, [_last_state(state="off")])

    await _setup(hass, mock_config_entry, created_bridges, created_listeners)

    state = hass.states.get(ENTITY_ID)
    assert state.state == "off"
    assert state.attributes["estimated"] is True


async def test_restored_state_is_replaced_by_the_first_real_broadcast(
    hass, mock_config_entry, patch_bridge, patch_listener, created_bridges, created_listeners
) -> None:
    mock_restore_cache(hass, [_last_state(state=STATE_ON, brightness=180)])
    _bridge, listener = await _setup(hass, mock_config_entry, created_bridges, created_listeners)

    assert hass.states.get(ENTITY_ID).attributes["estimated"] is True

    listener.emit(
        ChannelStatusMessage(
            room=ROOM_CONSERVATORY,
            channel=0,
            brightness=90,
            command=CommandType.SET_LEVEL,
            data=(1, 90),
            origin=MessageOrigin.CONTROL,
        )
    )
    await hass.async_block_till_done()

    state = hass.states.get(ENTITY_ID)
    assert state.attributes[ATTR_BRIGHTNESS] == 90
    assert state.attributes["estimated"] is False


async def test_a_room_the_bridge_does_know_about_ignores_the_restored_state(
    hass, mock_config_entry, patch_bridge, patch_listener, created_bridges, created_listeners
) -> None:
    """Restore only fills in genuinely unknown channels, never a known one."""
    mock_restore_cache(hass, [_last_state("light.room_a", state=STATE_ON, brightness=7)])

    await _setup(hass, mock_config_entry, created_bridges, created_listeners)

    # Room A is in the fixture's scene cache (scene 2 -> 192), so the restored
    # 7 must never have been applied.
    state = hass.states.get("light.room_a")
    assert state.attributes[ATTR_BRIGHTNESS] == 192
    assert state.attributes["estimated"] is False


def _last_state(entity_id: str = ENTITY_ID, *, state: str, brightness: int | None = None) -> State:
    attributes = {ATTR_BRIGHTNESS: brightness} if brightness is not None else {}
    return State(entity_id, state, attributes)
