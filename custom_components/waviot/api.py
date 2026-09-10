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

    async def get_volt_betar(self, modem_id: str, jwt: str) -> float:
        headers = {"Authorization": f"Bearer {jwt}", "no-redirect": "true"}
        params = {
            "type": "value",
            "obis_code": "voltage",
            "modem_id": modem_id,
            "limit": "1",
        }
        async with self._session.get(EAV_URL, headers=headers, params=params) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise WaviotApiError(resp.status, text)
            data = await resp.json(content_type=None)
        events = data.get(modem_id) or []
        if not events:
            return 0.0
        return float(events[0].get("value", 0.0))

    async def check_leak(self, modem_id: str, jwt: str) -> bool:
        headers = {"Authorization": f"Bearer {jwt}", "no-redirect": "true"}
        params = {"modem_id": modem_id, "tag": "water7.event", "limit": "1"}
        async with self._session.get(ROLL_URL, headers=headers, params=params) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise WaviotApiError(resp.status, text)
            data = await resp.json(content_type=None)
        events = data.get(modem_id) or []
        for event in events:
            for item in (event.get("data") or {}).values():
                if item.get("name") == "leakage_detected":
                    return True
        return False

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
        body = {"cmd": CHARGER_CMD_NAME, "data": data}
        bd_id = await self._send_downlink(modem_id, jwt, body)
        for _ in range(CHARGER_POLL_ATTEMPTS):
            await asyncio.sleep(CHARGER_POLL_INTERVAL)
            status = await self._check_status_with_timer(modem_id, bd_id, jwt)
            if status in ("FAILED", "TIMEOUT"):
                raise WaviotApiError(0, f"charger command for {modem_id} failed: {status}")
            if status == "EXECUTED":
                return
        raise WaviotApiError(0, f"charger command for {modem_id} exceeded timeout")
