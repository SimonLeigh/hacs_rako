"""Shared fixtures for the Rako integration test suite.

Everything here is deterministic: no real sockets, no real time. The bridge
and its status listener are faked (see ``tests/fakes.py``); state snapshots
are built with the real ``python_rako.state`` types so the tests exercise the
library's actual provenance rules, not a re-implementation of them.

Placeholder identity only -- this is a public repository. ``TEST_MAC``/
``TEST_HOST`` use TEST-NET-1 (RFC 5737) and a documentation MAC prefix; room
names are generic ("Kitchen", "Room A", ...), never real house rooms.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from python_rako import (
    ChannelLight,
    ChannelVentilation,
    LevelCache,
    LevelCacheItem,
    RoomChannel,
    RoomLight,
    RoomVentilation,
    SceneCache,
)

from custom_components.rako import coordinator as coordinator_module
from custom_components.rako.const import DOMAIN
from homeassistant.const import CONF_HOST, CONF_MAC, CONF_NAME, CONF_PORT

from .fakes import FakeBridge, FakeStatusListener

# -- placeholder identity -----------------------------------------------------

TEST_MAC = "00:11:22:33:44:55"
TEST_HOST = "192.0.2.10"
TEST_PORT = 9761
TEST_NAME = "Rako Bridge (Test)"

#: A room with both a whole-room light and one addressable channel.
ROOM_KITCHEN = 1
CHANNEL_KITCHEN_CEILING = 1
#: A room controlled only as a whole -- brightness selects a scene.
ROOM_A = 2
#: A whole-room fan.
ROOM_BATHROOM = 3
#: A room with one addressable ventilation channel.
ROOM_UTILITY = 4
CHANNEL_UTILITY_EXTRACT = 1
#: Fade-controlled: present in the level table but never in the scene cache,
#: exactly as BRIDGE_BEHAVIOUR.md fact 2 describes a room a fade button has
#: touched. Used by the restore-on-startup tests.
ROOM_CONSERVATORY = 5

_SCENE_LEVELS = {1: 255, 2: 192, 3: 128, 4: 64, 0: 0}


@pytest.fixture(autouse=True)
def enable_custom_integrations(enable_custom_integrations: Any) -> None:
    """Always allow loading this repo's ``custom_components/rako``."""
    return enable_custom_integrations


@pytest.fixture
def default_scene_cache() -> SceneCache:
    """Rooms 1 and 2 selected; room 5 (fade-controlled) absent by design."""
    return SceneCache({ROOM_KITCHEN: 1, ROOM_A: 2, ROOM_BATHROOM: 1, ROOM_UTILITY: 2})


@pytest.fixture
def default_level_cache() -> LevelCache:
    """A level table covering every default fixture room/channel."""
    cache = LevelCache()
    cache[RoomChannel(ROOM_KITCHEN, 0)] = LevelCacheItem(
        0, ROOM_KITCHEN, 0, dict(_SCENE_LEVELS)
    )
    cache[RoomChannel(ROOM_KITCHEN, CHANNEL_KITCHEN_CEILING)] = LevelCacheItem(
        0, ROOM_KITCHEN, CHANNEL_KITCHEN_CEILING, {1: 200, 2: 150, 3: 100, 4: 50, 0: 0}
    )
    cache[RoomChannel(ROOM_A, 0)] = LevelCacheItem(0, ROOM_A, 0, dict(_SCENE_LEVELS))
    cache[RoomChannel(ROOM_BATHROOM, 0)] = LevelCacheItem(
        0, ROOM_BATHROOM, 0, dict(_SCENE_LEVELS)
    )
    cache[RoomChannel(ROOM_UTILITY, CHANNEL_UTILITY_EXTRACT)] = LevelCacheItem(
        0, ROOM_UTILITY, CHANNEL_UTILITY_EXTRACT, dict(_SCENE_LEVELS)
    )
    cache[RoomChannel(ROOM_CONSERVATORY, 0)] = LevelCacheItem(
        0, ROOM_CONSERVATORY, 0, dict(_SCENE_LEVELS)
    )
    return cache


@pytest.fixture
def default_lights() -> list[Any]:
    return [
        RoomLight(room_id=ROOM_KITCHEN, room_title="Kitchen"),
        ChannelLight(
            room_id=ROOM_KITCHEN,
            room_title="Kitchen",
            channel_id=CHANNEL_KITCHEN_CEILING,
            channel_type="dimmer",
            channel_name="Ceiling",
            channel_levels="255,200,150,100,50,0",
        ),
        RoomLight(room_id=ROOM_A, room_title="Room A"),
        RoomLight(room_id=ROOM_CONSERVATORY, room_title="Conservatory"),
    ]


@pytest.fixture
def default_ventilation() -> list[Any]:
    return [
        RoomVentilation(room_id=ROOM_BATHROOM, room_title="Bathroom"),
        ChannelVentilation(
            room_id=ROOM_UTILITY,
            room_title="Utility",
            channel_id=CHANNEL_UTILITY_EXTRACT,
            channel_type="fan",
            channel_name="Extract",
            channel_levels="255,0",
        ),
    ]


@pytest.fixture
def created_bridges() -> list[FakeBridge]:
    """Every :class:`FakeBridge` a patched ``Bridge()`` call has produced."""
    return []


@pytest.fixture
def created_listeners() -> list[FakeStatusListener]:
    """Every :class:`FakeStatusListener` a patched ``StatusListener()`` call has produced."""
    return []


@pytest.fixture
def patch_bridge(
    monkeypatch: pytest.MonkeyPatch,
    created_bridges: list[FakeBridge],
    default_scene_cache: SceneCache,
    default_level_cache: LevelCache,
    default_lights: list[Any],
    default_ventilation: list[Any],
) -> None:
    """Replace ``coordinator.Bridge`` with a factory producing fakes.

    Each fake starts pre-loaded with the fixture rooms above; tests that need
    something different mutate the returned instance's ``scene_cache`` /
    ``level_cache`` / ``devices`` after setup and trigger a poll, which is how
    the real coordinator would learn about a change too.
    """

    def factory(*args: Any, **kwargs: Any) -> FakeBridge:
        bridge = FakeBridge(*args, **kwargs)
        bridge.scene_cache = SceneCache(default_scene_cache)
        bridge.level_cache = LevelCache(default_level_cache)
        bridge.devices = (list(default_lights), list(default_ventilation))
        created_bridges.append(bridge)
        return bridge

    monkeypatch.setattr(coordinator_module, "Bridge", factory)


@pytest.fixture
def patch_listener(
    monkeypatch: pytest.MonkeyPatch, created_listeners: list[FakeStatusListener]
) -> None:
    """Replace ``coordinator.StatusListener`` with a factory producing fakes."""

    def factory(*args: Any, **kwargs: Any) -> FakeStatusListener:
        listener = FakeStatusListener(*args, **kwargs)
        created_listeners.append(listener)
        return listener

    monkeypatch.setattr(coordinator_module, "StatusListener", factory)


@pytest.fixture
def mock_config_entry() -> Any:
    """A standard, not-yet-added Rako config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_MAC,
        title=f"Rako Bridge ({TEST_NAME})",
        data={
            CONF_HOST: TEST_HOST,
            CONF_PORT: TEST_PORT,
            CONF_MAC: TEST_MAC,
            CONF_NAME: TEST_NAME,
        },
    )


@pytest.fixture
async def rako_integration(
    hass: Any,
    mock_config_entry: Any,
    patch_bridge: None,
    patch_listener: None,
    created_bridges: list[FakeBridge],
    created_listeners: list[FakeStatusListener],
) -> Any:
    """Set the integration up end to end, against the fakes.

    Returns a namespace with the entry, its coordinator, and the fake bridge
    and listener the coordinator built -- everything a test needs to inject
    broadcasts, flip listener health, or inspect commands sent.
    """
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    return SimpleNamespace(
        entry=mock_config_entry,
        coordinator=mock_config_entry.runtime_data.coordinator,
        bridge=created_bridges[-1],
        listener=created_listeners[-1],
    )
