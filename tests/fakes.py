"""Fakes standing in for python_rako's ``Bridge`` and ``StatusListener``.

The coordinator constructs both directly (``Bridge(...)``, ``StatusListener(...)``),
so tests patch the class objects imported into ``custom_components.rako.coordinator``
(and ``.config_flow``) rather than injecting collaborators. These fakes mirror the
real classes' public surface closely enough that the tests exercise the same
contract the coordinator relies on:

* ``StatusListener``: ``subscribe()`` returns an unsubscribe handle, ``start``/
  ``stop`` are coroutines, health changes are reported through the
  ``on_health_change`` callback passed at construction (never polled), and
  ``.health.messages_received`` is the evidence
  ``RakoCoordinator._push_path_proven`` gates verification and
  failed-poll-tolerance on (review findings 4/9).
* ``Bridge``: ``set_room_scene``/``set_channel_level`` take a ``verify`` flag,
  exactly like the real ``Bridge`` methods -- ``verify=True`` is echo-verified,
  returning the bridge's echo or raising ``RakoCommandError`` when nothing
  confirmed it (BRIDGE_BEHAVIOUR.md facts 10-12); ``verify=False`` sends and
  returns ``None`` without confirming anything, the real behaviour when the
  coordinator judges the push path unproven. ``get_state_snapshot`` returns a
  real ``BridgeStateSnapshot`` built from ``python_rako.state`` types, never a
  mock.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from python_rako import (
    BridgeInfo,
    ChannelStatusMessage,
    CommandType,
    LevelCache,
    MessageOrigin,
    SceneCache,
    SceneStatusMessage,
)
from python_rako.state import BridgeStateSnapshot


async def _yield() -> None:
    """A single event-loop checkpoint.

    The real ``Bridge`` methods this stands in for all perform genuine network
    I/O, so callers that schedule them as eager background tasks (as the
    coordinator's level-table refresh does) rely on them actually suspending
    rather than running to completion synchronously. Without this, a fake
    that returns instantly changes the ordering the real bridge would have
    given.
    """
    await asyncio.sleep(0)


def default_bridge_info() -> BridgeInfo:
    """A plausible ``BridgeInfo`` for a WTC-Bridge, with placeholder identity."""
    return BridgeInfo(
        version="2.5.0 WTC",
        buildDate="Sep 26 2019",
        hostName="RAKOBRIDGE",
        hostIP="192.0.2.10",
        hostMAC="00:11:22:33:44:55",
        hwStatus="0",
        dbVersion="-18",
        requirepassword="0",
        passhash="",
        charset="utf-8",
    )


@dataclass
class _FakeListenerHealth:
    """A minimal stand-in for ``python_rako.ListenerHealth``.

    Only the attributes the coordinator actually reads.
    """

    is_running: bool
    restart_count: int = 0
    last_error: str | None = None
    messages_received: int = 0


class FakeStatusListener:
    """Stands in for :class:`python_rako.StatusListener`.

    Tests call :meth:`emit` to deliver a decoded status message to every
    subscriber, exactly as the real listener would after decoding a
    broadcast, and :meth:`set_health` to simulate the listener going up or
    down (the coordinator reacts only through the ``on_health_change``
    callback, never by polling).

    ``.health.messages_received`` -- read live by
    ``RakoCoordinator._push_path_proven``, never delivered through the health
    callback (the real listener does not publish health on every message
    either) -- increments on every non-duplicate :meth:`emit`.
    :meth:`mark_messages_received` bumps it directly, for a test that needs to
    establish "the push path is already proven" as a starting condition without
    the side effects (an applied snapshot, a fired event) a real message would
    have.
    """

    def __init__(
        self,
        bridge_host: str,
        port: int = 9761,
        *,
        listen_host: str = "0.0.0.0",  # noqa: S104 -- mirrors StatusListener; never binds a socket
        on_health_change: Any = None,
        **_kwargs: Any,
    ) -> None:
        self.bridge_host = bridge_host
        self.port = port
        self.listen_host = listen_host
        self._on_health_change = on_health_change
        self._subscriptions: list[list[Any]] = []
        self.start_calls = 0
        self.stop_calls = 0
        self.running = False
        self._restart_count = 0
        self._last_error: str | None = None
        self._messages_received = 0

    @property
    def health(self) -> _FakeListenerHealth:
        """A live snapshot, matching ``StatusListener.health`` being read on demand."""
        return _FakeListenerHealth(
            is_running=self.running,
            restart_count=self._restart_count,
            last_error=self._last_error,
            messages_received=self._messages_received,
        )

    def subscribe(self, callback: Any, *, include_duplicates: bool = False) -> Any:
        entry = [callback, include_duplicates]
        self._subscriptions.append(entry)

        def unsubscribe() -> None:
            if entry in self._subscriptions:
                self._subscriptions.remove(entry)

        return unsubscribe

    async def start(self) -> None:
        """Bind and report healthy, mirroring the real supervised listener."""
        self.start_calls += 1
        self.running = True
        self.set_health(is_running=True)

    async def stop(self) -> None:
        self.stop_calls += 1
        self.running = False
        self.set_health(is_running=False)

    def emit(self, message: Any, *, duplicate: bool = False) -> None:
        """Deliver ``message`` to every subscriber, as a real broadcast would."""
        if not duplicate:
            self._messages_received += 1
        for callback, include_duplicates in list(self._subscriptions):
            if duplicate and not include_duplicates:
                continue
            callback(message)

    def mark_messages_received(self, count: int = 1) -> None:
        """Bump the received-message count without dispatching anything.

        For establishing a "push path already proven" starting condition
        (review findings 4/9) without the side effects a real message
        delivery has -- an applied snapshot, a fired ``rako_event``.
        """
        self._messages_received += count

    def set_health(
        self,
        *,
        is_running: bool,
        restart_count: int = 0,
        last_error: str | None = None,
    ) -> None:
        """Simulate the listener's supervised loop going up or down."""
        self._restart_count = restart_count
        self._last_error = last_error
        if self._on_health_change is None:
            return
        self._on_health_change(
            _FakeListenerHealth(
                is_running=is_running,
                restart_count=restart_count,
                last_error=last_error,
                messages_received=self._messages_received,
            )
        )


class FakeBridge:
    """Stands in for :class:`python_rako.Bridge`.

    ``echoes`` maps a ``(room_id, channel_id)`` target to the outcome of the
    *next verified* command sent to it: a status message to echo back
    (including one that differs from what was requested, simulating pacing
    coalescing to a newer command), or an exception instance to raise
    (simulating silence after retry, i.e. ``RakoCommandError``). Left unset, a
    verified command echoes back exactly what was requested -- the common
    case. An *unverified* command (``verify=False``) is recorded and always
    returns ``None``, exactly like the real ``Bridge``.
    """

    def __init__(
        self,
        host: str,
        port: int,
        name: str,
        mac: str,
        commander: Any = None,
        **kwargs: Any,
    ) -> None:
        self.host = host
        self.port = port
        self.name = name
        self.mac = mac
        self.commander = commander
        self.min_command_interval = kwargs.get("min_command_interval")

        self.scene_cache = SceneCache()
        self.level_cache = LevelCache()
        self.devices = ([], [])
        self.info = default_bridge_info()

        self.echoes = {}
        self.commands = []
        #: verify flag actually used for each entry in .commands, same order/index.
        self.command_verify: list[bool] = []
        self.snapshot_error = None
        #: Raised by discover_devices, e.g. ExpatError/KeyError for a bad rako.xml.
        self.discover_error: Exception | None = None
        #: A command that never returns, for exercising the coordinator's
        #: overall command timeout. Only set_channel_level honours it.
        self.hang_forever = False

        self.get_state_snapshot_calls = 0
        self.refresh_level_table_calls = 0
        self.closed = False
        self.detached = False
        self.listener = None

    # -- lifecycle -----------------------------------------------------

    def attach_listener(self, listener: Any) -> None:
        self.listener = listener

    def detach_listener(self) -> None:
        self.detached = True
        self.listener = None

    async def close(self) -> None:
        self.closed = True

    # -- discovery -------------------------------------------------------

    async def discover_devices(
        self, session: Any, force_refresh: bool = False
    ) -> tuple[list[Any], list[Any]]:
        await _yield()
        if self.discover_error is not None:
            raise self.discover_error
        return self.devices

    async def get_info(self, session: Any, force_refresh: bool = False) -> BridgeInfo:
        await _yield()
        return self.info

    # -- state -----------------------------------------------------------

    async def get_state_snapshot(
        self, session: Any = None, *, refresh_level_table: bool = False
    ) -> BridgeStateSnapshot:
        await _yield()
        self.get_state_snapshot_calls += 1
        if refresh_level_table:
            await self.refresh_level_table()
        if self.snapshot_error is not None:
            raise self.snapshot_error
        return BridgeStateSnapshot.from_caches(self.scene_cache, self.level_cache)

    async def refresh_level_table(self) -> LevelCache:
        await _yield()
        self.refresh_level_table_calls += 1
        return self.level_cache

    # -- commands ----------------------------------------------------------

    async def set_room_scene(self, room_id: int, scene: int, *, verify: bool = True) -> Any:
        await _yield()
        self.commands.append(("scene", room_id, scene))
        self.command_verify.append(verify)
        if not verify:
            return None
        return self._resolve_echo(
            room_id,
            0,
            default=lambda: SceneStatusMessage(
                room=room_id,
                channel=0,
                scene=scene,
                command=CommandType.SET_SCENE,
                data=(1, scene),
                origin=MessageOrigin.CONTROL,
            ),
        )

    async def set_channel_level(
        self, room_id: int, channel_id: int, level: int, *, verify: bool = True
    ) -> Any:
        await _yield()
        if self.hang_forever:
            # Exercises the coordinator's overall command timeout: nothing
            # short of that timeout ever completes this await.
            await asyncio.Event().wait()
        self.commands.append(("level", room_id, channel_id, level))
        self.command_verify.append(verify)
        if not verify:
            return None
        return self._resolve_echo(
            room_id,
            channel_id,
            default=lambda: ChannelStatusMessage(
                room=room_id,
                channel=channel_id,
                brightness=level,
                command=CommandType.SET_LEVEL,
                data=(1, level),
                origin=MessageOrigin.CONTROL,
            ),
        )

    def _resolve_echo(self, room_id: int, channel_id: int, *, default: Any) -> Any:
        outcome = self.echoes.get((room_id, channel_id))
        if outcome is None:
            return default()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
