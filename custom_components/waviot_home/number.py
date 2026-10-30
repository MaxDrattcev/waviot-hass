"""Number-сущность лимита мощности зарядки wbox."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WaviotConfigEntry
from .account import WaviotAccount
from .api import WaviotApiError, WaviotClient
from .const import CHARGER_FALLBACK_MAX_POWER_KW, CHARGER_MIN_POWER_KW, DOMAIN, HW_TYPE_WBOX
from .coordinator import WaviotChargingCoordinator
from .device import WaviotDeviceInfo


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WaviotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    if runtime.charging_coordinator is None:
        return
    entities = [
        WaviotChargerMaxPowerNumber(
            runtime.charging_coordinator, runtime.client, runtime.account, device
        )
        for device in runtime.devices
        if device.kind == HW_TYPE_WBOX
    ]
    async_add_entities(entities)


class WaviotChargerMaxPowerNumber(CoordinatorEntity[WaviotChargingCoordinator], NumberEntity):
    """Лимит мощности зарядки wbox (`max_power`).

    Не путать с `max_power_limit` (отдельный read-only сенсор — потолок от сети,
    см. sensor.py). Верхняя граница слайдера намеренно зафиксирована константой,
    а не берётся из max_power_limit: проверка на живом устройстве показала, что
    это поле "плавает" само по себе и со временем подтягивается к текущему
    max_power, из-за чего диапазон слайдера сужался вместо того, чтобы просто
    двигать позицию.
    """

    _attr_native_min_value = CHARGER_MIN_POWER_KW
    _attr_native_max_value = CHARGER_FALLBACK_MAX_POWER_KW
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

    @property
    def native_value(self) -> float | None:
        status = self._status
        return status["max_power"] if status else None

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
