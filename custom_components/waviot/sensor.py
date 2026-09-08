"""Sensor-сущности Waviot."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WaviotConfigEntry
from .const import (
    DOMAIN,
    HW_TYPE_AQUA,
    HW_TYPE_BETAR,
    HW_TYPE_GEFEST,
    HW_TYPE_LADOGA,
    HW_TYPE_PHOBOS,
    HW_TYPE_WALLHUB,
    HW_TYPE_WBOX,
    describe_evse_state,
)
from .coordinator import WaviotChargingCoordinator, WaviotMainCoordinator
from .device import WaviotDeviceInfo


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WaviotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    coordinator = runtime.main_coordinator
    entities: list[SensorEntity] = []
    for device in runtime.devices:
        if device.kind == HW_TYPE_AQUA:
            entities.append(WaviotWaterMeterSensor(coordinator, device, "Холодная вода"))
            entities.append(WaviotVoltageSensor(coordinator, device))
        elif device.kind == HW_TYPE_GEFEST:
            entities.append(WaviotWaterMeterSensor(coordinator, device, "Горячая вода"))
            entities.append(WaviotVoltageSensor(coordinator, device))
        elif device.kind == HW_TYPE_BETAR:
            entities.append(WaviotGasMeterSensor(coordinator, device))
            entities.append(WaviotVoltageSensor(coordinator, device))
        elif device.kind == HW_TYPE_LADOGA:
            entities.append(WaviotVoltageSensor(coordinator, device))
        elif device.kind == HW_TYPE_WALLHUB:
            entities.append(WaviotTemperatureSensor(coordinator, device))
            entities.append(WaviotHumiditySensor(coordinator, device))
        elif device.kind == HW_TYPE_PHOBOS:
            entities.append(WaviotEnergyMeterSensor(coordinator, device))
            entities.append(WaviotMainsVoltageSensor(coordinator, device))
            entities.append(WaviotPowerSensor(coordinator, device))
            entities.append(WaviotCurrentSensor(coordinator, device))
        elif device.kind == HW_TYPE_WBOX and runtime.charging_coordinator is not None:
            charging_coordinator = runtime.charging_coordinator
            entities.append(WaviotPowerSensor(charging_coordinator, device))
            entities.append(WaviotChargerEnergySensor(charging_coordinator, device))
            entities.append(WaviotMaxPowerLimitSensor(charging_coordinator, device))
            entities.append(WaviotTemperatureSensor(charging_coordinator, device))
            entities.append(WaviotEvseStatusSensor(charging_coordinator, device))
    async_add_entities(entities)


class _WaviotSensorBase(CoordinatorEntity, SensorEntity):
    """Общая база для sensor-сущностей.

    Координатор может быть WaviotMainCoordinator или WaviotChargingCoordinator (wbox) —
    обе формы данных одинаковы (dict[str, dict[str, Any]]), жёсткая типизация не нужна.
    """

    def __init__(
        self,
        coordinator: WaviotMainCoordinator | WaviotChargingCoordinator,
        device: WaviotDeviceInfo,
        key: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        self._key = key
        self._attr_unique_id = f"{device.modem_id}_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.modem_id)},
            name=f"Waviot {device.kind} {device.modem_id}",
            manufacturer="Waviot",
            model=device.kind,
        )

    @property
    def _status(self) -> dict | None:
        return self.coordinator.data.get(self._device.modem_id)

    @property
    def available(self) -> bool:
        return super().available and self._status is not None


class WaviotWaterMeterSensor(_WaviotSensorBase):
    """Счётчик воды aqua/gefest.

    aqua = холодная вода, gefest = горячая (
    aqua -> devices.types.smart_meter.cold_water, gefest -> ...hot_water). Официальное
    приложение Waviot Home само это не различает (gefest у них — отдельная категория
    "Heat"/теплосчётчик, не "горячая вода"), но пользователь решил взять готовую, разделение на холодную и горячую воду.
    """

    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS

    def __init__(self, coordinator: WaviotMainCoordinator, device: WaviotDeviceInfo, name: str) -> None:
        super().__init__(coordinator, device, "meter", name)

    @property
    def native_value(self) -> float | None:
        status = self._status
        return status["meter"] if status else None


class WaviotGasMeterSensor(_WaviotSensorBase):
    _attr_device_class = SensorDeviceClass.GAS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS

    def __init__(self, coordinator: WaviotMainCoordinator, device: WaviotDeviceInfo) -> None:
        super().__init__(coordinator, device, "meter", "Расход газа")

    @property
    def native_value(self) -> float | None:
        status = self._status
        return status["meter"] if status else None


class WaviotVoltageSensor(_WaviotSensorBase):
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WaviotMainCoordinator, device: WaviotDeviceInfo) -> None:
        super().__init__(coordinator, device, "voltage", "Напряжение батареи")

    @property
    def native_value(self) -> float | None:
        status = self._status
        return status["voltage"] if status else None


class WaviotTemperatureSensor(_WaviotSensorBase):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: WaviotMainCoordinator, device: WaviotDeviceInfo) -> None:
        super().__init__(coordinator, device, "temperature", "Температура")

    @property
    def native_value(self) -> float | None:
        status = self._status
        return status["temperature"] if status else None


class WaviotHumiditySensor(_WaviotSensorBase):
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: WaviotMainCoordinator, device: WaviotDeviceInfo) -> None:
        super().__init__(coordinator, device, "humidity", "Влажность")

    @property
    def native_value(self) -> float | None:
        status = self._status
        return status["humidity"] if status else None


class WaviotEnergyMeterSensor(_WaviotSensorBase):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "kWh"

    def __init__(self, coordinator: WaviotMainCoordinator, device: WaviotDeviceInfo) -> None:
        super().__init__(coordinator, device, "meter", "Расход энергии")

    @property
    def native_value(self) -> float | None:
        status = self._status
        return status["meter"] if status else None


class WaviotMainsVoltageSensor(_WaviotSensorBase):
    """Сетевое напряжение (не путать с WaviotVoltageSensor — там напряжение батареи)."""

    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT

    def __init__(self, coordinator: WaviotMainCoordinator, device: WaviotDeviceInfo) -> None:
        super().__init__(coordinator, device, "voltage", "Напряжение")

    @property
    def native_value(self) -> float | None:
        status = self._status
        return status["voltage"] if status else None


class WaviotPowerSensor(_WaviotSensorBase):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT

    def __init__(self, coordinator: WaviotMainCoordinator, device: WaviotDeviceInfo) -> None:
        super().__init__(coordinator, device, "power", "Мощность")

    @property
    def native_value(self) -> float | None:
        status = self._status
        return status["power"] if status else None


class WaviotCurrentSensor(_WaviotSensorBase):
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE

    def __init__(self, coordinator: WaviotMainCoordinator, device: WaviotDeviceInfo) -> None:
        super().__init__(coordinator, device, "amperage", "Ток")

    @property
    def native_value(self) -> float | None:
        status = self._status
        return status["amperage"] if status else None


class WaviotChargerEnergySensor(_WaviotSensorBase):
    """Энергия текущей сессии зарядки wbox — обнуляется на каждой новой зарядке.

    TOTAL_INCREASING специально рассчитан на счётчики с периодическим сбросом
    (HA сам корректно обработает обнуление в долгосрочной статистике) — см.
    поведение подтверждено на реальном устройстве.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "kWh"

    def __init__(self, coordinator: WaviotMainCoordinator, device: WaviotDeviceInfo) -> None:
        super().__init__(coordinator, device, "meter", "Энергия сессии")

    @property
    def native_value(self) -> float | None:
        status = self._status
        return status["meter"] if status else None


class WaviotMaxPowerLimitSensor(_WaviotSensorBase):
    """Потолок мощности от сети/железа (read-only) — не путать с number-сущностью
    max_power (реально настраиваемый пользователем лимит, см. number.py)."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WaviotMainCoordinator, device: WaviotDeviceInfo) -> None:
        super().__init__(coordinator, device, "max_power_limit", "Лимит мощности сети")

    @property
    def native_value(self) -> float | None:
        status = self._status
        return status["max_power_limit"] if status else None


class WaviotEvseStatusSensor(_WaviotSensorBase):
    """Текстовый статус EVSE (Свободна/Ожидает старта/Заряжается/ошибка) —
    помогает понять, почему switch.WaviotChargerSwitch может быть недоступен."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WaviotMainCoordinator, device: WaviotDeviceInfo) -> None:
        super().__init__(coordinator, device, "evse_status", "Статус зарядки")

    @property
    def native_value(self) -> str | None:
        status = self._status
        return describe_evse_state(status["state"]) if status else None
