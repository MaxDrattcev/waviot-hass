"""Binary sensor протечки Waviot (ladoga) — на отдельном быстром координаторе."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WaviotConfigEntry
from .const import DOMAIN, HW_TYPE_LADOGA
from .coordinator import WaviotLeakCoordinator
from .device import WaviotDeviceInfo


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WaviotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    leak_coordinator = entry.runtime_data.leak_coordinator
    if leak_coordinator is None:
        return

    entities = [
        WaviotLeakSensor(leak_coordinator, device)
        for device in entry.runtime_data.devices
        if device.kind == HW_TYPE_LADOGA
    ]
    async_add_entities(entities)


class WaviotLeakSensor(CoordinatorEntity[WaviotLeakCoordinator], BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.MOISTURE

    def __init__(self, coordinator: WaviotLeakCoordinator, device: WaviotDeviceInfo) -> None:
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"{device.modem_id}_leak"
        self._attr_name = "Протечка"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.modem_id)},
            name=f"Waviot {device.kind} {device.modem_id}",
            manufacturer="Waviot",
            model=device.kind,
        )

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.get(self._device.modem_id)
