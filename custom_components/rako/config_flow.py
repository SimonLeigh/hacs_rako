"""Config and options flows for Rako."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from python_rako import Bridge, RakoBridgeError, discover_bridge
from python_rako.const import RAKO_BRIDGE_DEFAULT_PORT
from python_rako.pacing import DEFAULT_MIN_COMMAND_INTERVAL
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_BASE, CONF_HOST, CONF_MAC, CONF_NAME, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_MIN_COMMAND_INTERVAL,
    CONF_POLL_INTERVAL,
    CONF_TRANSPORT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TRANSPORT,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_COMMAND_INTERVAL_CEILING,
    MIN_COMMAND_INTERVAL_FLOOR,
    MIN_POLL_INTERVAL,
    TRANSPORTS,
)

if TYPE_CHECKING:
    from python_rako import BridgeDescription, BridgeInfo

    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_POLL_INTERVAL): NumberSelector(
            NumberSelectorConfig(
                min=MIN_POLL_INTERVAL,
                max=MAX_POLL_INTERVAL,
                step=10,
                unit_of_measurement="s",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Required(CONF_TRANSPORT): SelectSelector(
            SelectSelectorConfig(
                options=TRANSPORTS,
                translation_key=CONF_TRANSPORT,
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Required(CONF_MIN_COMMAND_INTERVAL): NumberSelector(
            NumberSelectorConfig(
                min=MIN_COMMAND_INTERVAL_FLOOR,
                max=MIN_COMMAND_INTERVAL_CEILING,
                step=0.05,
                unit_of_measurement="s",
                mode=NumberSelectorMode.BOX,
            )
        ),
    }
)


class RakoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a Rako config flow."""

    VERSION = 1
    # Measured on a live WTC bridge: a healthy get_info (rako.xml fetch + parse)
    # can take >3s, and discovery replies are sub-second. Generous beats flaky.
    rako_timeout = 10.0
    discovery_timeout = 3.0

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> RakoOptionsFlow:
        """Return the options flow for this entry."""
        return RakoOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initiated by the user."""
        bridge_desc: BridgeDescription | dict[str, Any] = {}
        if user_input is None:
            try:
                bridge_desc = await asyncio.wait_for(
                    discover_bridge(), timeout=self.discovery_timeout
                )
            except (TimeoutError, RakoBridgeError, ValueError) as ex:
                _LOGGER.warning("Couldn't auto discover Rako bridge %s", ex)

            if bridge_desc:
                return self._show_setup_form(bridge_desc=bridge_desc)
            return self._show_setup_form(
                bridge_desc=bridge_desc, errors={CONF_BASE: "no_devices_found"}
            )

        bridge_desc = {
            "host": user_input[CONF_HOST],
            "port": user_input[CONF_PORT],
            "mac": user_input[CONF_MAC],
            "name": user_input.get(CONF_NAME) or user_input[CONF_MAC],
        }
        try:
            # just check we can connect using the given data
            await self._get_bridge_info(bridge_desc)
        except (RakoBridgeError, TimeoutError):
            return self._show_setup_form(
                bridge_desc=bridge_desc, errors={CONF_BASE: "cannot_connect"}
            )

        await self.async_set_unique_id(user_input[CONF_MAC], raise_on_progress=True)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=f"Rako Bridge ({bridge_desc['name']})",
            data=bridge_desc,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change where an already-configured bridge lives on the network.

        The MAC address stays the entry's unique id, so the entities, devices
        and automations built on it are untouched.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            bridge_desc = {
                "host": user_input[CONF_HOST],
                "port": user_input[CONF_PORT],
                "mac": entry.data[CONF_MAC],
                "name": entry.data[CONF_NAME],
            }
            try:
                await self._get_bridge_info(bridge_desc)
            except (RakoBridgeError, TimeoutError):
                errors[CONF_BASE] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_PORT: user_input[CONF_PORT],
                    },
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOST,
                        default=(user_input or entry.data).get(CONF_HOST),
                    ): str,
                    vol.Required(
                        CONF_PORT,
                        default=(user_input or entry.data).get(
                            CONF_PORT, RAKO_BRIDGE_DEFAULT_PORT
                        ),
                    ): int,
                }
            ),
            errors=errors,
        )

    def _show_setup_form(
        self,
        bridge_desc: BridgeDescription | dict[str, Any],
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Show the setup form to the user."""
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=bridge_desc.get("host")): str,
                    vol.Required(CONF_PORT, default=RAKO_BRIDGE_DEFAULT_PORT): int,
                    vol.Optional(CONF_NAME, default=bridge_desc.get("name")): str,
                    vol.Required(CONF_MAC, default=bridge_desc.get("mac")): str,
                }
            ),
            errors=errors or {},
        )

    async def _get_bridge_info(
        self, bridge_desc: BridgeDescription | dict[str, Any]
    ) -> BridgeInfo:
        session = async_get_clientsession(self.hass)
        bridge = Bridge(**bridge_desc)
        try:
            return await asyncio.wait_for(
                bridge.get_info(session), timeout=self.rako_timeout
            )
        finally:
            await bridge.close()


class RakoOptionsFlow(OptionsFlow):
    """Tune how the integration talks to the bridge."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_POLL_INTERVAL: int(user_input[CONF_POLL_INTERVAL]),
                    CONF_TRANSPORT: user_input[CONF_TRANSPORT],
                    CONF_MIN_COMMAND_INTERVAL: float(
                        user_input[CONF_MIN_COMMAND_INTERVAL]
                    ),
                }
            )

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA,
                {
                    CONF_POLL_INTERVAL: options.get(
                        CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
                    ),
                    CONF_TRANSPORT: options.get(CONF_TRANSPORT, DEFAULT_TRANSPORT),
                    CONF_MIN_COMMAND_INTERVAL: options.get(
                        CONF_MIN_COMMAND_INTERVAL, DEFAULT_MIN_COMMAND_INTERVAL
                    ),
                },
            ),
        )
