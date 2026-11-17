"""Время запуска и остановки зарядки по расписанию станции Wbox."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WaviotConfigEntry
from .account import WaviotAccount
from .api import CHARGER_SCHEDULER_CMD, WaviotApiError, WaviotClient
from .const import DOMAIN, HW_TYPE_WBOX
from .coordinator import WaviotChargingCoordinator
from .device import WaviotDeviceInfo
from .pending import PendingValue


@dataclass(frozen=True, kw_only=True)
class ScheduleTimeSpec:
    """key — ключ в статусе («ЧЧ:ММ») и в unique_id; field — поле объекта расписания."""

    key: str
    name: str
    field: str


SCHEDULE_TIMES: tuple[ScheduleTimeSpec, ...] = (
    ScheduleTimeSpec(key="schedule_start_time", name="Расписание запуска: время", field="start_time"),
    ScheduleTimeSpec(key="schedule_stop_time", name="Расписание остановки: время", field="stop_time"),
)


def hhmm_to_time(value: str | None) -> time | None:
    if not value:
        return None
    hours, _, minutes = value.partition(":")
    try:
        return time(int(hours), int(minutes))
    except ValueError:
        return None


def time_to_minutes(value: time) -> int:
    """Приложение хранит время расписания минутами от полуночи (слайдер 0–1440)."""
    return value.hour * 60 + value.minute


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WaviotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    if runtime.charging_coordinator is None:
        return
    async_add_entities(
        WaviotScheduleTime(
            runtime.charging_coordinator, runtime.client, runtime.account, device, spec
        )
        for device in runtime.devices
        if device.kind == HW_TYPE_WBOX
        for spec in SCHEDULE_TIMES
    )


class WaviotScheduleTime(CoordinatorEntity[WaviotChargingCoordinator], TimeEntity):
    """Время расписания зарядки. Запись: свежий объект расписания, одно поле, весь объект."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: WaviotChargingCoordinator,
        client: WaviotClient,
        account: WaviotAccount,
        device: WaviotDeviceInfo,
        spec: ScheduleTimeSpec,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._account = account
        self._device = device
        self._spec = spec
        self._pending = PendingValue()
        self._attr_unique_id = f"{device.modem_id}_{spec.key}"
        self._attr_name = spec.name
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
    def native_value(self) -> time | None:
        status = self._status
        cloud = status.get(self._spec.key) if status else None
        shown = self._pending.resolve(cloud)
        return hhmm_to_time(shown)

    async def async_set_value(self, value: time) -> None:
        spec = self._spec
        status = self._status or {}
        baseline = status.get(spec.key)
        try:
            await self._account.async_call(
                lambda jwt: self._client.charger_update_object(
                    self._device.modem_id,
                    jwt,
                    CHARGER_SCHEDULER_CMD,
                    {spec.field: time_to_minutes(value)},
                )
            )
        except WaviotApiError as err:
            raise HomeAssistantError(f"Не удалось изменить «{spec.name}»: {err}") from err
        self._pending.set(f"{value.hour:02d}:{value.minute:02d}", baseline)
        self.async_write_ha_state()
