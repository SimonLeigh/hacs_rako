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

from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any

from python_rako import (
    Bridge,
    BridgeCommanderHTTP,
    BridgeStateSnapshot,
    RakoBridgeError,
    RakoCommandError,
    StateSource,
    StatusListener,
)
from python_rako.protocol import UnknownStatusMessage

from homeassistant.const import CONF_HOST, CONF_MAC, CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
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
from .events import build_event_data

if TYPE_CHECKING:
    import asyncio

    from python_rako import ListenerHealth, StatusMessage

    from .model import RakoConfigEntry

_LOGGER = logging.getLogger(__name__)

#: Errors from the bridge or the network that a poll is expected to survive.
_BRIDGE_ERRORS = (RakoBridgeError, OSError, TimeoutError)


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
        self.listener = StatusListener(
            host, port, on_health_change=self._handle_listener_health
        )

        self._unsubscribe_listener: Any = None
        self._listener_healthy = False
        self._poll_failed = False
        self._closed = False
        self._level_table_task: asyncio.Task[None] | None = None

        poll_interval = int(options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {self.mac}",
            update_interval=timedelta(seconds=poll_interval),
        )

    # -- lifecycle ---------------------------------------------------------

    async def async_setup(self) -> None:
        """Start listening, then take the first snapshot.

        The listener goes first so that the bridge can verify commands -- and
        so that a broadcast arriving during start-up is not missed.
        """
        self._unsubscribe_listener = self.listener.subscribe(
            self._handle_status_message
        )
        await self.listener.start()
        self.bridge.attach_listener(self.listener)
        await self.async_config_entry_first_refresh()

    async def async_shutdown(self) -> None:
        """Stop the listener and release the bridge's sockets.

        Registered automatically against the config entry by
        :class:`DataUpdateCoordinator`, and safe to call twice.
        """
        if self._closed:
            return
        self._closed = True
        await super().async_shutdown()
        if self._unsubscribe_listener is not None:
            self._unsubscribe_listener()
            self._unsubscribe_listener = None
        self.bridge.detach_listener()
        await self.listener.stop()
        await self.bridge.close()

    # -- the poll path -----------------------------------------------------

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
        except _BRIDGE_ERRORS as err:
            self._poll_failed = True
            if self.data is not None and self._listener_healthy:
                # The push path is still delivering, so state is not stale;
                # a failed poll on its own is no reason to go unavailable.
                _LOGGER.warning(
                    "Rako reconciliation poll failed (%s); keeping pushed state "
                    "because the status listener is healthy",
                    err,
                )
                return self.data
            raise UpdateFailed(
                f"Error reading state from the Rako bridge: {err}"
            ) from err

        self._poll_failed = False
        if self.data is None:
            return fresh
        return self.data.reconcile(fresh)

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
            # Still starting up; the first refresh will read the full state.
            return

        snapshot = self.data.apply(message)
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
        except _BRIDGE_ERRORS as err:
            _LOGGER.warning("Could not refresh the Rako level table: %s", err)
            return
        if self.data is not None:
            self.async_set_updated_data(self.data.with_level_table(level_table))

    # -- availability ------------------------------------------------------

    def _handle_listener_health(self, health: ListenerHealth) -> None:
        """React to the listener going down or coming back.

        Called by the library from the event loop.
        """
        was_healthy = self._listener_healthy
        self._listener_healthy = health.is_running

        if health.is_running and not was_healthy:
            if health.restart_count:
                _LOGGER.info(
                    "Rako status listener recovered after %s restart(s)",
                    health.restart_count,
                )
            if self.data is not None:
                # Anything could have changed while it was deaf.
                self.hass.async_create_task(self.async_request_refresh())
        elif not health.is_running and was_healthy:
            _LOGGER.warning(
                "Rako status listener stopped: %s", health.last_error or "unknown error"
            )

        self._async_update_availability()

    @callback
    def _async_update_availability(self) -> None:
        """Entities go unavailable only when *both* paths are down.

        A dead listener with a working poll still bounds staleness at one poll
        interval; a failing poll with a live listener still sees every change
        the bridge makes.  Only losing both means Home Assistant no longer
        knows anything, and that is when it should say so rather than keep
        showing a confident, wrong state.
        """
        degraded = not self._listener_healthy and self._poll_failed
        if degraded and self.last_update_success:
            self.async_set_update_error(
                UpdateFailed(
                    "The Rako status listener is down and the reconciliation "
                    "poll is failing"
                )
            )
        elif not degraded and not self.last_update_success and self.data is not None:
            self.async_set_updated_data(self.data)

    # -- state seeded by entities -----------------------------------------

    @callback
    def async_restore_channel_level(
        self, room_id: int, channel_id: int, level: int
    ) -> None:
        """Seed a channel nobody has reported from a restored entity state.

        The bridge deletes a room from its scene cache the moment a fade button
        is used on it, so absence from the cache means "unknown", never "off".
        An entity that remembers what it was showing before the restart is
        better evidence than a zero, and is flagged as estimated.

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
        await self._async_command(self.bridge.set_room_scene(room_id, scene))

    async def async_set_channel_level(
        self, room_id: int, channel_id: int, level: int
    ) -> None:
        """Drive one channel to a level, and wait for confirmation."""
        await self._async_command(
            self.bridge.set_channel_level(room_id, channel_id, level)
        )

    async def _async_command(self, command: Any) -> None:
        """Await a bridge command and adopt its echo as the new state.

        The echo -- the bridge's own broadcast of what it did -- is the only
        evidence the change happened; the acknowledgement merely says the
        request was received (``BRIDGE_BEHAVIOUR.md`` fact 5).  The listener
        delivers the same broadcast a moment later, so applying it here is
        belt and braces rather than an optimistic guess.
        """
        try:
            echo = await command
        except RakoCommandError as err:
            raise HomeAssistantError(
                f"The Rako bridge did not confirm the command: {err}"
            ) from err
        except _BRIDGE_ERRORS as err:
            raise HomeAssistantError(
                f"Error sending a command to the Rako bridge: {err}"
            ) from err

        if echo is None or self.data is None:
            return
        self.async_set_updated_data(
            self.data.apply(echo, source=StateSource.COMMAND_ECHO)
        )
