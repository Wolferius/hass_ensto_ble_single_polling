"""Base entity for Ensto BLE integration."""
from homeassistant.helpers.entity import Entity
from .const import DOMAIN

class EnstoBaseEntity(Entity):
    """Base entity class for Ensto BLE."""

    # Entities are refreshed by the integration's sequential update loop, not polled
    _attr_should_poll = False

    def __init__(self, manager):
        """Initialize the base entity."""
        self._manager = manager

    async def async_internal_added_to_hass(self) -> None:
        """Register with the manager so the update loop refreshes this entity."""
        await super().async_internal_added_to_hass()
        self._manager.entities.append(self)

    async def async_internal_will_remove_from_hass(self) -> None:
        """Unregister from the manager."""
        if self in self._manager.entities:
            self._manager.entities.remove(self)
        await super().async_internal_will_remove_from_hass()

    @property
    def device_info(self):
        """Return device info for Home Assistant device registry.

        Returns a dictionary containing:
        - unique_id: MAC address as the device identifier
        - name: device name or default name with MAC
        - manufacturer: fixed value "Ensto"
        - model: model number if available
        - sw_version: software version if available
        - hw_version: hardware version if available
        """
        name = self._manager.device_name or f"Ensto Thermostat {self._manager.mac_address}"
        
        # Basic info that's always included
        info = {
            "identifiers": {(DOMAIN, self._manager.mac_address)},
            "name": name,
            "manufacturer": "Ensto",
            "model": self._manager.model_number or "model name not available",
        }
        
        # Add version info if available
        if hasattr(self._manager, "sw_version"):
            info["sw_version"] = self._manager.sw_version
        if hasattr(self._manager, "hw_version"):
            info["hw_version"] = self._manager.hw_version
            
        return info
