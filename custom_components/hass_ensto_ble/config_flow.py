"""Config flow for Ensto BLE integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    DOMAIN,
    CURRENCY_MAP,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    CONF_ENERGY_PRICE,
    CONF_ENERGY_PRICE_ENTITY,
    ENERGY_PRICE_MIN,
    ENERGY_PRICE_MAX,
)
from .ensto_thermostat_manager import EnstoThermostatManager

_LOGGER = logging.getLogger(__name__)

# CONF_CURRENCY = "currency"
CONF_CURRENCY = "Please select a currency for energy cost calculations"
DEFAULT_CURRENCY = 1  # EUR

class EnstoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Ensto BLE."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_devices = {}
        self._mac_address = None
        self._manager = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> EnstoOptionsFlow:
        """Return the options flow behind the Configure button."""
        return EnstoOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step.
        
        If user_input is None, scans for devices in pairing mode and shows device selection form.
        If user_input contains MAC address, attempts to connect and verify the device.
        """
        if user_input is not None:
            self._mac_address = user_input["Please select an Ensto BLE device in pairing mode"]

            # Set unique_id and abort if already configured
            await self.async_set_unique_id(self._mac_address)
            self._abort_if_unique_id_configured()

            # Move to currency selection step
            return await self.async_step_currency()

        # Initialize manager for device scanning
        manager = EnstoThermostatManager(self.hass, "")
        manager.setup()
        
        # Scan for devices that are in pairing mode
        pairing_devices = manager.find_devices_in_pairing_mode()

        if not pairing_devices:
            return self.async_abort(
                reason="No Ensto BLE devices in pairing mode. Hold BLE reset button for >0.5 seconds. Blue LED will blink."
            )

        self._discovered_devices = {}
        for addr, (device, _) in pairing_devices.items():
            # Get RSSI value for the device
            rssi = device.rssi if hasattr(device, 'rssi') else None
            
            # Include RSSI in the device name if available
            if rssi is not None:
                self._discovered_devices[addr] = f"{device.name} ({addr}) [{rssi} dBm]"
            else:
                self._discovered_devices[addr] = f"{device.name} ({addr})"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("Please select an Ensto BLE device in pairing mode"): vol.In(self._discovered_devices)
                }
            )
        )

    async def async_step_currency(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle currency selection."""
        if user_input is not None:
            # Initialize manager and get device info
            self._manager = EnstoThermostatManager(self.hass, self._mac_address)
            self._manager.setup()
            
            # Try to connect and authenticate the device
            try:
                async with self._manager.session():
                    pass
                await self._manager.release()
            except Exception as e:
                _LOGGER.error("Action connect for [%s]: %s", self._mac_address, e)
                return self.async_abort(
                    reason="Connection and authentication with the device failed. Please try again."
                )
            
            # Create config entry with device info and currency
            model = self._manager.model_number or "Unknown Model"
            name = self._manager.device_name or self._mac_address
            title = f"{model} {name}"
            
            return self.async_create_entry(
                title=title,
                data={
                    "mac_address": self._mac_address,
                    CONF_CURRENCY: user_input[CONF_CURRENCY],
                }
            )

        # Show currency selection form
        currency_options = {code: name for code, name in CURRENCY_MAP.items()}

        return self.async_show_form(
            step_id="currency",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CURRENCY, default=DEFAULT_CURRENCY): vol.In(currency_options)
                }
            )
        )


class EnstoOptionsFlow(config_entries.OptionsFlow):
    """Polling interval and energy unit price for one thermostat."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show and save the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        schema = vol.Schema(
            {
                vol.Required(CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL): NumberSelector(
                    NumberSelectorConfig(
                        min=1, max=60, step=1, unit_of_measurement="min", mode=NumberSelectorMode.BOX
                    )
                ),
                # Leave both price fields empty to manage the price with the Energy unit price entity
                vol.Optional(CONF_ENERGY_PRICE): NumberSelector(
                    NumberSelectorConfig(
                        min=ENERGY_PRICE_MIN, max=ENERGY_PRICE_MAX, step=0.01, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(CONF_ENERGY_PRICE_ENTITY): EntitySelector(
                    EntitySelectorConfig(domain=["sensor", "input_number", "number"])
                ),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, self.config_entry.options),
        )
