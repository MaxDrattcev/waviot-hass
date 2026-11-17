"""Binary sensor протечки Waviot (ladoga) — на отдельном быстром координаторе."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WaviotConfigEntry
from .const import DOMAIN, HW_TYPE_LADOGA, HW_TYPE_WBOX
from .coordinator import WaviotChargingCoordinator, WaviotLeakCoordinator
from .device import WaviotDeviceInfo


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WaviotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    entities: list[BinarySensorEntity] = []

    if runtime.leak_coordinator is not None:
        entities.extend(
            WaviotLeakSensor(runtime.leak_coordinator, device)
            for device in runtime.devices
            if device.kind == HW_TYPE_LADOGA
        )

    if runtime.charging_coordinator is not None:
        entities.extend(
            WaviotChargerBinarySensor(runtime.charging_coordinator, device, spec)
            for device in runtime.devices
            if device.kind == HW_TYPE_WBOX
            for spec in WBOX_BINARY_SENSORS
        )

    async_add_entities(entities)


@dataclass(frozen=True, kw_only=True)
class WaviotBinarySpec:
    """Описание бинарного сенсора зарядки; key совпадает с ключом в статусе и в unique_id."""

    key: str
    name: str
    device_class: BinarySensorDeviceClass | None = None
    category: EntityCategory | None = None
    enabled_default: bool = True


# Настройки станции (балансировка, ограничения, тарифы, расписание, NFC) здесь не
# дублируются: для них есть управляющие переключатели. Остались только состояния, которые
# не являются настройкой.
WBOX_BINARY_SENSORS: tuple[WaviotBinarySpec, ...] = (
    WaviotBinarySpec(
        key="cable_connected",
        name="Кабель подключён",
        device_class=BinarySensorDeviceClass.PLUG,
    ),
    WaviotBinarySpec(
        key="problem",
        name="Неисправность",
        device_class=BinarySensorDeviceClass.PROBLEM,
        enabled_default=False,
    ),
)


class WaviotChargerBinarySensor(CoordinatorEntity[WaviotChargingCoordinator], BinarySensorEntity):
    """Бинарный сенсор зарядки wbox, описанный WaviotBinarySpec."""

    def __init__(
        self,
        coordinator: WaviotChargingCoordinator,
        device: WaviotDeviceInfo,
        spec: WaviotBinarySpec,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        self._key = spec.key
        self._attr_unique_id = f"{device.modem_id}_{spec.key}"
        self._attr_has_entity_name = True
        self._attr_name = spec.name
        self._attr_device_class = spec.device_class
        self._attr_entity_category = spec.category
        self._attr_entity_registry_enabled_default = spec.enabled_default
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.modem_id)},
            name=device.title,
            manufacturer="Waviot",
            model=device.kind,
        )

    @property
    def _status(self) -> dict | None:
        return self.coordinator.data.get(self._device.modem_id)

    @property
    def available(self) -> bool:
        return super().available and self._status is not None

    @property
    def is_on(self) -> bool | None:
        status = self._status
        return status.get(self._key) if status else None


class WaviotLeakSensor(CoordinatorEntity[WaviotLeakCoordinator], BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.MOISTURE

    def __init__(self, coordinator: WaviotLeakCoordinator, device: WaviotDeviceInfo) -> None:
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"{device.modem_id}_leak"
        self._attr_name = "Протечка"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.modem_id)},
            name=device.title,
            manufacturer="Waviot",
            model=device.kind,
        )

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.get(self._device.modem_id)
