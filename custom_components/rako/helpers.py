"""Pure functions over a bridge snapshot.

Kept free of Home Assistant's plumbing and of the bridge so the interesting
decisions -- which of two sources to believe, what "unknown" looks like, when a
poll is telling us nothing -- can be tested directly.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, NamedTuple

from python_rako import SceneStatusMessage, StateSource
from python_rako.helpers import convert_to_brightness

from homeassistant.util.percentage import (
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)

if TYPE_CHECKING:
    from python_rako import BridgeStateSnapshot, StatusMessage

#: Rako levels run 1-255 when on; 0 is off and is handled separately, exactly
#: as Home Assistant's fan helpers expect.
LEVEL_RANGE: tuple[float, float] = (1, 255)


class LevelView(NamedTuple):
    """A level and how much it should be trusted.

    ``brightness`` is ``None`` when the level is genuinely unknown -- after a
    fade, or for a room the bridge has dropped from its scene cache and that
    nothing has reported since.  That is *not* the same as off, and entities
    report it as unknown rather than inventing a zero.
    """

    brightness: int | None
    estimated: bool


UNKNOWN = LevelView(None, estimated=True)


def channel_level(
    snapshot: BridgeStateSnapshot, room_id: int, channel_id: int
) -> LevelView:
    """Return one channel's level, with its provenance collapsed to a flag."""
    state = snapshot.channel_state(room_id, channel_id)
    if state is None:
        return UNKNOWN
    return LevelView(state.level, state.is_estimated or state.level is None)


def room_level(snapshot: BridgeStateSnapshot, room_id: int) -> LevelView:
    """Return the level to show for a whole-room entity.

    A Rako room is controlled by *scene*, and this integration has always shown
    a room's scene as a brightness -- scene 1 is 255, scene 4 is 64, scene 0 is
    off -- so that moving the brightness slider picks a scene.  That mapping is
    preserved here.

    A room can also be driven to an absolute level (channel 0), by an app
    slider or by us; that is a true reported value rather than a scene, so it
    wins whenever it is fresher than the scene we are tracking.
    """
    room_state = snapshot.rooms.get(room_id)
    channel_state = snapshot.channel_state(room_id, 0)

    if (
        channel_state is not None
        and channel_state.level is not None
        and not channel_state.is_estimated
        and (room_state is None or channel_state.updated_at >= room_state.updated_at)
    ):
        return LevelView(channel_state.level, estimated=False)

    if room_state is not None and room_state.scene is not None:
        return LevelView(convert_to_brightness(room_state.scene), estimated=False)

    if channel_state is not None:
        return LevelView(channel_state.level, estimated=True)
    return UNKNOWN


def has_scene_data(snapshot: BridgeStateSnapshot) -> bool:
    """Whether a snapshot read from the bridge's caches says anything at all.

    A snapshot with no room in any scene is what an *unreachable* bridge
    produces: the HTTP scene read fails, the library falls back to the UDP
    query, and that returns an empty cache rather than raising.  Every room the
    level table knows about then appears with ``scene=None``, which reconcile
    reads as "we missed a scene change everywhere" and dutifully wipes the lot.

    An empty scene cache is never evidence, so it is treated as a failed read
    instead of as news.
    """
    return any(state.scene is not None for state in snapshot.rooms.values())


def apply_command_echo(
    snapshot: BridgeStateSnapshot, echo: StatusMessage
) -> BridgeStateSnapshot:
    """Apply the bridge's confirmation of a command we sent.

    ``COMMAND_ECHO`` provenance means "the bridge reported this exact value",
    and it must only be attached to a value the bridge actually reported.  A
    scene echo confirms the *scene*; the per-channel levels that follow from it
    are still the level table's approximation, and marking those as reported
    would let them outrank a real level broadcast and survive reconciliation
    for ever.  So a scene echo is applied with its natural provenance, and only
    a level echo carries ``COMMAND_ECHO``.
    """
    if isinstance(echo, SceneStatusMessage):
        return snapshot.apply(echo)
    return snapshot.apply(echo, source=StateSource.COMMAND_ECHO)


def brightness_to_percentage(brightness: int | None) -> int | None:
    """Convert a Rako level (0-255) to a fan percentage (0-100).

    Scaled with Home Assistant's own ranged-value helper so a Rako fan behaves
    like every other fan in Home Assistant, with one guard: that helper floors,
    so levels 1 and 2 come out as 0% and a running fan would report itself
    stopped.  A level above zero is never reported as 0%.
    """
    if brightness is None:
        return None
    if brightness <= 0:
        return 0
    return max(1, ranged_value_to_percentage(LEVEL_RANGE, brightness))


def percentage_to_brightness(percentage: int) -> int:
    """Convert a fan percentage (0-100) to a Rako level (0-255).

    Rounded up, so asking for 1% turns the circuit on rather than off.
    """
    if percentage <= 0:
        return 0
    return math.ceil(percentage_to_ranged_value(LEVEL_RANGE, percentage))
