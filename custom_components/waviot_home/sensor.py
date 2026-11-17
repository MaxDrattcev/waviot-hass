"""Sensor-сущности Waviot."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfReactivePower,
    UnitOfTemperature,
    UnitOfTime,
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
from .coordinator import (
    WaviotChargingCoordinator,
    WaviotMainCoordinator,
)
from .device import WaviotDeviceInfo, has_multiple_tariffs, is_three_phase


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
            # liter_forw приходит в м³ (так показывает и мобильное приложение), несмотря на имя
            entities.append(
                WaviotWaterMeterSensor(
                    coordinator, device, "Холодная вода", UnitOfVolume.CUBIC_METERS
                )
            )
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
            entities.extend(
                WaviotSpecSensor(coordinator, device, spec) for spec in WALLHUB_SENSORS
            )
        elif device.kind == HW_TYPE_WBOX and runtime.charging_coordinator is not None:
            charging_coordinator = runtime.charging_coordinator
            entities.append(WaviotPowerSensor(charging_coordinator, device))
            entities.append(WaviotChargerEnergySensor(charging_coordinator, device))
            entities.append(WaviotMaxPowerLimitSensor(charging_coordinator, device))
            entities.append(WaviotTemperatureSensor(charging_coordinator, device))
            entities.append(WaviotEvseStatusSensor(charging_coordinator, device))
            entities.extend(
                WaviotSpecSensor(charging_coordinator, device, spec) for spec in WBOX_SENSORS
            )
    async_add_entities(entities)
    _setup_phobos_sensors(entry, coordinator, async_add_entities)


def _setup_phobos_sensors(
    entry: WaviotConfigEntry,
    coordinator: WaviotMainCoordinator,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Постоянный набор сенсоров для каждого электросчётчика.

    Набор определяется только типом счётчика и не зависит от того, онлайн он сейчас
    или нет: офлайн-прибор показывает сущности недоступными, а не теряет их.
    """
    entities = [
        WaviotSpecSensor(coordinator, device, spec)
        for device in entry.runtime_data.devices
        if device.kind == HW_TYPE_PHOBOS
        for spec in phobos_specs_for(device)
    ]
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


class WaviotWaterMeterSensor(_WaviotSensorBase):
    """Счётчик воды aqua/gefest.

    aqua маркируется как холодная вода, gefest — как горячая. Официальное
    приложение так их не разделяет (gefest проходит у него отдельной категорией
    теплосчётчика), но для пользователя разделение на холодную и горячую
    понятнее, чем два одинаковых "расхода воды".
    """

    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        coordinator: WaviotMainCoordinator,
        device: WaviotDeviceInfo,
        name: str,
        unit: str = UnitOfVolume.LITERS,
    ) -> None:
        super().__init__(coordinator, device, "meter", name)
        self._attr_native_unit_of_measurement = unit

    @property
    def native_value(self) -> float | None:
        status = self._status
        return status.get("meter") if status else None


class WaviotGasMeterSensor(_WaviotSensorBase):
    _attr_device_class = SensorDeviceClass.GAS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS

    def __init__(self, coordinator: WaviotMainCoordinator, device: WaviotDeviceInfo) -> None:
        super().__init__(coordinator, device, "meter", "Расход газа")

    @property
    def native_value(self) -> float | None:
        status = self._status
        return status.get("meter") if status else None


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




@dataclass(frozen=True, kw_only=True)
class WaviotSensorSpec:
    """Описание сенсора счётчика или зарядки; key совпадает с ключом в статусе и в unique_id."""

    key: str
    name: str
    device_class: SensorDeviceClass | None = None
    unit: str | None = None
    state_class: SensorStateClass | None = None
    category: EntityCategory | None = None
    enabled_default: bool = True
    suggested_unit: str | None = None
    tariff: int | None = None
    timestamp: bool = False
    precision: int | None = None
    phase: str | None = None  # "A" | "B" | "C": у трёхфазных уходит в дочернее устройство


def _phase_specs(
    keys: tuple[str, ...],
    title: str,
    *,
    device_class: SensorDeviceClass,
    unit: str,
    enabled_default: bool = True,
) -> list[WaviotSensorSpec]:
    phases = ("A", "B", "C")
    return [
        WaviotSensorSpec(
            key=key,
            name=title,
            phase=phase,
            device_class=device_class,
            unit=unit,
            state_class=SensorStateClass.MEASUREMENT,
            enabled_default=enabled_default,
        )
        for key, phase in zip(keys, phases, strict=True)
    ]


_DIAGNOSTIC = EntityCategory.DIAGNOSTIC

PHOBOS_SENSORS: tuple[WaviotSensorSpec, ...] = (
    # Энергия A+: облако отдаёт Вт·ч; в интерфейсе по умолчанию кВт·ч, как в приложении.
    WaviotSensorSpec(
        key="meter",
        name="Расход энергии",
        device_class=SensorDeviceClass.ENERGY,
        unit=UnitOfEnergy.WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_unit=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    *(
        WaviotSensorSpec(
            key=f"energy_t{i}",
            name=f"Энергия A+ T{i}",
            device_class=SensorDeviceClass.ENERGY,
            unit=UnitOfEnergy.WATT_HOUR,
            state_class=SensorStateClass.TOTAL_INCREASING,
            suggested_unit=UnitOfEnergy.KILO_WATT_HOUR,
            tariff=i,
            enabled_default=(i == 1),  # T2–T4 выключены по умолчанию, включает пользователь
        )
        for i in range(1, 5)
    ),
    *_phase_specs(
        ("voltage", "voltage_b", "voltage_c"),
        "Напряжение",
        device_class=SensorDeviceClass.VOLTAGE,
        unit=UnitOfElectricPotential.VOLT,
    ),
    *_phase_specs(
        ("amperage", "amperage_b", "amperage_c"),
        "Ток",
        device_class=SensorDeviceClass.CURRENT,
        unit=UnitOfElectricCurrent.AMPERE,
    ),
    *_phase_specs(
        ("power", "power_b", "power_c"),
        "Мощность",
        device_class=SensorDeviceClass.POWER,
        unit=UnitOfPower.WATT,
    ),
    WaviotSensorSpec(
        key="power_sum",
        name="Мощность суммарная",
        device_class=SensorDeviceClass.POWER,
        unit=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        enabled_default=False,
    ),
    *_phase_specs(
        ("reactive_a", "reactive_b", "reactive_c"),
        "Реактивная мощность",
        device_class=SensorDeviceClass.REACTIVE_POWER,
        unit=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        enabled_default=False,
    ),
    WaviotSensorSpec(
        key="reactive_sum",
        name="Реактивная мощность суммарная",
        device_class=SensorDeviceClass.REACTIVE_POWER,
        unit=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        state_class=SensorStateClass.MEASUREMENT,
        enabled_default=False,
    ),
    *_phase_specs(
        ("apparent_a", "apparent_b", "apparent_c"),
        "Полная мощность",
        device_class=SensorDeviceClass.APPARENT_POWER,
        unit=UnitOfApparentPower.VOLT_AMPERE,
        enabled_default=False,
    ),
    WaviotSensorSpec(
        key="apparent_sum",
        name="Полная мощность суммарная",
        device_class=SensorDeviceClass.APPARENT_POWER,
        unit=UnitOfApparentPower.VOLT_AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        enabled_default=False,
    ),
    WaviotSensorSpec(
        key="frequency",
        name="Частота",
        device_class=SensorDeviceClass.FREQUENCY,
        unit=UnitOfFrequency.HERTZ,
        state_class=SensorStateClass.MEASUREMENT,
        enabled_default=False,
    ),
    WaviotSensorSpec(
        key="neutral_current",
        name="Ток нейтрали",
        device_class=SensorDeviceClass.CURRENT,
        unit=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        enabled_default=False,
    ),
    # Состояние прибора.
    WaviotSensorSpec(
        key="battery_voltage",
        name="Напряжение батареи",
        device_class=SensorDeviceClass.VOLTAGE,
        unit=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        category=_DIAGNOSTIC,
    ),
    WaviotSensorSpec(
        key="sw_version",
        name="Версия ПО",
        category=_DIAGNOSTIC,
        enabled_default=False,
    ),
)


def phobos_specs_for(device: WaviotDeviceInfo) -> list[WaviotSensorSpec]:
    """Постоянный набор сенсоров счётчика по его типу.

    T2–T4 — только у многотарифных счётчиков (правило мобильного приложения),
    фазы B и C — только у трёхфазных.
    """
    multi_tariff = has_multiple_tariffs(device.hw_type)
    three_phase = is_three_phase(device.hw_type)
    result = []
    for spec in PHOBOS_SENSORS:
        if spec.tariff is not None and spec.tariff > 1 and not multi_tariff:
            continue
        if spec.phase in ("B", "C") and not three_phase:
            continue
        result.append(spec)
    return result


def _shared_specs(*keys: str) -> tuple[WaviotSensorSpec, ...]:
    by_key = {spec.key: spec for spec in PHOBOS_SENSORS}
    return tuple(by_key[key] for key in keys)


# Дополнительные сенсоры зарядки wbox. Прежние (мощность, энергия сессии, лимит сети,
# температура, статус) остались отдельными классами ради неизменных unique_id.
WBOX_SENSORS: tuple[WaviotSensorSpec, ...] = (
    WaviotSensorSpec(
        key="duration",
        name="Длительность сессии",
        device_class=SensorDeviceClass.DURATION,
        unit=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_unit=UnitOfTime.MINUTES,
        precision=0,
        enabled_default=False,
    ),
    # Последняя завершённая сессия (история зарядок из облака)
    WaviotSensorSpec(
        key="last_session_energy",
        name="Энергия последней зарядки",
        device_class=SensorDeviceClass.ENERGY,
        unit=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    WaviotSensorSpec(
        key="last_session_duration",
        name="Длительность последней зарядки",
        device_class=SensorDeviceClass.DURATION,
        unit=UnitOfTime.SECONDS,
        suggested_unit=UnitOfTime.MINUTES,
        precision=0,
    ),
    WaviotSensorSpec(
        key="last_session_end",
        name="Окончание последней зарядки",
        device_class=SensorDeviceClass.TIMESTAMP,
        timestamp=True,
    ),
    # Настройки станции, у которых есть управляющая сущность (switch/number/time), здесь
    # не дублируются; остался только предел температуры корпуса — его менять не даём.
    # NFC
    WaviotSensorSpec(
        key="nfc_cards",
        name="Карт NFC",
        category=_DIAGNOSTIC,
        enabled_default=False,
    ),
    # Температуры
    WaviotSensorSpec(
        key="temp_input",
        name="Температура на входе",
        device_class=SensorDeviceClass.TEMPERATURE,
        unit=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        category=_DIAGNOSTIC,
        enabled_default=False,
    ),
    WaviotSensorSpec(
        key="temp_output",
        name="Температура на выходе",
        device_class=SensorDeviceClass.TEMPERATURE,
        unit=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        category=_DIAGNOSTIC,
        enabled_default=False,
    ),
    # Информация об устройстве — те же сенсоры, что у электросчётчиков
    *_shared_specs("battery_voltage", "sw_version"),
    WaviotSensorSpec(
        key="pair_device",
        name="Привязанный счётчик",
        category=_DIAGNOSTIC,
        enabled_default=False,
    ),
)


# СмартХаб: то, что показывает карточка «Информация» приложения.
WALLHUB_SENSORS: tuple[WaviotSensorSpec, ...] = _shared_specs("sw_version")


class WaviotSpecSensor(_WaviotSensorBase):
    """Сенсор, описанный WaviotSensorSpec (электросчётчики и зарядка)."""

    def __init__(
        self,
        coordinator: WaviotMainCoordinator | WaviotChargingCoordinator,
        device: WaviotDeviceInfo,
        spec: WaviotSensorSpec,
    ) -> None:
        super().__init__(coordinator, device, spec.key, spec.name)
        # Имя устройства HA добавляет сам, поэтому в имени сущности его нет.
        self._attr_has_entity_name = True
        enabled = spec.enabled_default
        if spec.phase is not None and is_three_phase(device.hw_type):
            self._attr_name = f"{spec.name}, фаза {spec.phase}"
            # Фазы B и C по умолчанию скрыты, чтобы не заполнять страницу счётчика.
            enabled = enabled and spec.phase == "A"
        self._timestamp = spec.timestamp
        self._attr_device_class = spec.device_class
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_state_class = spec.state_class
        self._attr_entity_category = spec.category
        self._attr_entity_registry_enabled_default = enabled
        if spec.suggested_unit is not None:
            self._attr_suggested_unit_of_measurement = spec.suggested_unit
        if spec.precision is not None:
            self._attr_suggested_display_precision = spec.precision

    @property
    def native_value(self) -> float | str | datetime | None:
        status = self._status
        value = status.get(self._key) if status else None
        if value is not None and self._timestamp:
            return datetime.fromtimestamp(value, tz=UTC)
        return value


class WaviotPowerSensor(_WaviotSensorBase):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT

    def __init__(self, coordinator: WaviotMainCoordinator, device: WaviotDeviceInfo) -> None:
        super().__init__(coordinator, device, "power", "Мощность")

    @property
    def native_value(self) -> float | None:
        status = self._status
        return status.get("power") if status else None



class WaviotChargerEnergySensor(_WaviotSensorBase):
    """Энергия текущей сессии зарядки wbox — обнуляется на каждой новой зарядке.

    TOTAL_INCREASING специально рассчитан на счётчики с периодическим сбросом —
    HA сам корректно обработает обнуление в долгосрочной статистике.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "kWh"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: WaviotMainCoordinator, device: WaviotDeviceInfo) -> None:
        super().__init__(coordinator, device, "meter", "Энергия сессии")

    @property
    def native_value(self) -> float | None:
        status = self._status
        return status.get("meter") if status else None


class WaviotMaxPowerLimitSensor(_WaviotSensorBase):
    """Допустимая мощность с учётом балансировки (read-only) — не путать с number-сущностью
    max_power (лимит, который задаёт пользователь, см. number.py)."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WaviotMainCoordinator, device: WaviotDeviceInfo) -> None:
        super().__init__(coordinator, device, "max_power_limit", "Допустимая мощность (балансировка)")

    @property
    def native_value(self) -> float | None:
        status = self._status
        return status.get("max_power_limit") if status else None


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
