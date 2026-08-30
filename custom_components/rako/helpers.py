"""Pure functions turning a bridge snapshot into entity state.

Kept free of Home Assistant and of the bridge so the interesting decisions --
which of two sources to believe, what "unknown" looks like -- can be tested
directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from python_rako.helpers import convert_to_brightness

if TYPE_CHECKING:
    from python_rako import BridgeStateSnapshot


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


def brightness_to_percentage(brightness: int | None) -> int | None:
    """Convert a Rako level (0-255) to a fan percentage (0-100)."""
    if brightness is None:
        return None
    return int(brightness / 255 * 100) if brightness > 0 else 0


def percentage_to_brightness(percentage: int) -> int:
    """Convert a fan percentage (0-100) to a Rako level (0-255)."""
    return int(percentage / 100 * 255) if percentage > 0 else 0
