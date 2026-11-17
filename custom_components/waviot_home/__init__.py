"""Интеграция Waviot для Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .account import WaviotAccount
from .api import WaviotApiError, WaviotAuthError, WaviotClient
from .const import HW_TYPE_LADOGA, HW_TYPE_WBOX, SUPPORTED_HW_TYPES
from .coordinator import WaviotChargingCoordinator, WaviotLeakCoordinator, WaviotMainCoordinator
from .device import WaviotDeviceInfo
from .events_client import WaviotEventsClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.BUTTON,
    Platform.CALENDAR,
    Platform.TIME,
    Platform.SELECT,
]


@dataclass
class WaviotRuntimeData:
    client: WaviotClient
    account: WaviotAccount
    main_coordinator: WaviotMainCoordinator
    leak_coordinator: WaviotLeakCoordinator | None
    charging_coordinator: WaviotChargingCoordinator | None
    devices: list[WaviotDeviceInfo]
    events_client: WaviotEventsClient


type WaviotConfigEntry = ConfigEntry[WaviotRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: WaviotConfigEntry) -> bool:
    client = WaviotClient(async_get_clientsession(hass))
    account = WaviotAccount(hass, entry, client)

    # Временный сбой облака при старте — не повод падать в SETUP_ERROR:
    # ConfigEntryNotReady заставляет Home Assistant повторять настройку с нарастающей паузой.
    # ConfigEntryAuthFailed из async_refresh не перехватываем — он запускает реавторизацию.
    try:
        jwt = await account.async_refresh()
        raw_devices = await client.get_devices(jwt)
    except (WaviotApiError, WaviotAuthError, aiohttp.ClientError, TimeoutError) as err:
        raise ConfigEntryNotReady(f"Не удалось получить данные из облака WAVIoT HOME: {err}") from err

    devices: list[WaviotDeviceInfo] = []
    for raw in raw_devices:
        info = WaviotDeviceInfo.from_api(raw)
        if info is None:
            continue
        if info.kind not in SUPPORTED_HW_TYPES:
            _LOGGER.debug("Пропускаю пока не поддержанный тип устройства: %s", info)
            continue
        devices.append(info)

    # wbox опрашивается отдельным адаптивным координатором (WaviotChargingCoordinator) —
    # исключаем его из основного, чтобы не дублировать запросы к одним и тем же modem_id.
    main_devices = [d for d in devices if d.kind != HW_TYPE_WBOX]
    main_coordinator = WaviotMainCoordinator(hass, client, account, main_devices)
    await main_coordinator.async_config_entry_first_refresh()

    ladoga_ids = [d.modem_id for d in devices if d.kind == HW_TYPE_LADOGA]
    leak_coordinator: WaviotLeakCoordinator | None = None
    if ladoga_ids:
        leak_coordinator = WaviotLeakCoordinator(hass, client, account, ladoga_ids)
        await leak_coordinator.async_config_entry_first_refresh()

    wbox_devices = [d for d in devices if d.kind == HW_TYPE_WBOX]
    charging_coordinator: WaviotChargingCoordinator | None = None
    if wbox_devices:
        charging_coordinator = WaviotChargingCoordinator(hass, client, account, wbox_devices)
        await charging_coordinator.async_config_entry_first_refresh()

    events_client = WaviotEventsClient(
        hass,
        account,
        main_coordinator,
        leak_coordinator,
        charging_coordinator,
    )

    entry.runtime_data = WaviotRuntimeData(
        client=client,
        account=account,
        main_coordinator=main_coordinator,
        leak_coordinator=leak_coordinator,
        charging_coordinator=charging_coordinator,
        devices=devices,
        events_client=events_client,
    )

    # events_client запускаем только после успешной настройки платформ: если
    # async_forward_entry_setups упадёт, async_setup_entry целиком считается
    # неудачным и async_unload_entry не вызывается — запущенный раньше клиент
    # и его AMQP-соединение останутся висеть до перезапуска HA.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    events_client.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WaviotConfigEntry) -> bool:
    await entry.runtime_data.events_client.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
