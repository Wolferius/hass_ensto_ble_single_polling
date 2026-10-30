"""The Ensto BLE integration."""
from __future__ import annotations

import asyncio
import logging
import time
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError, ServiceValidationError

from .const import (
    DOMAIN,
    UPDATE_TICK,
    CONF_POLLING_MODE,
    POLLING_MODE_ORIGINAL,
    DEFAULT_POLLING_MODE,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    CONF_ENERGY_PRICE,
    CONF_ENERGY_PRICE_ENTITY,
    ENERGY_PRICE_MIN,
    ENERGY_PRICE_MAX,
)

from .config_flow import CONF_CURRENCY, DEFAULT_CURRENCY
from .ensto_thermostat_manager import EnstoThermostatManager
from .storage_manager import EnstoStorageManager

_LOGGER = logging.getLogger(__name__)

# Type alias for config entry with runtime data
type EnstoConfigEntry = ConfigEntry[EnstoThermostatManager]

# List of supported platforms for this integration
PLATFORMS = [Platform.SENSOR, Platform.SWITCH, Platform.SELECT, Platform.NUMBER, Platform.DATETIME]

# Set services
SERVICE_GET_CALENDAR_DAY = "get_calendar_day"
SERVICE_SET_CALENDAR_DAY = "set_calendar_day"

GET_DAY_SCHEMA = vol.Schema({
    vol.Required("day"): vol.Range(min=1, max=7)
}, extra=vol.ALLOW_EXTRA)

SET_DAY_SCHEMA = vol.Schema({
    vol.Required("day"): vol.Range(min=1, max=7),
    vol.Required("programs"): vol.All(vol.Length(max=6), [
        vol.Schema({
            vol.Required("start_hour"): vol.Range(min=0, max=23),
            vol.Required("start_minute"): vol.Range(min=0, max=59),
            vol.Required("end_hour"): vol.Range(min=0, max=23),
            vol.Required("end_minute"): vol.Range(min=0, max=59),
            vol.Required("temp_offset"): vol.Range(min=-20, max=20),
            vol.Required("power_offset"): vol.Range(min=-100, max=100),
            vol.Required("enabled"): bool
        })
    ])
}, extra=vol.ALLOW_EXTRA)

async def async_setup_entry(hass: HomeAssistant, entry: EnstoConfigEntry) -> bool:
    """Set up Ensto BLE from a config entry."""

    # Migrate old entries without unique_id
    if entry.unique_id is None:
        hass.config_entries.async_update_entry(
            entry, unique_id=entry.data["mac_address"]
        )
        _LOGGER.debug("Migrated config entry %s to have unique_id", entry.data["mac_address"])

    try:
        # Initialize the thermostat manager
        manager = EnstoThermostatManager(hass, entry.data["mac_address"])
        manager.sequential = (
            entry.options.get(CONF_POLLING_MODE, DEFAULT_POLLING_MODE) != POLLING_MODE_ORIGINAL
        )

        # Setup scanner and verify connection
        manager.setup()
        async with manager.session():
            pass

        # Store the manager instance in runtime_data
        entry.runtime_data = manager

        # Read device information
        manager.sw_version = await manager.read_software_revision()
        manager.hw_version = await manager.read_hardware_revision()

        # Update config entry title with current info
        title = f"{manager.model_number or 'Unknown Model'} {manager.device_name or entry.data['mac_address']}"
        hass.config_entries.async_update_entry(entry, title=title)

        # Initialize device currency from config flow, keeping the price stored on the device
        try:
            config_currency = entry.data.get(CONF_CURRENCY, DEFAULT_CURRENCY)
            current = await manager.read_energy_unit()

            if current is None:
                _LOGGER.warning("Could not read energy unit, device currency not initialized")
            elif current['currency_code'] != config_currency:
                success = await manager.write_energy_unit(config_currency, current['price'])
                if not success:
                    _LOGGER.warning("Failed to set device currency")

        except Exception as e:
            _LOGGER.warning("Failed to initialize device currency: %s", e)

        # Apply the energy price from the options, if one is configured
        manager.energy_price_managed = bool(
            entry.options.get(CONF_ENERGY_PRICE_ENTITY) or entry.options.get(CONF_ENERGY_PRICE) is not None
        )
        await _apply_energy_price(hass, entry, manager)

        async def get_calendar_day(call: ServiceCall) -> None:
                    """Get calendar day programs service."""
                    # Get device_id from target
                    device_ids = call.data.get("target", [])
                    if not device_ids:
                        raise ServiceValidationError("No target device specified")

                    if not isinstance(device_ids, list):
                        device_ids = [device_ids]

                    day = call.data["day"]
                    device_registry = dr.async_get(hass)

                    for device_id in device_ids:
                        # Find config entry for this device
                        device_entry = device_registry.async_get(device_id)

                        if not device_entry:
                            raise ServiceValidationError(f"Device {device_id} not found")

                        # Get config entry from device (config_entry_id exists from Home Assistant 2026.8)
                        config_entry_id = getattr(device_entry, "config_entry_id", None) or next(iter(device_entry.config_entries))
                        config_entry = hass.config_entries.async_get_entry(config_entry_id)
                        if config_entry is None or config_entry.state is not ConfigEntryState.LOADED:
                            raise ServiceValidationError(f"Device {device_id} is disabled or not available")
                        manager = config_entry.runtime_data

                        # Read calendar day
                        result = await manager.read_calendar_day(day)

                        if result:
                            enabled_programs = [p for p in result['programs'] if p['enabled']]
                            programs_str = ", ".join([f"{p['start_hour']:02d}:{p['start_minute']:02d}-{p['end_hour']:02d}:{p['end_minute']:02d} {p['temp_offset']:+.1f}°C" for p in enabled_programs])
                            _LOGGER.info("Action [Get Calendar Day %d] for [%s] (%s): %d programs [%s]",
                                    day, manager.device_name or "Unknown", manager.mac_address, len(enabled_programs), programs_str)
                        else:
                            raise HomeAssistantError(f"Could not read calendar day from {manager.device_name or manager.mac_address}")

        async def set_calendar_day(call: ServiceCall) -> None:
                    """Set calendar day programs service."""
                    # Get device_id from target
                    device_ids = call.data.get("target", [])
                    if not device_ids:
                        raise ServiceValidationError("No target device specified")

                    if not isinstance(device_ids, list):
                        device_ids = [device_ids]

                    day = call.data["day"]
                    programs = call.data["programs"]
                    device_registry = dr.async_get(hass)

                    for device_id in device_ids:
                        # Find config entry for this device
                        device_entry = device_registry.async_get(device_id)

                        if not device_entry:
                            raise ServiceValidationError(f"Device {device_id} not found")

                        # Get config entry from device (config_entry_id exists from Home Assistant 2026.8)
                        config_entry_id = getattr(device_entry, "config_entry_id", None) or next(iter(device_entry.config_entries))
                        config_entry = hass.config_entries.async_get_entry(config_entry_id)
                        if config_entry is None or config_entry.state is not ConfigEntryState.LOADED:
                            raise ServiceValidationError(f"Device {device_id} is disabled or not available")
                        manager = config_entry.runtime_data

                        # Write calendar day
                        success = await manager.write_calendar_day(day, programs)

                        if success:
                            enabled_programs = [p for p in programs if p['enabled']]
                            programs_str = ", ".join([f"{p['start_hour']:02d}:{p['start_minute']:02d}-{p['end_hour']:02d}:{p['end_minute']:02d} {p['temp_offset']:+.1f}°C" for p in enabled_programs])
                            _LOGGER.info("Action [Set Calendar Day %d] for [%s] (%s): %d programs saved [%s]",
                                    day, manager.device_name or "Unknown", manager.mac_address, len(enabled_programs), programs_str)
                        else:
                            raise HomeAssistantError(f"Could not write calendar day to {manager.device_name or manager.mac_address}")

        # Only register services if they don't already exist
        if not hass.services.has_service(DOMAIN, SERVICE_GET_CALENDAR_DAY):
            hass.services.async_register(
                DOMAIN,
                SERVICE_GET_CALENDAR_DAY,
                get_calendar_day,
                schema=GET_DAY_SCHEMA,
            )

        if not hass.services.has_service(DOMAIN, SERVICE_SET_CALENDAR_DAY):
            hass.services.async_register(
                DOMAIN,
                SERVICE_SET_CALENDAR_DAY,
                set_calendar_day,
                schema=SET_DAY_SCHEMA,
            )

        # Set up the platforms
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        # Disconnect now that the initial values are read; the update loop reconnects.
        # In original mode release() does nothing and the connection stays open.
        await manager.release()
        manager.next_refresh = time.monotonic() + _update_interval_seconds(entry)
        _start_update_loop(hass)

        # Reload when the options change, so the new interval and price apply at once
        manager.options_snapshot = dict(entry.options)
        entry.async_on_unload(entry.add_update_listener(_async_options_updated))

        return True
        
    except Exception as ex:
        _LOGGER.error("Error setting up Ensto BLE: %s", str(ex))
        raise ConfigEntryNotReady from ex

async def async_unload_entry(hass: HomeAssistant, entry: EnstoConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    if unload_ok:
        manager = entry.runtime_data
        await manager.cleanup()
    
    # Only remove services if this is the last config entry for the domain
    remaining_entries = [
        e for e in hass.config_entries.async_entries(DOMAIN) 
        if e.entry_id != entry.entry_id
    ]
    if not remaining_entries:
        hass.services.async_remove(DOMAIN, SERVICE_GET_CALENDAR_DAY)
        hass.services.async_remove(DOMAIN, SERVICE_SET_CALENDAR_DAY)
        cancel_update_loop = hass.data.get(DOMAIN, {}).pop("cancel_update_loop", None)
        if cancel_update_loop:
            cancel_update_loop()

    return unload_ok

async def _async_options_updated(hass: HomeAssistant, entry: EnstoConfigEntry) -> None:
    """Reload the entry after its options were changed."""
    # Update listeners also fire on title changes; only reload for new options
    if entry.options == entry.runtime_data.options_snapshot:
        return
    await hass.config_entries.async_reload(entry.entry_id)

def _update_interval_seconds(entry: EnstoConfigEntry) -> float:
    return float(entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)) * 60

def _configured_energy_price(hass: HomeAssistant, entry: EnstoConfigEntry) -> float | None:
    """Return the price from the linked entity or the fixed option, or None if neither is set."""
    entity_id = entry.options.get(CONF_ENERGY_PRICE_ENTITY)
    if entity_id:
        state = hass.states.get(entity_id)
        try:
            price = float(state.state)
        except (AttributeError, TypeError, ValueError):
            _LOGGER.warning("Energy price entity %s has no numeric state (%s), using the fixed price",
                            entity_id, state.state if state else "missing")
        else:
            # The device stores 0..655.35; negative spot prices are stored as 0
            return min(max(price, ENERGY_PRICE_MIN), ENERGY_PRICE_MAX)

    return entry.options.get(CONF_ENERGY_PRICE)

async def _apply_energy_price(hass: HomeAssistant, entry: EnstoConfigEntry, manager: EnstoThermostatManager) -> None:
    """Write the configured energy price to the device when it differs from the stored one."""
    price = _configured_energy_price(hass, entry)
    if price is None:
        return

    current = await manager.read_energy_unit()
    if current is None:
        _LOGGER.warning("Could not read energy unit from %s, price not updated", manager.mac_address)
        return
    # The device stores the price with two decimals
    if round(current['price'], 2) == round(price, 2):
        return

    if await manager.write_energy_unit(current['currency_code'], price):
        _LOGGER.info("Energy unit price for %s (%s) set to %.2f",
                     manager.device_name or "Unknown", manager.mac_address, price)

def _start_update_loop(hass: HomeAssistant) -> None:
    """Refresh each thermostat when its polling interval is due, one device at a time."""
    data = hass.data.setdefault(DOMAIN, {})
    if "cancel_update_loop" in data:
        return

    running = asyncio.Lock()

    async def _refresh_due(now=None) -> None:
        if running.locked():
            return
        async with running:
            for entry in hass.config_entries.async_entries(DOMAIN):
                # Re-check each time; an entry may be unloaded during the round
                if entry.state is not ConfigEntryState.LOADED:
                    continue
                manager = entry.runtime_data
                if time.monotonic() < manager.next_refresh:
                    continue
                _LOGGER.debug("Refreshing %s (%s)", manager.device_name, manager.mac_address)
                try:
                    # The price write and the entity refresh share one connection
                    await _apply_energy_price(hass, entry, manager)
                    # Original-mode entities poll themselves; only the price is handled here
                    if manager.sequential:
                        await manager.refresh_entities()
                except Exception as e:
                    _LOGGER.warning("Could not refresh %s (%s): %s",
                                    manager.device_name or "Unknown", manager.mac_address, e)
                manager.next_refresh = time.monotonic() + _update_interval_seconds(entry)

    data["cancel_update_loop"] = async_track_time_interval(
        hass, _refresh_due, UPDATE_TICK, cancel_on_shutdown=True
    )

async def async_remove_entry(hass: HomeAssistant, entry: EnstoConfigEntry) -> None:
    """Remove a config entry.
    
    Called when user deletes the integration completely, not on disable.
    Removes stored factory reset ID for the device.
    """
    storage_manager = EnstoStorageManager(hass)
    await storage_manager.async_remove_device_data(entry.data["mac_address"])