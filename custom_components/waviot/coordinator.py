"""DataUpdateCoordinator-ы Waviot: основной опрос + быстрый опрос протечки ladoga."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .account import WaviotAccount
from .api import WaviotApiError, WaviotClient
from .const import (
    CHARGER_CHARGING_UPDATE_INTERVAL,
    CHARGER_IDLE_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    EVSE_STATE_CHARGING,
    HW_TYPE_BETAR,
    LEAK_UPDATE_INTERVAL,
)
from .device import WaviotDeviceInfo
from .parser import parse_device_status

_LOGGER = logging.getLogger(__name__)


class WaviotMainCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Раз в DEFAULT_UPDATE_INTERVAL опрашивает батч-статус всех устройств аккаунта."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: WaviotClient,
        account: WaviotAccount,
        devices: list[WaviotDeviceInfo],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_main",
            update_interval=timedelta(seconds=DEFAULT_UPDATE_INTERVAL),
        )
        self._client = client
        self._account = account
        self._devices = devices

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        modem_ids = [device.modem_id for device in self._devices]
        try:
            raw_batch = await self._account.async_call(
                lambda jwt: self._client.get_status_devices_batch(modem_ids, jwt)
            )
        except WaviotApiError as err:
            raise UpdateFailed(str(err)) from err

        result: dict[str, dict[str, Any]] = {}
        for device in self._devices:
            status = parse_device_status(device.kind, raw_batch.get(device.modem_id))
            if status is None:
                continue
            if device.kind == HW_TYPE_BETAR:
                try:
                    status["voltage"] = await self._account.async_call(
                        lambda jwt, modem_id=device.modem_id: self._client.get_volt_betar(
                            modem_id, jwt
                        )
                    )
                except WaviotApiError as err:
                    _LOGGER.warning(
                        "Не удалось получить напряжение betar %s: %s",
                        device.modem_id,
                        err,
                    )
            result[device.modem_id] = status
        return result


class WaviotLeakCoordinator(DataUpdateCoordinator[dict[str, bool]]):
    """Раз в LEAK_UPDATE_INTERVAL опрашивает протечку по устройствам ladoga."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: WaviotClient,
        account: WaviotAccount,
        modem_ids: list[str],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_leak",
            update_interval=timedelta(seconds=LEAK_UPDATE_INTERVAL),
        )
        self._client = client
        self._account = account
        self._modem_ids = modem_ids

    async def _async_update_data(self) -> dict[str, bool]:
        result: dict[str, bool] = {}
        for modem_id in self._modem_ids:
            try:
                result[modem_id] = await self._account.async_call(
                    lambda jwt, modem_id=modem_id: self._client.check_leak(modem_id, jwt)
                )
            except WaviotApiError as err:
                raise UpdateFailed(str(err)) from err
        return result


class WaviotChargingCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Опрашивает статус зарядок wbox с адаптивным интервалом.

    Пока идёт зарядка (state == Charging хотя бы у одного wbox) — опрос быстрый
    (CHARGER_CHARGING_UPDATE_INTERVAL), иначе — как обычно (CHARGER_IDLE_UPDATE_INTERVAL).
    Пользователь попросил обновлять чаще именно во время активной зарядки.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: WaviotClient,
        account: WaviotAccount,
        devices: list[WaviotDeviceInfo],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_charging",
            update_interval=timedelta(seconds=CHARGER_IDLE_UPDATE_INTERVAL),
        )
        self._client = client
        self._account = account
        self._devices = devices

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        modem_ids = [device.modem_id for device in self._devices]
        try:
            raw_batch = await self._account.async_call(
                lambda jwt: self._client.get_status_devices_batch(modem_ids, jwt)
            )
        except WaviotApiError as err:
            raise UpdateFailed(str(err)) from err

        result: dict[str, dict[str, Any]] = {}
        for device in self._devices:
            status = parse_device_status(device.kind, raw_batch.get(device.modem_id))
            if status is not None:
                result[device.modem_id] = status

        self.update_interval = charging_update_interval(result)
        return result


def charging_update_interval(statuses: dict[str, dict[str, Any]]) -> timedelta:
    """Быстрый интервал, если хоть один wbox сейчас заряжается, иначе обычный."""
    any_charging = any(status["state"] == EVSE_STATE_CHARGING for status in statuses.values())
    seconds = CHARGER_CHARGING_UPDATE_INTERVAL if any_charging else CHARGER_IDLE_UPDATE_INTERVAL
    return timedelta(seconds=seconds)
