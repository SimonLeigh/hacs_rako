"""The Rako coordinator: one bridge, one listener, one snapshot.

The bridge is a push device -- it broadcasts a status message for every state
change it performs -- but the push path alone is not enough, for two reasons
found in Phase 0 (``docs/BRIDGE_BEHAVIOUR.md``):

* a listener that dies leaves Home Assistant showing stale state forever, and
* nothing is broadcast for changes made while Home Assistant is down.

So this coordinator runs both paths at once.  Broadcasts refine the snapshot as
they arrive; a periodic poll of the bridge's scene cache folds in anything the
push path missed, using the library's provenance rule so an approximate,
scene-derived level can never overwrite a level the bridge actually reported.

Commands are neither optimistic nor fire-and-forget: the library paces them,
waits for the bridge's own echo, retries once and raises if the change is never
confirmed.  State moves only when the bridge says it moved.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any

from python_rako import (
    Bridge,
    BridgeCommanderHTTP,
    BridgeStateSnapshot,
    RakoCommandError,
    StatusListener,
    UnknownStatusMessage,
)
from python_rako.const import RAKO_BRIDGE_DEFAULT_PORT

from homeassistant.const import CONF_HOST, CONF_MAC, CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_MIN_COMMAND_INTERVAL,
    CONF_POLL_INTERVAL,
    CONF_TRANSPORT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TRANSPORT,
    DOMAIN,
    EVENT_RAKO,
    TRANSPORT_HTTP,
)
from .errors import BRIDGE_ERRORS
from .events import build_event_data
from .helpers import apply_command_echo, has_scene_data

if TYPE_CHECKING:
    from datetime import datetime

    from python_rako import ListenerHealth, StatusMessage

    from homeassistant.core import CALLBACK_TYPE

    from .model import RakoConfigEntry

_LOGGER = logging.getLogger(__name__)

#: Broadcasts arriving before the first snapshot is built are held here rather
#: than dropped -- a level broadcast is unrecoverable once lost, because the
#: bridge cannot be asked what level a circuit is at.
PENDING_MESSAGE_LIMIT = 200

#: Overall cap on one command, covering pacing, the verify window and the
#: library's retry.  Deep enough for a queue of about twenty commands, so it
#: never fires in normal use; it exists so a wedged bridge cannot hold a
#: service call open indefinitely.
COMMAND_TIMEOUT = 30.0


class RakoCoordinator(DataUpdateCoordinator[BridgeStateSnapshot]):
    """Owns one bridge, its status listener and the state snapshot."""

    config_entry: RakoConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: RakoConfigEntry,
        bridge_device_id: str | None = None,
    ) -> None:
        """Build the bridge and listener for ``entry`` without starting them."""
        self.mac: str = entry.data[CONF_MAC]
        self.bridge_device_id = bridge_device_id

        host: str = entry.data[CONF_HOST]
        port: int = entry.data[CONF_PORT]
        options = entry.options

        commander = None
        if options.get(CONF_TRANSPORT, DEFAULT_TRANSPORT) == TRANSPORT_HTTP:
            commander = BridgeCommanderHTTP(host, port, async_get_clientsession(hass))

        bridge_kwargs: dict[str, Any] = {}
        if (min_interval := options.get(CONF_MIN_COMMAND_INTERVAL)) is not None:
            bridge_kwargs["min_command_interval"] = float(min_interval)

        self.bridge = Bridge(
            host,
            port,
            entry.data[CONF_NAME],
            self.mac,
            commander,
            **bridge_kwargs,
        )
        # The listener's port is the *local* port to bind, not the bridge's
        # command port: status broadcasts always arrive on 9761 whatever port
        # commands are sent to, so binding the configured port would leave a
        # non-default installation permanently deaf.
        self.listener = StatusListener(
            host,
            RAKO_BRIDGE_DEFAULT_PORT,
            on_health_change=self._handle_listener_health,
        )

        self._unsubscribe_listener: CALLBACK_TYPE | None = None
        self._unsubscribe_poll: CALLBACK_TYPE | None = None
        self._listener_running = False
        self._poll_failed = False
        self._closed = False
        self._warned_unverified = False
        self._level_table_task: asyncio.Task[None] | None = None
        self._pending_messages: deque[StatusMessage] = deque(
            maxlen=PENDING_MESSAGE_LIMIT
        )

        self._poll_interval = timedelta(
            seconds=int(options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {self.mac}",
            # Deliberately no update_interval: the base class reschedules its
            # timer on every async_set_updated_data, and this integration calls
            # that on every broadcast. A busy installation -- occupancy sensors
            # alone produce a few hundred a day -- would push the reconciliation
            # poll back for ever, which is exactly the poll it needs most. The
            # poll therefore runs on its own fixed timer, below.
            update_interval=None,
        )

    # -- lifecycle ---------------------------------------------------------

    async def async_setup(self) -> None:
        """Start listening, take the first snapshot, then start polling.

        The listener goes first so that the bridge can verify commands, and so
        that a broadcast arriving during start-up is buffered rather than lost.
        """
        self._unsubscribe_listener = self.listener.subscribe(
            self._handle_status_message
        )
        await self.listener.start()
        self.bridge.attach_listener(self.listener)
        await self.async_config_entry_first_refresh()
        self._async_apply_pending_messages()

        self._unsubscribe_poll = async_track_time_interval(
            self.hass,
            self._async_scheduled_poll,
            self._poll_interval,
            name=f"{DOMAIN} {self.mac} reconciliation poll",
        )

    async def async_shutdown(self) -> None:
        """Stop the listener and release the bridge's sockets.

        Registered automatically against the config entry by
        :class:`DataUpdateCoordinator`, and safe to call twice.
        """
        if self._closed:
            return
        # Set before stopping the listener: stopping it fires the health
        # callback, and a shutting-down coordinator must not report that as the
        # listener failing.
        self._closed = True
        await super().async_shutdown()
        if self._unsubscribe_poll is not None:
            self._unsubscribe_poll()
            self._unsubscribe_poll = None
        if self._unsubscribe_listener is not None:
            self._unsubscribe_listener()
            self._unsubscribe_listener = None
        self.bridge.detach_listener()
        await self.listener.stop()
        await self.bridge.close()

    # -- the poll path -----------------------------------------------------

    async def _async_scheduled_poll(self, now: datetime) -> None:
        """Run the reconciliation poll on its own fixed cadence."""
        await self.async_refresh()

    async def _async_update_data(self) -> BridgeStateSnapshot:
        """Reconcile the tracked snapshot with the bridge's own caches.

        Read over HTTP (``scenes.htm``) so the poll can never contend with the
        listener for the UDP socket.  The merge is the library's
        :meth:`~python_rako.BridgeStateSnapshot.reconcile`, which applies
        cache-derived levels only where the cached *scene* differs from the one
        being tracked -- never on top of a fresher level broadcast.
        """
        session = async_get_clientsession(self.hass)
        stale = self.data is not None and self.data.level_table_stale
        try:
            fresh = await self.bridge.get_state_snapshot(
                session, refresh_level_table=stale
            )
        except BRIDGE_ERRORS as err:
            return self._handle_poll_failure(err)

        if not has_scene_data(fresh):
            if self.data is None:
                # Nothing to lose yet. Coming up with everything unknown is the
                # honest starting point -- entities restore their last state --
                # and the next poll corrects it.
                _LOGGER.warning(
                    "The Rako bridge returned an empty scene cache; starting "
                    "with unknown state until a broadcast or the next poll"
                )
                self._poll_failed = False
                return fresh
            return self._handle_poll_failure(
                UpdateFailed("the bridge returned an empty scene cache")
            )

        self._poll_failed = False
        if self.data is None:
            return fresh
        return self.data.reconcile(fresh)

    def _handle_poll_failure(self, err: Exception) -> BridgeStateSnapshot:
        """Decide what a failed poll costs us.

        Keeping the tracked snapshot is only defensible while the push path is
        proven -- a listener that is bound but has never heard this bridge is
        not evidence of anything.
        """
        self._poll_failed = True
        if self.data is not None and self._push_path_proven:
            _LOGGER.warning(
                "Rako reconciliation poll failed (%s); keeping pushed state "
                "because the status listener is delivering",
                err,
            )
            return self.data
        raise UpdateFailed(f"Error reading state from the Rako bridge: {err}") from err

    # -- the push path -----------------------------------------------------

    @callback
    def _handle_status_message(self, message: StatusMessage) -> None:
        """Apply one broadcast to the snapshot and fire it on the event bus."""
        if isinstance(message, UnknownStatusMessage):
            _LOGGER.debug(
                "Unmodelled Rako instruction %s from room %s channel %s: %s "
                "(fired as an event anyway)",
                message.command_value,
                message.room,
                message.channel,
                message.data,
            )
        self._async_fire_event(message)

        if self.data is None:
            # Still building the first snapshot. A level broadcast cannot be
            # recovered later -- the bridge cannot be asked what level a
            # circuit is at -- so hold it rather than drop it.
            self._pending_messages.append(message)
            return

        self._async_apply(self.data.apply(message))

    @callback
    def _async_apply_pending_messages(self) -> None:
        """Fold broadcasts received during start-up into the first snapshot.

        Applied after the snapshot rather than before it: they arrived later
        than the cache read, so they are the better evidence.
        """
        pending = list(self._pending_messages)
        self._pending_messages.clear()
        if not pending or self.data is None:
            return
        _LOGGER.debug("Applying %d broadcast(s) received during start-up", len(pending))
        snapshot = self.data
        for message in pending:
            snapshot = snapshot.apply(message)
        self._async_apply(snapshot)

    @callback
    def _async_apply(self, snapshot: BridgeStateSnapshot) -> None:
        """Publish a new snapshot, refreshing the level table if it went stale."""
        if snapshot.level_table_stale:
            # A keypad rewrote a scene definition, so every scene-derived level
            # is suspect until the table is read again.
            self._async_schedule_level_table_refresh()
        self.async_set_updated_data(snapshot)

    @callback
    def _async_fire_event(self, message: StatusMessage) -> None:
        self.hass.bus.async_fire(
            EVENT_RAKO,
            build_event_data(
                message, bridge_mac=self.mac, device_id=self.bridge_device_id
            ),
        )

    @callback
    def _async_schedule_level_table_refresh(self) -> None:
        if self._level_table_task is not None and not self._level_table_task.done():
            return
        self._level_table_task = self.config_entry.async_create_background_task(
            self.hass,
            self._async_refresh_level_table(),
            name=f"{DOMAIN} {self.mac} level table refresh",
        )

    async def _async_refresh_level_table(self) -> None:
        """Re-read the scene->level table after a STORE broadcast."""
        try:
            level_table = await self.bridge.refresh_level_table()
        except BRIDGE_ERRORS as err:
            _LOGGER.warning("Could not refresh the Rako level table: %s", err)
            return
        if self.data is not None:
            self.async_set_updated_data(self.data.with_level_table(level_table))

    # -- availability ------------------------------------------------------

    @property
    def _push_path_proven(self) -> bool:
        """Whether the listener is up *and* has ever heard from this bridge.

        A bound socket proves nothing: a firewall, the wrong VLAN or a bridge
        that has stopped broadcasting all leave a listener that is running and
        permanently silent.  Only a message actually received shows the push
        path works, and that is the evidence used before trusting it -- for
        keeping state through a failed poll, and for verifying commands.
        """
        return self._listener_running and self.listener.health.messages_received > 0

    def _handle_listener_health(self, health: ListenerHealth) -> None:
        """React to the listener going down or coming back.

        Called by the library from the event loop.
        """
        if self._closed:
            # Shutting down: our own listener.stop() is not a fault to report.
            return

        was_running = self._listener_running
        self._listener_running = health.is_running

        if health.is_running and not was_running:
            if health.restart_count:
                _LOGGER.info(
                    "Rako status listener recovered after %s restart(s)",
                    health.restart_count,
                )
            if self.data is not None:
                # Anything could have changed while it was deaf.
                self.hass.async_create_task(self.async_request_refresh())
        elif not health.is_running and was_running:
            _LOGGER.warning(
                "Rako status listener stopped: %s", health.last_error or "unknown error"
            )

        self._async_update_availability()

    @callback
    def _async_update_availability(self) -> None:
        """Mark entities unavailable once *both* paths are down.

        A dead listener with a working poll still bounds staleness at one poll
        interval; a failing poll with a proven listener still sees every change
        the bridge makes.  Only losing both means Home Assistant no longer
        knows anything, and that is when it should say so rather than keep
        showing a confident, wrong state.

        Recovery is deliberately never asserted here.  A socket rebinding is
        not news about the lights, and flipping entities back to available on
        it would present stale state as current.  Availability returns when
        evidence does: a status message, or a poll that succeeds.
        """
        if (
            self._poll_failed
            and not self._push_path_proven
            and self.last_update_success
        ):
            self.async_set_update_error(
                UpdateFailed(
                    "The Rako status listener is not delivering and the "
                    "reconciliation poll is failing"
                )
            )

    # -- state seeded by entities -----------------------------------------

    @callback
    def async_restore_channel_level(
        self, room_id: int, channel_id: int, level: int
    ) -> None:
        """Seed a channel nobody has reported from a restored entity state.

        The bridge deletes a room from its scene cache the moment a fade button
        is used on it, so absence from the cache means "unknown", never "off".
        An entity that remembers what it was showing before the restart is
        better evidence than a zero, and is flagged as estimated.  A channel the
        bridge *has* reported is never overwritten.

        Deliberately does not notify listeners: this runs while entities are
        being added, and each one renders its own state on the way in.
        """
        if self.data is None:
            return
        if self.data.channel_level(room_id, channel_id) is not None:
            return
        self.data = self.data.with_restored(room_id, channel_id, level)

    # -- commands ----------------------------------------------------------

    async def async_set_room_scene(self, room_id: int, scene: int) -> None:
        """Put a room into a scene, and wait for the bridge to confirm it."""
        verify = self._push_path_proven
        await self._async_command(
            self.bridge.set_room_scene(room_id, scene, verify=verify), verified=verify
        )

    async def async_set_channel_level(
        self, room_id: int, channel_id: int, level: int
    ) -> None:
        """Drive one channel to a level, and wait for confirmation."""
        verify = self._push_path_proven
        await self._async_command(
            self.bridge.set_channel_level(room_id, channel_id, level, verify=verify),
            verified=verify,
        )

    async def _async_command(self, command: Any, *, verified: bool) -> None:
        """Await a bridge command and adopt its echo as the new state.

        The echo -- the bridge's own broadcast of what it did -- is the only
        evidence the change happened; the acknowledgement merely says the
        request was received (``BRIDGE_BEHAVIOUR.md`` fact 5).  The listener
        delivers the same broadcast a moment later, so applying it here is
        belt and braces rather than an optimistic guess.

        When the listener has never heard this bridge, verification is turned
        off instead of failing every command: a deaf listener is our problem,
        and refusing to switch on a light that physically switches on is worse
        than admitting we cannot confirm it.
        """
        if not verified and not self._warned_unverified:
            self._warned_unverified = True
            _LOGGER.warning(
                "Sending Rako commands unverified: the status listener has not "
                "received anything from the bridge, so commands cannot be "
                "confirmed and state will follow the reconciliation poll"
            )

        try:
            async with asyncio.timeout(COMMAND_TIMEOUT):
                echo = await command
        except TimeoutError as err:
            raise HomeAssistantError(
                f"The Rako bridge did not complete the command within "
                f"{COMMAND_TIMEOUT:.0f}s"
            ) from err
        except RakoCommandError as err:
            raise HomeAssistantError(
                f"The Rako bridge did not confirm the command: {err}"
            ) from err
        except BRIDGE_ERRORS as err:
            raise HomeAssistantError(
                f"Error sending a command to the Rako bridge: {err}"
            ) from err

        if echo is None or self.data is None:
            return
        self._async_apply(apply_command_echo(self.data, echo))
