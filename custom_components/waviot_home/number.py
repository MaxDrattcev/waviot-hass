"""Number-сущность лимита мощности зарядки wbox."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WaviotConfigEntry
from .account import WaviotAccount
from .api import CHARGER_SETTINGS_CMD, WaviotApiError, WaviotClient
from .const import (
    CHARGER_FALLBACK_MAX_POWER_KW,
    CHARGER_MIN_POWER_KW,
    DOMAIN,
    HW_TYPE_PHOBOS,
    HW_TYPE_WBOX,
)
from .coordinator import WaviotChargingCoordinator, WaviotMainCoordinator
from .device import WaviotDeviceInfo
from .pending import PendingValue


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WaviotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    entities: list[NumberEntity] = []
    for device in runtime.devices:
        if device.kind == HW_TYPE_WBOX and runtime.charging_coordinator is not None:
            coordinator = runtime.charging_coordinator
            entities.append(
                WaviotChargerMaxPowerNumber(coordinator, runtime.client, runtime.account, device)
            )
            entities.extend(
                WaviotChargerConfigNumber(coordinator, runtime.client, runtime.account, device, spec)
                for spec in WBOX_CONFIG_NUMBERS
            )
        elif device.kind == HW_TYPE_PHOBOS:
            coordinator = runtime.main_coordinator
            entities.extend(
                WaviotPhobosNumber(coordinator, runtime.client, runtime.account, device, spec)
                for spec in PHOBOS_NUMBERS
            )
            entities.append(
                WaviotPowersharingNumber(coordinator, runtime.client, runtime.account, device)
            )
    async_add_entities(entities)


class WaviotChargerMaxPowerNumber(CoordinatorEntity[WaviotChargingCoordinator], NumberEntity):
    """Лимит мощности зарядки wbox (`max_power`).

    Не путать с `max_power_limit` (отдельный read-only сенсор — допустимая мощность
    с учётом балансировки). Верхняя граница слайдера — `max_balancing_power` из настроек
    станции (как в мобильном приложении), а при его отсутствии — запасная константа.
    `max_power_limit` для границы не используется: проверка на живом устройстве показала,
    что это поле "плавает" само по себе, из-за чего диапазон слайдера сужался вместо того,
    чтобы просто двигать позицию.
    """

    _attr_native_min_value = CHARGER_MIN_POWER_KW
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: WaviotChargingCoordinator,
        client: WaviotClient,
        account: WaviotAccount,
        device: WaviotDeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._account = account
        self._device = device
        self._attr_unique_id = f"{device.modem_id}_max_power"
        self._attr_name = "Лимит мощности зарядки"
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
    def native_max_value(self) -> float:
        status = self._status
        limit = status.get("max_balancing_power") if status else None
        if limit is None or limit < CHARGER_MIN_POWER_KW:
            return CHARGER_FALLBACK_MAX_POWER_KW
        return limit

    @property
    def native_value(self) -> float | None:
        status = self._status
        return status.get("max_power") if status else None

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self._account.async_call(
                lambda jwt: self._client.charger_set_max_power(
                    self._device.modem_id, jwt, value
                )
            )
        except WaviotApiError as err:
            await self.coordinator.async_request_refresh()
            raise HomeAssistantError(f"Не удалось установить лимит мощности: {err}") from err
        # При успехе не форсируем немедленный refresh — устройство отчитается на
        # следующем плановом опросе, см. объяснение в switch.py.


@dataclass(frozen=True, kw_only=True)
class ChargerNumberSpec:
    """Числовая настройка станции wbox (объект CHARGER_CMD_CHARGE_SETTINGS).

    Только настройки, которые есть в мобильном приложении. Границы — по смыслу поля
    (час суток): в самом приложении диапазонов нет.
    """

    key: str
    name: str
    field: str
    minimum: float
    maximum: float
    step: float
    unit: str


# Имена подобраны под алфавитную сортировку HA: «Тарифы: два тарифа» (переключатель)
# идёт перед «Тарифы: начало …».
WBOX_CONFIG_NUMBERS: tuple[ChargerNumberSpec, ...] = (
    ChargerNumberSpec(
        key="hour_start_day_tariff", name="Тарифы: начало дневного", field="hour_start_day_tariff",
        minimum=0, maximum=23, step=1, unit=UnitOfTime.HOURS,
    ),
    ChargerNumberSpec(
        key="hour_start_night_tariff", name="Тарифы: начало ночного",
        field="hour_start_night_tariff", minimum=0, maximum=23, step=1, unit=UnitOfTime.HOURS,
    ),
)


class WaviotChargerConfigNumber(CoordinatorEntity[WaviotChargingCoordinator], NumberEntity):
    """Числовая настройка станции; запись «свежий объект, одно поле, весь объект»."""

    _attr_mode = NumberMode.BOX
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: WaviotChargingCoordinator,
        client: WaviotClient,
        account: WaviotAccount,
        device: WaviotDeviceInfo,
        spec: ChargerNumberSpec,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._account = account
        self._device = device
        self._spec = spec
        self._pending = PendingValue()
        self._attr_unique_id = f"{device.modem_id}_{spec.key}"
        self._attr_name = spec.name
        self._attr_native_min_value = spec.minimum
        self._attr_native_max_value = spec.maximum
        self._attr_native_step = spec.step
        self._attr_native_unit_of_measurement = spec.unit
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
    def native_value(self) -> float | None:
        status = self._status
        return self._pending.resolve(status.get(self._spec.key) if status else None)

    async def async_set_native_value(self, value: float) -> None:
        spec = self._spec
        baseline = self._status.get(spec.key) if self._status else None
        wire_value = round(value)
        try:
            await self._account.async_call(
                lambda jwt: self._client.charger_update_object(
                    self._device.modem_id, jwt, CHARGER_SETTINGS_CMD, {spec.field: wire_value}
                )
            )
        except WaviotApiError as err:
            raise HomeAssistantError(f"Не удалось изменить «{spec.name}»: {err}") from err
        self._pending.set(value, baseline)
        self.async_write_ha_state()


@dataclass(frozen=True, kw_only=True)
class PhobosNumberSpec:
    """Числовая настройка счётчика Фобос; obis — команда приложения для записи."""

    key: str
    name: str
    obis: str
    minimum: float
    maximum: float
    unit: str
    device_class: NumberDeviceClass | None = None
    # Допустимые значения приложения: 0 (выключено) либо от minimum_active
    minimum_active: float | None = None
    enabled_default: bool = False


PHOBOS_NUMBERS: tuple[PhobosNumberSpec, ...] = (
    PhobosNumberSpec(
        key="instant_period", name="Мгновенные данные: период отправки",
        obis="1.0.0.8.9.255_2", minimum=0, maximum=3600, unit=UnitOfTime.SECONDS,
        device_class=NumberDeviceClass.DURATION, minimum_active=10, enabled_default=True,
    ),
    PhobosNumberSpec(
        key="power_limit", name="Ограничение мощности: порог отключения",
        obis="0.0.17.0.0.255_3", minimum=0, maximum=100000, unit=UnitOfPower.WATT,
        device_class=NumberDeviceClass.POWER,
    ),
    PhobosNumberSpec(
        key="time_turn_off", name="Ограничение мощности: время отключения",
        obis="0.0.17.0.0.255_6", minimum=10, maximum=3600, unit=UnitOfTime.SECONDS,
        device_class=NumberDeviceClass.DURATION,
    ),
    PhobosNumberSpec(
        key="time_turn_on", name="Ограничение мощности: время включения",
        obis="0.0.17.0.0.255_7", minimum=10, maximum=3600, unit=UnitOfTime.SECONDS,
        device_class=NumberDeviceClass.DURATION,
    ),
)


class _PhobosConfigNumberBase(CoordinatorEntity[WaviotMainCoordinator], NumberEntity):
    _attr_mode = NumberMode.BOX
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: WaviotMainCoordinator,
        client: WaviotClient,
        account: WaviotAccount,
        device: WaviotDeviceInfo,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._account = account
        self._device = device
        self._key = key
        self._pending = PendingValue()
        self._attr_unique_id = f"{device.modem_id}_{key}"
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
    def native_value(self) -> float | None:
        status = self._status
        return self._pending.resolve(status.get(self._key) if status else None)


class WaviotPhobosNumber(_PhobosConfigNumberBase):
    """Числовая настройка Фобоса. Облако отдаёт значение только после чтения прибором,
    поэтому до этого оно «неизвестно», но записать его можно."""

    def __init__(
        self,
        coordinator: WaviotMainCoordinator,
        client: WaviotClient,
        account: WaviotAccount,
        device: WaviotDeviceInfo,
        spec: PhobosNumberSpec,
    ) -> None:
        super().__init__(coordinator, client, account, device, spec.key)
        self._spec = spec
        self._attr_name = spec.name
        self._attr_native_min_value = spec.minimum
        self._attr_native_max_value = spec.maximum
        self._attr_native_step = 1
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_device_class = spec.device_class
        self._attr_entity_registry_enabled_default = spec.enabled_default

    async def async_set_native_value(self, value: float) -> None:
        spec = self._spec
        wire_value = round(value)
        if (
            spec.minimum_active is not None
            and wire_value != 0
            and wire_value < spec.minimum_active
        ):
            raise HomeAssistantError(
                f"«{spec.name}»: допустимо 0 (выключено) или от {int(spec.minimum_active)}"
            )
        baseline = self._status.get(spec.key) if self._status else None
        try:
            await self._account.async_call(
                lambda jwt: self._client.phobos_write_number(
                    self._device.modem_id, jwt, spec.obis, wire_value
                )
            )
        except WaviotApiError as err:
            raise HomeAssistantError(f"Не удалось изменить «{spec.name}»: {err}") from err
        self._pending.set(wire_value, baseline)
        self.async_write_ha_state()


class WaviotPowersharingNumber(_PhobosConfigNumberBase):
    """Лимит балансировки мощности счётчика, кВт (p_max).

    Команда несёт state и p_max вместе. Если текущее состояние неизвестно, не пишем:
    подставленное значение по умолчанию могло бы включить балансировку.
    """

    _attr_native_min_value = 0.1
    _attr_native_max_value = 249.9
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_device_class = NumberDeviceClass.POWER
    _attr_entity_registry_enabled_default = False
    _attr_name = "Балансировка: лимит"

    def __init__(
        self,
        coordinator: WaviotMainCoordinator,
        client: WaviotClient,
        account: WaviotAccount,
        device: WaviotDeviceInfo,
    ) -> None:
        super().__init__(coordinator, client, account, device, "powersharing_max")

    async def async_set_native_value(self, value: float) -> None:
        status = self._status or {}
        enabled = status.get("powersharing_enabled")
        if enabled is None:
            raise HomeAssistantError(
                "Текущее состояние балансировки неизвестно: откройте настройки счётчика "
                "в приложении или сначала включите/выключите переключатель «Балансировка: включена»"
            )
        baseline = status.get("powersharing_max")
        try:
            await self._account.async_call(
                lambda jwt: self._client.phobos_write_powersharing(
                    self._device.modem_id, jwt, enabled, value
                )
            )
        except WaviotApiError as err:
            raise HomeAssistantError(f"Не удалось изменить лимит балансировки: {err}") from err
        self._pending.set(value, baseline)
        self.async_write_ha_state()
