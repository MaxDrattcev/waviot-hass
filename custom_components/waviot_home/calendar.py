"""Календарь «История зарядок» станции Wbox: каждая завершённая сессия — событие."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

import aiohttp
from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WaviotConfigEntry
from .account import WaviotAccount
from .api import WaviotApiError, WaviotClient
from .const import DOMAIN, HW_TYPE_WBOX
from .device import WaviotDeviceInfo
from .parser import parse_charge_history

_LOGGER = logging.getLogger(__name__)

# Календарь запрашивает события по нескольку раз при открытии вида — чтобы не дёргать
# облако на каждый вызов, история кэшируется на короткое время.
HISTORY_CACHE_SECONDS = 60


def _decimal(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def _format_duration(seconds: float) -> str:
    minutes = int(seconds) // 60
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"


def session_to_event(modem_id: str, session: dict[str, float]) -> CalendarEvent:
    """Сессия зарядки -> событие: начало = окончание − длительность."""
    end = datetime.fromtimestamp(session["end"], tz=UTC)
    start = end - timedelta(seconds=session["duration"])
    energy = session["t1"] + session["t2"]
    description = [f"Длительность: {_format_duration(session['duration'])}"]
    description.append(f"Тариф 1: {_decimal(session['t1'])} кВт·ч")
    if session["t2"]:
        description.append(f"Тариф 2: {_decimal(session['t2'])} кВт·ч")
    return CalendarEvent(
        start=start,
        end=end,
        summary=f"Зарядка {_decimal(energy)} кВт·ч",
        description="\n".join(description),
        uid=f"{modem_id}_{int(session['end'])}",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WaviotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    async_add_entities(
        WaviotChargeHistoryCalendar(runtime.client, runtime.account, device)
        for device in runtime.devices
        if device.kind == HW_TYPE_WBOX
    )


class WaviotChargeHistoryCalendar(CalendarEntity):
    """История зарядок станции. Прошлые сессии не «предстоящие», поэтому
    состояние календаря всегда «нет события»; ценность — в просмотре периода."""

    _attr_has_entity_name = True
    _attr_name = "История зарядок"
    _attr_icon = "mdi:ev-station"

    def __init__(
        self, client: WaviotClient, account: WaviotAccount, device: WaviotDeviceInfo
    ) -> None:
        self._client = client
        self._account = account
        self._device = device
        self._cache: tuple[float, list[dict[str, float]]] | None = None
        self._lock = asyncio.Lock()
        self._attr_unique_id = f"{device.modem_id}_charge_history"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.modem_id)},
            name=device.title,
            manufacturer="Waviot",
            model=device.kind,
        )

    @property
    def event(self) -> CalendarEvent | None:
        return None

    async def _async_history(self) -> list[dict[str, float]]:
        async with self._lock:
            now = time.monotonic()
            if self._cache and now - self._cache[0] < HISTORY_CACHE_SECONDS:
                return self._cache[1]
            try:
                records = await self._account.async_call(
                    lambda jwt: self._client.get_charge_history(self._device.modem_id, jwt)
                )
            except (WaviotApiError, aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.warning(
                    "Не удалось получить историю зарядок %s: %s", self._device.modem_id, err
                )
                # При сбое показываем то, что уже загружали, а не пустой календарь.
                return self._cache[1] if self._cache else []
            sessions = parse_charge_history(records)
            self._cache = (now, sessions)
            return sessions

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        events = []
        for session in await self._async_history():
            event = session_to_event(self._device.modem_id, session)
            if event.end > start_date and event.start < end_date:
                events.append(event)
        return events
