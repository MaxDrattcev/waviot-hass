"""DataUpdateCoordinator-ы Waviot: основной опрос + быстрый опрос протечки ladoga."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import aiohttp
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
    HW_TYPE_PHOBOS,
    LEAK_UPDATE_INTERVAL,
)
from .device import WaviotDeviceInfo
from .parser import (
    apply_instant,
    parse_charge_session,
    parse_device_status,
    parse_leak_event,
)

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
                await self._async_add_betar_eav(device.modem_id, status)
            result[device.modem_id] = status
        await self._async_add_instant_data(result)
        return result

    async def _async_add_betar_eav(self, modem_id: str, status: dict[str, Any]) -> None:
        """Показание газа (EAV `value`) и напряжение батареи (EAV `voltage`) бетара.

        Источник тот же, что у мобильного приложения. Сбой одного запроса оставляет это
        значение неизвестным, но не роняет опрос остальных приборов.
        """
        for key, obis in (("meter", "value"), ("voltage", "voltage")):
            try:
                status[key] = await self._account.async_call(
                    lambda jwt, obis=obis: self._client.get_latest_eav(modem_id, jwt, obis)
                )
            except (WaviotApiError, aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.warning("Не удалось получить %s бетара %s: %s", obis, modem_id, err)

    async def _async_add_instant_data(self, result: dict[str, dict[str, Any]]) -> None:
        """Мгновенные значения счётчиков (фазы, частота) одним запросом на весь аккаунт.

        Это дополнение: сбой запроса не должен ронять основной опрос статусов.
        """
        phobos_ids = [
            device.modem_id
            for device in self._devices
            if device.kind == HW_TYPE_PHOBOS and device.modem_id in result
        ]
        if not phobos_ids:
            return
        try:
            instant = await self._account.async_call(
                lambda jwt: self._client.get_instant_batch(phobos_ids, jwt)
            )
        except (WaviotApiError, aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("Не удалось получить мгновенные значения счётчиков: %s", err)
            return
        for modem_id, record in instant.items():
            if modem_id in result:
                apply_instant(result[modem_id], record)


class WaviotLeakCoordinator(DataUpdateCoordinator[dict[str, bool]]):
    """Раз в LEAK_UPDATE_INTERVAL опрашивает журнал событий датчиков протечки ladoga.

    Протечка идёт, если последнее событие журнала — «протечка обнаружена» (так же
    считает мобильное приложение).
    """

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
                record = await self._account.async_call(
                    lambda jwt, modem_id=modem_id: self._client.get_last_leak_event(modem_id, jwt)
                )
            except WaviotApiError as err:
                raise UpdateFailed(str(err)) from err
            event = parse_leak_event(record)
            result[modem_id] = event is not None and event["name"] == "leakage_detected"
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
        # Последняя завершённая сессия каждой станции; обновляется, пока станция не заряжается.
        self._sessions: dict[str, dict[str, Any]] = {}

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

        await self._async_update_sessions(result)
        for modem_id, status in result.items():
            status.update(self._sessions.get(modem_id, {}))

        self.update_interval = charging_update_interval(result)
        return result

    async def _async_update_sessions(self, result: dict[str, dict[str, Any]]) -> None:
        """История зарядок: запрос только когда станция не заряжается (во время зарядки
        опрос частый, а новая запись истории появляется лишь по её окончании).

        Это дополнение: сбой запроса не должен ронять основной опрос.
        """
        for modem_id, status in result.items():
            if status["state"] == EVSE_STATE_CHARGING and modem_id in self._sessions:
                continue
            try:
                record = await self._account.async_call(
                    lambda jwt, m=modem_id: self._client.get_last_charge_session(m, jwt)
                )
            except (WaviotApiError, aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.warning("Не удалось получить историю зарядок %s: %s", modem_id, err)
                continue
            session = parse_charge_session(record)
            if session is not None:
                self._sessions[modem_id] = session


def charging_update_interval(statuses: dict[str, dict[str, Any]]) -> timedelta:
    """Быстрый интервал, если хоть один wbox сейчас заряжается, иначе обычный."""
    any_charging = any(status["state"] == EVSE_STATE_CHARGING for status in statuses.values())
    seconds = CHARGER_CHARGING_UPDATE_INTERVAL if any_charging else CHARGER_IDLE_UPDATE_INTERVAL
    return timedelta(seconds=seconds)
