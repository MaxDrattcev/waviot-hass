"""Вода и газ: единицы и источник показаний — как в мобильном приложении."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.waviot_home.api import WaviotApiError, WaviotClient
from custom_components.waviot_home.coordinator import WaviotMainCoordinator
from custom_components.waviot_home.device import WaviotDeviceInfo
from custom_components.waviot_home.sensor import WaviotWaterMeterSensor


def _device(kind: str, hw_type: str) -> WaviotDeviceInfo:
    return WaviotDeviceInfo(modem_id="7", hw_type=hw_type, kind=kind)


# --- единицы воды -----------------------------------------------------------------------

def test_aqua_cold_water_is_cubic_meters_like_in_the_app():
    """liter_forw = 85.05 приложение показывает как 85.05 м³ (суточное значение 74120 л
    меньше текущих 85050 л), поэтому единица — м³, а не литры."""
    from homeassistant.const import UnitOfVolume

    sensor = WaviotWaterMeterSensor(
        MagicMock(), _device("aqua", "aqua2_stm"), "Холодная вода", UnitOfVolume.CUBIC_METERS
    )
    assert sensor.native_unit_of_measurement == "m³"


def test_gefest_unit_is_left_as_before_because_nothing_to_verify_against():
    sensor = WaviotWaterMeterSensor(MagicMock(), _device("gefest", "gefest_stm"), "Горячая вода")
    assert sensor.native_unit_of_measurement == "L"


# --- EAV ------------------------------------------------------------------------------

class _Resp:
    status = 200

    def __init__(self, body):
        self._body = body

    async def text(self):
        return "ok"

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, body):
        self._body = body
        self.calls = []

    def get(self, url, headers=None, params=None):
        self.calls.append({"url": url, "params": params})
        return _Resp(self._body)


async def test_get_latest_eav_requests_like_the_app_and_returns_value():
    session = _Session({"7": [{"obis_code": "value", "timestamp": 1, "value": 1877.95}]})
    assert await WaviotClient(session).get_latest_eav("7", "jwt", "value") == 1877.95
    assert session.calls[0]["url"].endswith("/api/eav")
    assert session.calls[0]["params"] == {
        "type": "value", "obis_code": "value", "modem_id": "7", "limit": "1",
    }


@pytest.mark.parametrize(
    "body",
    [{}, {"7": []}, {"7": [{"value": None}]}, {"7": [{"value": "x"}]}, [], {"7": ["junk"]}],
)
async def test_get_latest_eav_none_when_no_usable_value(body):
    assert await WaviotClient(_Session(body)).get_latest_eav("7", "jwt", "value") is None


# --- бетар в координаторе -----------------------------------------------------------------

def _betar_self(client: MagicMock) -> SimpleNamespace:
    async def _call(func):
        return await func("jwt")

    account = MagicMock()
    account.async_call = AsyncMock(side_effect=_call)
    return SimpleNamespace(_account=account, _client=client)


async def test_betar_meter_and_voltage_filled_from_eav():
    client = MagicMock()

    async def _eav(modem_id, jwt, obis):
        return {"value": 1877.95, "voltage": 3.6}[obis]

    client.get_latest_eav = _eav
    status = {"meter": None, "voltage": None}
    await WaviotMainCoordinator._async_add_betar_eav(_betar_self(client), "7", status)
    assert status == {"meter": 1877.95, "voltage": 3.6}


@pytest.mark.parametrize(
    "error",
    [WaviotApiError(500, "boom"), aiohttp.ClientConnectionError("down"), TimeoutError()],
    ids=lambda e: type(e).__name__,
)
async def test_betar_eav_failure_keeps_unknown_and_does_not_raise(error):
    client = MagicMock()
    client.get_latest_eav = AsyncMock(side_effect=error)
    status = {"meter": None, "voltage": None}
    await WaviotMainCoordinator._async_add_betar_eav(_betar_self(client), "7", status)
    assert status == {"meter": None, "voltage": None}  # неизвестно, а не ложный 0
