"""Switch-сущности Waviot: реле phobos и зарядка wbox."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WaviotConfigEntry
from .account import WaviotAccount
from .api import (
    CHARGER_CONFIRMATION_CMD,
    CHARGER_SCHEDULER_CMD,
    CHARGER_SETTINGS_CMD,
    WaviotApiError,
    WaviotClient,
)
from .const import (
    DOMAIN,
    EVSE_STATE_CHARGING,
    EVSE_STATE_WAITING_START,
    HW_TYPE_PHOBOS,
    HW_TYPE_WBOX,
    describe_evse_state,
)
from .coordinator import WaviotChargingCoordinator, WaviotMainCoordinator
from .device import WaviotDeviceInfo
from .parser import parse_device_status
from .pending import PendingValue


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WaviotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    entities: list[SwitchEntity] = []
    for device in runtime.devices:
        if device.kind == HW_TYPE_PHOBOS:
            entities.append(
                WaviotRelaySwitch(runtime.main_coordinator, runtime.client, runtime.account, device)
            )
        elif device.kind == HW_TYPE_WBOX and runtime.charging_coordinator is not None:
            entities.append(
                WaviotChargerSwitch(
                    runtime.charging_coordinator, runtime.client, runtime.account, device
                )
            )
            entities.extend(
                WaviotChargerConfigSwitch(
                    runtime.charging_coordinator, runtime.client, runtime.account, device, spec
                )
                for spec in WBOX_CONFIG_SWITCHES
            )
        if device.kind == HW_TYPE_PHOBOS:
            entities.append(
                WaviotPowersharingSwitch(
                    runtime.main_coordinator, runtime.client, runtime.account, device
                )
            )
    async_add_entities(entities)


class _WaviotSwitchBase(CoordinatorEntity, SwitchEntity):
    """Общая база: карточка устройства + доступ к последнему статусу.

    Координатор может быть как WaviotMainCoordinator (phobos), так и
    WaviotChargingCoordinator (wbox) — оба отдают одинаковую по форме
    dict[str, dict[str, Any]], поэтому не типизируем жёстко generic-параметром.
    """

    def __init__(
        self,
        coordinator: WaviotMainCoordinator | WaviotChargingCoordinator,
        client: WaviotClient,
        account: WaviotAccount,
        device: WaviotDeviceInfo,
        key: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._account = account
        self._device = device
        self._optimistic_on: bool | None = None
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

    def _handle_coordinator_update(self) -> None:
        # Реальный опрос всегда важнее оптимистичного предположения о результате команды.
        self._optimistic_on = None
        super()._handle_coordinator_update()


class WaviotRelaySwitch(_WaviotSwitchBase):
    """Реле phobos.

    Команда исполняется асинхронно на стороне Waviot (до ~20 сек, см. api.py
    set_power_with_timer) — чтобы UI сразу показывал "переключается", а не висел,
    целевое состояние выставляется оптимистично сразу, а после ответа Waviot
    запрашивается реальный опрос координатора, который его подтвердит или поправит.
    """

    def __init__(
        self,
        coordinator: WaviotMainCoordinator,
        client: WaviotClient,
        account: WaviotAccount,
        device: WaviotDeviceInfo,
    ) -> None:
        super().__init__(coordinator, client, account, device, "switch", "Реле")

    @property
    def available(self) -> bool:
        return super().available and self._status is not None

    @property
    def is_on(self) -> bool | None:
        if self._optimistic_on is not None:
            return self._optimistic_on
        status = self._status
        return status["on"] if status else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set_power(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set_power(False)

    async def _async_set_power(self, turn_on: bool) -> None:
        self._optimistic_on = turn_on
        self.async_write_ha_state()
        try:
            await self._account.async_call(
                lambda jwt: self._client.set_power_with_timer(
                    self._device.modem_id, jwt, turn_on
                )
            )
        except WaviotApiError as err:
            self._optimistic_on = None
            self.async_write_ha_state()
            await self.coordinator.async_request_refresh()
            raise HomeAssistantError(f"Не удалось переключить реле: {err}") from err
        # При успехе НЕ дёргаем coordinator.async_request_refresh() сразу: устройство
        # само отчитывается на сервер Waviot нечасто (иногда раз в несколько часов),
        # поэтому немедленный опрос почти гарантированно вернёт тот же устаревший кэш
        # и затрёт только что подтверждённый результат команды оптимистичным откатом.
        # Координатор скорректирует состояние сам на ближайшем плановом опросе, когда
        # устройство реально пришлёт новый статус.


class WaviotChargerSwitch(_WaviotSwitchBase):
    """Зарядка wbox — включать/выключать можно только когда подключено.

    Сущность остаётся available, пока координатор вообще видит устройство:
    "не подключено" — обычное повседневное состояние (машины нет), а не
    ошибка, поэтому не должно рисоваться в HA как тревожный значок
    "Недоступно". Вместо этого команда в неподходящем состоянии блокируется
    понятной ошибкой в async_turn_on/async_turn_off.
    """

    def __init__(
        self,
        coordinator: WaviotChargingCoordinator,
        client: WaviotClient,
        account: WaviotAccount,
        device: WaviotDeviceInfo,
    ) -> None:
        super().__init__(coordinator, client, account, device, "charger_switch", "Зарядка")

    @property
    def available(self) -> bool:
        return super().available and self._status is not None

    @property
    def is_on(self) -> bool | None:
        if self._optimistic_on is not None:
            return self._optimistic_on
        status = self._status
        return status["on"] if status else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        state = await self._fetch_fresh_state()
        if state == EVSE_STATE_CHARGING:
            return
        if state != EVSE_STATE_WAITING_START:
            raise HomeAssistantError(
                "Зарядка не готова к запуску "
                f"(статус: {describe_evse_state(state) if state is not None else 'неизвестно'})"
            )
        await self._async_set_power(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        state = await self._fetch_fresh_state()
        if state != EVSE_STATE_CHARGING:
            return
        await self._async_set_power(False)

    async def _fetch_fresh_state(self) -> int | None:
        """Свежий запрос статуса перед командой.

        Кэш координатора (self._status) может быть устаревшим, и здесь это
        критично: решение "уже заряжается / уже не заряжается" на устаревших
        данных приводило к молчаливому no-op — включить не получалось сразу
        после только что выполненного выключения.
        """
        modem_id = self._device.modem_id
        try:
            raw_batch = await self._account.async_call(
                lambda jwt: self._client.get_status_devices_batch([modem_id], jwt)
            )
        except WaviotApiError:
            return None
        status = parse_device_status(self._device.kind, raw_batch.get(modem_id))
        return status["state"] if status else None

    async def _async_set_power(self, turn_on: bool) -> None:
        self._optimistic_on = turn_on
        self.async_write_ha_state()
        try:
            await self._account.async_call(
                lambda jwt: self._client.charger_start_stop(
                    self._device.modem_id, jwt, turn_on
                )
            )
        except WaviotApiError as err:
            self._optimistic_on = None
            self.async_write_ha_state()
            await self.coordinator.async_request_refresh()
            raise HomeAssistantError(f"Не удалось управлять зарядкой: {err}") from err


@dataclass(frozen=True, kw_only=True)
class ChargerSwitchSpec:
    """Настройка станции, которую можно включить или выключить.

    key — ключ в статусе и в unique_id; cmd и field — объект и поле настроек станции;
    as_int — флаг хранится числом 0/1 (CHARGER_CMD_CHARGE_SETTINGS), иначе булевым.
    """

    key: str
    name: str
    cmd: str
    field: str
    as_int: bool


# Только настройки, которые есть в мобильном приложении. Имена подобраны так, чтобы по
# алфавиту (так HA сортирует сущности на странице устройства) переключатель шёл первым,
# а число/время той же группы — сразу за ним: «…: включено» < «…: время/начало».
WBOX_CONFIG_SWITCHES: tuple[ChargerSwitchSpec, ...] = (
    ChargerSwitchSpec(
        key="balance_enabled", name="Балансировка: включена",
        cmd=CHARGER_SETTINGS_CMD, field="balance_enabled", as_int=True,
    ),
    ChargerSwitchSpec(
        key="use_two_tariff_enabled", name="Тарифы: два тарифа",
        cmd=CHARGER_SETTINGS_CMD, field="use_two_tariff_enabled", as_int=True,
    ),
    ChargerSwitchSpec(
        key="schedule_start_enabled", name="Расписание запуска: включено",
        cmd=CHARGER_SCHEDULER_CMD, field="start", as_int=False,
    ),
    ChargerSwitchSpec(
        key="schedule_stop_enabled", name="Расписание остановки: включено",
        cmd=CHARGER_SCHEDULER_CMD, field="stop", as_int=False,
    ),
    ChargerSwitchSpec(
        key="nfc_confirmation", name="NFC: подтверждение картой",
        cmd=CHARGER_CONFIRMATION_CMD, field="nfc_accept", as_int=False,
    ),
)


class WaviotChargerConfigSwitch(_WaviotSwitchBase):
    """Переключатель настройки станции wbox.

    Запись идёт по схеме «свежий объект настроек из облака, изменить одно поле, отправить
    весь объект» (как делает мобильное приложение), см. WaviotClient.charger_update_object.
    """

    def __init__(
        self,
        coordinator: WaviotChargingCoordinator,
        client: WaviotClient,
        account: WaviotAccount,
        device: WaviotDeviceInfo,
        spec: ChargerSwitchSpec,
    ) -> None:
        super().__init__(coordinator, client, account, device, spec.key, spec.name)
        self._spec = spec
        self._pending = PendingValue()
        self._attr_has_entity_name = True
        self._attr_entity_category = EntityCategory.CONFIG

    @property
    def available(self) -> bool:
        return super().available and self._status is not None

    @property
    def is_on(self) -> bool | None:
        status = self._status
        return self._pending.resolve(status.get(self._spec.key) if status else None)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, turn_on: bool) -> None:
        spec = self._spec
        baseline = self._status.get(spec.key) if self._status else None
        value = int(turn_on) if spec.as_int else turn_on
        try:
            await self._account.async_call(
                lambda jwt: self._client.charger_update_object(
                    self._device.modem_id, jwt, spec.cmd, {spec.field: value}
                )
            )
        except WaviotApiError as err:
            raise HomeAssistantError(f"Не удалось изменить «{spec.name}»: {err}") from err
        self._pending.set(turn_on, baseline)
        self.async_write_ha_state()


class WaviotPowersharingSwitch(_WaviotSwitchBase):
    """Балансировка мощности счётчика Фобос.

    Команда `powersharing` несёт сразу state и p_max. Если текущий p_max неизвестен
    (облако отдаёт его только после чтения прибором), не пишем: подставленное значение
    по умолчанию могло бы ограничить нагрузку.
    """

    def __init__(
        self,
        coordinator: WaviotMainCoordinator,
        client: WaviotClient,
        account: WaviotAccount,
        device: WaviotDeviceInfo,
    ) -> None:
        super().__init__(
            coordinator, client, account, device, "powersharing_enabled", "Балансировка: включена"
        )
        self._pending = PendingValue()
        self._attr_has_entity_name = True
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_entity_registry_enabled_default = False

    @property
    def available(self) -> bool:
        return super().available and self._status is not None

    @property
    def is_on(self) -> bool | None:
        status = self._status
        return self._pending.resolve(status.get("powersharing_enabled") if status else None)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, turn_on: bool) -> None:
        status = self._status or {}
        p_max = status.get("powersharing_max")
        if p_max is None:
            raise HomeAssistantError(
                "Текущий лимит балансировки неизвестен: сначала задайте его (число "
                "«Балансировка: лимит») или откройте настройки счётчика в приложении"
            )
        try:
            await self._account.async_call(
                lambda jwt: self._client.phobos_write_powersharing(
                    self._device.modem_id, jwt, turn_on, p_max
                )
            )
        except WaviotApiError as err:
            raise HomeAssistantError(f"Не удалось изменить балансировку: {err}") from err
        self._pending.set(turn_on, status.get("powersharing_enabled"))
        self.async_write_ha_state()
