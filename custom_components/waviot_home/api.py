"""Async-клиент Waviot API."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from .const import API_BASE_URL

AUTH_URL = f"{API_BASE_URL}/auth"
DEVICES_URL = f"{API_BASE_URL}/telecom/api/device"
STATUS_URL = f"{API_BASE_URL}/driver_electro5/api/setting"
# Мгновенные значения счётчиков (фазные U/I/P/Q/S, частота) — так их читает мобильное приложение.
INSTANT_URL = f"{API_BASE_URL}/driver_electro5/api/report/history"
INSTANT_PROFILE = "1.0.94.7.0.255"
INSTANT_LIMIT = "100"
DOWNLINK_URL = f"{API_BASE_URL}/api/dl"
EAV_URL = f"{API_BASE_URL}/api/eav"
ROLL_URL = f"{API_BASE_URL}/api/roll"

MAX_DOWNLINK_ATTEMPTS = 20
DOWNLINK_POLL_INTERVAL = 0.1
DOWNLINK_POLL_STEP = 0.1

# Команды зарядки (wbox): 10 попыток по 2 сек, без нарастающего интервала
# (в отличие от реле phobos выше).
CHARGER_CMD_NAME = "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD"
CHARGER_ACTION_START = 2
CHARGER_ACTION_STOP = 4
CHARGER_ACTION_SET_POWER = 0
# Объекты настроек станции: команда -> все поля, которые приложение пишет целиком.
CHARGER_SETTINGS_CMD = "CHARGER_CMD_CHARGE_SETTINGS"
CHARGER_SCHEDULER_CMD = "CHARGER_CMD_EVSE_SCHEDULER"
CHARGER_CONFIRMATION_CMD = "CHARGER_CMD_CHARGE_CONFIRMATION"
CHARGER_OBJECT_FIELDS: dict[str, tuple[str, ...]] = {
    CHARGER_SETTINGS_CMD: (
        "balance_enabled",
        "case_temp_limit",
        "charge_power_limit_enabled",
        "charge_time_limit_enabled",
        "hour_start_day_tariff",
        "hour_start_night_tariff",
        "max_balancing_power",
        "max_charge_power",
        "max_charge_time",
        "use_two_tariff_enabled",
    ),
    CHARGER_SCHEDULER_CMD: ("start", "start_time", "stop", "stop_time"),
    CHARGER_CONFIRMATION_CMD: ("charge_started", "nfc_accept"),
}

# Длительность индикации СмартХаба (команда мобильного приложения).
WALLHUB_INDICATION_CMD = "SSERIAL_CMD_INDICATION_TIMEOUT"

# Числовые настройки счётчика Фобос (OBIS-команды мобильного приложения).
PHOBOS_POWERSHARING_CMD = "powersharing"

CHARGER_POLL_ATTEMPTS = 10
CHARGER_POLL_INTERVAL = 2.0

# Подстроки в ответе API, означающие, что refresh-сессия мертва окончательно
# и повторять обновление токена бессмысленно — нужен полный релогин.
_PERMANENT_SESSION_ERRORS = (
    "session closed",
    "rt invalid sign",
    "old or non-existent refresh token",
)


class WaviotApiError(Exception):
    """Ошибка ответа Waviot API (не-200 статус)."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"waviot api error: {status} {body}")

    @property
    def is_permanent_session_error(self) -> bool:
        lowered = self.body.lower()
        return any(marker in lowered for marker in _PERMANENT_SESSION_ERRORS)


class WaviotAuthError(Exception):
    """Ответ 200, но токены в теле пустые — неверный логин/пароль."""


@dataclass
class WaviotTokens:
    jwt: str
    refresh: str

    def to_json(self) -> dict[str, str]:
        return {"WAVIOT_JWT": self.jwt, "rt": self.refresh}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "WaviotTokens":
        jwt = data.get("WAVIOT_JWT") or ""
        refresh = data.get("rt") or ""
        if not jwt or not refresh:
            raise WaviotAuthError("empty tokens in waviot response")
        return cls(jwt=jwt, refresh=refresh)


class WaviotClient:
    """Тонкий async-клиент Waviot API."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def authorize(self, login: str, password: str) -> WaviotTokens:
        return await self._auth_request({"login": login, "password": password})

    async def refresh_tokens(self, tokens: WaviotTokens) -> WaviotTokens:
        return await self._auth_request(tokens.to_json())

    async def _auth_request(self, body: dict[str, str]) -> WaviotTokens:
        async with self._session.post(AUTH_URL, json=body) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise WaviotApiError(resp.status, text)
            data = await resp.json(content_type=None)
        return WaviotTokens.from_json(data)

    async def get_devices(self, jwt: str) -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {jwt}", "no-redirect": "true"}
        params = {"offset": "0", "limit": "128"}
        async with self._session.get(
            DEVICES_URL, headers=headers, params=params
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise WaviotApiError(resp.status, text)
            return await resp.json(content_type=None)

    async def get_status_devices_batch(
        self, modem_ids: list[str], jwt: str
    ) -> dict[str, dict[str, Any]]:
        if not modem_ids:
            return {}
        headers = {"Authorization": f"Bearer {jwt}", "no-redirect": "true"}
        params = {"modem": ",".join(modem_ids)}
        async with self._session.get(
            STATUS_URL, headers=headers, params=params
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise WaviotApiError(resp.status, text)
            return await resp.json(content_type=None)

    async def get_instant_batch(
        self, modem_ids: list[str], jwt: str
    ) -> dict[str, dict[str, Any]]:
        """Последняя запись мгновенного профиля по каждому счётчику.

        Как в мобильном приложении: запрашивается limit=100, берётся последняя запись.
        Счётчики, не присылающие мгновенный профиль, в результат не попадают.
        """
        if not modem_ids:
            return {}
        headers = {"Authorization": f"Bearer {jwt}", "no-redirect": "true"}
        params = {
            "modem": ",".join(modem_ids),
            "profile": INSTANT_PROFILE,
            "limit": INSTANT_LIMIT,
        }
        async with self._session.get(
            INSTANT_URL, headers=headers, params=params
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise WaviotApiError(resp.status, text)
            data = await resp.json(content_type=None)

        result: dict[str, dict[str, Any]] = {}
        if not isinstance(data, list):
            return result
        for item in data:
            if not isinstance(item, dict):
                continue
            meterings = item.get("meterings")
            if isinstance(meterings, list) and meterings and isinstance(meterings[-1], dict):
                result[str(item.get("modem"))] = meterings[-1]
        return result

    async def get_latest_eav(self, modem_id: str, jwt: str, obis_code: str) -> float | None:
        """Последнее значение EAV по прибору (источник показаний воды, газа и тепла в приложении)."""
        headers = {"Authorization": f"Bearer {jwt}", "no-redirect": "true"}
        params = {
            "type": "value",
            "obis_code": obis_code,
            "modem_id": modem_id,
            "limit": "1",
        }
        async with self._session.get(EAV_URL, headers=headers, params=params) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise WaviotApiError(resp.status, text)
            data = await resp.json(content_type=None)
        events = data.get(modem_id) if isinstance(data, dict) else None
        if not events or not isinstance(events[0], dict):
            return None
        value = events[0].get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    async def get_last_leak_event(self, modem_id: str, jwt: str) -> dict[str, Any] | None:
        """Последняя запись журнала water7.event датчика протечки (как запрашивает приложение)."""
        headers = {"Authorization": f"Bearer {jwt}", "no-redirect": "true"}
        params = {"modem_id": modem_id, "tag": "water7.event", "limit": "1"}
        async with self._session.get(ROLL_URL, headers=headers, params=params) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise WaviotApiError(resp.status, text)
            data = await resp.json(content_type=None)
        records = data.get(modem_id) if isinstance(data, dict) else None
        if isinstance(records, list) and records and isinstance(records[0], dict):
            return records[0]
        return None

    async def get_last_charge_session(self, modem_id: str, jwt: str) -> dict[str, Any] | None:
        """Последняя запись истории зарядок wbox (самая новая идёт первой)."""
        headers = {"Authorization": f"Bearer {jwt}", "no-redirect": "true"}
        params = {"modem_id": modem_id, "tag": "electro5.data_profile_charge", "limit": "1"}
        async with self._session.get(ROLL_URL, headers=headers, params=params) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise WaviotApiError(resp.status, text)
            data = await resp.json(content_type=None)
        records = data.get(modem_id) if isinstance(data, dict) else None
        if isinstance(records, list) and records and isinstance(records[0], dict):
            return records[0]
        return None

    async def get_charge_history(self, modem_id: str, jwt: str) -> list[dict[str, Any]]:
        """Вся история зарядок wbox — тем же запросом, что и мобильное приложение
        (тег без limit и без дат, период отбирается на стороне клиента)."""
        headers = {"Authorization": f"Bearer {jwt}", "no-redirect": "true"}
        params = {"modem_id": modem_id, "tag": "electro5.data_profile_charge"}
        async with self._session.get(ROLL_URL, headers=headers, params=params) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise WaviotApiError(resp.status, text)
            data = await resp.json(content_type=None)
        records = data.get(modem_id) if isinstance(data, dict) else None
        return records if isinstance(records, list) else []

    async def set_power_with_timer(self, modem_id: str, jwt: str, turn_on: bool) -> None:
        """Управление реле phobos: POST timer0-команда, затем ждать исполнения."""
        body = {
            "cmd": "timer0",
            "data": {
                "action": 4 if turn_on else 3,
                "duration": 0,
                "number": 0,
                "period": 0,
                "timestamp": int(time.time()) + 86400,
            },
        }
        bd_id = await self._send_downlink(modem_id, jwt, body)
        await self._wait_downlink_executed(modem_id, bd_id, jwt)

    async def _send_downlink(
        self, modem_id: str, jwt: str, body: dict[str, Any]
    ) -> str:
        headers = {
            "Authorization": f"Bearer {jwt}",
            "no-redirect": "true",
            "Content-Type": "application/json",
        }
        params = {"modem_id": modem_id}
        async with self._session.post(
            DOWNLINK_URL, headers=headers, params=params, json=body
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise WaviotApiError(resp.status, text)
            data = await resp.json(content_type=None)
        modem_data = data.get(modem_id)
        if not modem_data or "bd_id" not in modem_data:
            raise WaviotApiError(resp.status, f"bd_id not found for modem {modem_id}")
        return str(modem_data["bd_id"])

    async def _check_status_with_timer(
        self, modem_id: str, bd_id: str, jwt: str
    ) -> str:
        headers = {"Authorization": f"Bearer {jwt}", "no-redirect": "true"}
        params = {"modem_id": modem_id, "bd_id": bd_id}
        async with self._session.get(
            DOWNLINK_URL, headers=headers, params=params
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise WaviotApiError(resp.status, text)
            data = await resp.json(content_type=None)
        for item in data.get(modem_id, []):
            if item.get("db_id") == bd_id:
                status = item.get("dl_status")
                if not status:
                    raise WaviotApiError(resp.status, f"empty dl_status for bd_id {bd_id}")
                return status
        raise WaviotApiError(resp.status, f"db_id {bd_id} not found for modem {modem_id}")

    async def _wait_downlink_executed(self, modem_id: str, bd_id: str, jwt: str) -> None:
        interval = DOWNLINK_POLL_INTERVAL
        for _ in range(MAX_DOWNLINK_ATTEMPTS):
            status = await self._check_status_with_timer(modem_id, bd_id, jwt)
            if status == "EXECUTED":
                return
            if status in ("FAILED", "TIMEOUT"):
                raise WaviotApiError(0, f"downlink for device {modem_id} failed: {status}")
            if status in ("PENDING", "WAIT_DATA"):
                interval += DOWNLINK_POLL_STEP
            await asyncio.sleep(interval)
        raise WaviotApiError(0, f"downlink for device {modem_id} exceeded timeout")

    async def charger_start_stop(self, modem_id: str, jwt: str, start: bool) -> None:
        """Старт/стоп зарядки wbox."""
        action = CHARGER_ACTION_START if start else CHARGER_ACTION_STOP
        await self._send_charger_short_cmd(modem_id, jwt, {"action": action})

    async def charger_set_max_power(self, modem_id: str, jwt: str, max_power_kw: float) -> None:
        """Установить лимит мощности зарядки wbox.

        Внимание на единицы: при ЧТЕНИИ inst_power/max_power приходят в кВт,
        а при ЗАПИСИ API ожидает Вт. Несимметрично, но это фактическое
        поведение API — отсюда умножение на 1000 ниже.
        """
        await self._send_charger_short_cmd(
            modem_id,
            jwt,
            {"action": CHARGER_ACTION_SET_POWER, "max_power": max_power_kw * 1000},
        )

    async def _send_charger_short_cmd(
        self, modem_id: str, jwt: str, data: dict[str, Any]
    ) -> None:
        await self._send_charger_cmd(modem_id, jwt, CHARGER_CMD_NAME, data)

    async def _send_charger_cmd(
        self, modem_id: str, jwt: str, cmd: str, data: Any
    ) -> None:
        body = {"cmd": cmd, "data": data}
        bd_id = await self._send_downlink(modem_id, jwt, body)
        for _ in range(CHARGER_POLL_ATTEMPTS):
            await asyncio.sleep(CHARGER_POLL_INTERVAL)
            status = await self._check_status_with_timer(modem_id, bd_id, jwt)
            if status in ("FAILED", "TIMEOUT"):
                raise WaviotApiError(0, f"charger command for {modem_id} failed: {status}")
            if status == "EXECUTED":
                return
        raise WaviotApiError(0, f"charger command for {modem_id} exceeded timeout")

    async def charger_update_object(
        self, modem_id: str, jwt: str, cmd: str, changes: dict[str, Any]
    ) -> None:
        """Изменить поля объекта настроек станции.

        Приложение пишет такие объекты целиком, поэтому: берём свежий объект из облака,
        меняем только нужные поля и отправляем все поля. Если текущие значения
        недоступны, не пишем ничего — частичный или выдуманный объект затёр бы настройки.
        """
        fields = CHARGER_OBJECT_FIELDS[cmd]
        unknown = set(changes) - set(fields)
        if unknown:
            raise ValueError(f"unknown fields for {cmd}: {sorted(unknown)}")
        raw = await self.get_status_devices_batch([modem_id], jwt)
        current = (raw.get(modem_id) or {}).get(cmd)
        if not isinstance(current, dict) or any(field not in current for field in fields):
            raise WaviotApiError(
                0,
                f"{cmd} for {modem_id}: current settings unavailable, refusing to write",
            )
        payload = {field: current[field] for field in fields}
        payload.update(changes)
        await self._send_charger_cmd(modem_id, jwt, cmd, payload)

    async def wallhub_write_indication(
        self, modem_id: str, jwt: str, data: dict[str, Any]
    ) -> None:
        """Длительность индикации СмартХаба: {"timeout": секунды} или {"infinite": true}."""
        await self._send_charger_cmd(modem_id, jwt, WALLHUB_INDICATION_CMD, data)

    async def phobos_write_number(
        self, modem_id: str, jwt: str, obis_cmd: str, value: int
    ) -> None:
        """Записать числовую настройку счётчика Фобос (команда — OBIS вида 0.0.17.0.0.255_3)."""
        bd_id = await self._send_downlink(modem_id, jwt, {"cmd": obis_cmd, "data": value})
        await self._wait_downlink_executed(modem_id, bd_id, jwt)

    async def phobos_write_powersharing(
        self, modem_id: str, jwt: str, enabled: bool, p_max_kw: float
    ) -> None:
        """Балансировка мощности счётчика: state + p_max (на проводе Вт, в HA кВт)."""
        body = {
            "cmd": PHOBOS_POWERSHARING_CMD,
            "data": {"state": enabled, "p_max": p_max_kw * 1000},
        }
        bd_id = await self._send_downlink(modem_id, jwt, body)
        await self._wait_downlink_executed(modem_id, bd_id, jwt)
