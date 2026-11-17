"""Кнопка «Обновить данные» на каждом устройстве Waviot."""
from __future__ import annotations

import asyncio

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import WaviotConfigEntry, WaviotRuntimeData
from .const import DOMAIN, HW_TYPE_LADOGA, HW_TYPE_WBOX
from .device import WaviotDeviceInfo


def refresh_targets(
    runtime: WaviotRuntimeData, device: WaviotDeviceInfo
) -> list[DataUpdateCoordinator]:
    """Какие координаторы опрашивают устройство и должны обновиться по кнопке."""
    if device.kind == HW_TYPE_WBOX:
        return [runtime.charging_coordinator] if runtime.charging_coordinator else []
    targets: list[DataUpdateCoordinator] = [runtime.main_coordinator]
    if device.kind == HW_TYPE_LADOGA and runtime.leak_coordinator:
        targets.append(runtime.leak_coordinator)
    return targets


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WaviotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    async_add_entities(
        WaviotRefreshButton(device, refresh_targets(runtime, device))
        for device in runtime.devices
    )


class WaviotRefreshButton(ButtonEntity):
    """Запрашивает у облака WAVIoT свежие данные по устройству.

    Облако отдаёт то, что получило от прибора: приборы на батарейках отчитываются
    редко, поэтому значения могут не измениться. Повторные нажатия чаще, чем раз в
    10 секунд, сглаживает защита координатора.
    """

    _attr_has_entity_name = True
    _attr_name = "Обновить данные"
    _attr_icon = "mdi:refresh"

    def __init__(
        self, device: WaviotDeviceInfo, coordinators: list[DataUpdateCoordinator]
    ) -> None:
        self._coordinators = coordinators
        self._attr_unique_id = f"{device.modem_id}_refresh"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.modem_id)},
            name=device.title,
            manufacturer="Waviot",
            model=device.kind,
        )

    async def async_press(self) -> None:
        await asyncio.gather(
            *(coordinator.async_request_refresh() for coordinator in self._coordinators)
        )
