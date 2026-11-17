"""Тесты чистой логики координаторов — без реального HA/API."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.waviot_home.api import WaviotApiError
from custom_components.waviot_home.const import (
    CHARGER_CHARGING_UPDATE_INTERVAL,
    CHARGER_IDLE_UPDATE_INTERVAL,
    EVSE_STATE_CHARGING,
    EVSE_STATE_WAITING_START,
)
from custom_components.waviot_home.coordinator import (
    WaviotChargingCoordinator,
    WaviotMainCoordinator,
    charging_update_interval,
)
from custom_components.waviot_home.device import WaviotDeviceInfo


def test_idle_interval_when_nothing_charging():
    statuses = {
        "1": {"state": EVSE_STATE_WAITING_START},
        "2": {"state": EVSE_STATE_WAITING_START},
    }
    assert charging_update_interval(statuses) == timedelta(seconds=CHARGER_IDLE_UPDATE_INTERVAL)


def test_fast_interval_when_at_least_one_charging():
    statuses = {
        "1": {"state": EVSE_STATE_WAITING_START},
        "2": {"state": EVSE_STATE_CHARGING},
    }
    assert charging_update_interval(statuses) == timedelta(seconds=CHARGER_CHARGING_UPDATE_INTERVAL)


def test_idle_interval_when_no_devices_at_all():
    assert charging_update_interval({}) == timedelta(seconds=CHARGER_IDLE_UPDATE_INTERVAL)


# --- мгновенные значения счётчиков ---------------------------------------------------

def _instant_self(client: MagicMock, kinds: dict[str, str]) -> SimpleNamespace:
    devices = [
        WaviotDeviceInfo.from_api({"modem_id": modem_id, "hw_type": hw_type})
        for modem_id, hw_type in kinds.items()
    ]
    async def _async_call(func):
        return await func("jwt")

    account = MagicMock()
    account.async_call = AsyncMock(side_effect=_async_call)
    return SimpleNamespace(_devices=devices, _account=account, _client=client)


async def test_instant_values_are_merged_into_phobos_status():
    client = MagicMock()
    client.get_instant_batch = AsyncMock(return_value={"1": {"32.7.0": 230.0, "52.7.0": 231.0}})
    fake = _instant_self(client, {"1": "phobos3_wa_stm", "2": "ladoga_stm"})
    result = {"1": {"voltage": None}, "2": {"voltage": 3.1}}

    await WaviotMainCoordinator._async_add_instant_data(fake, result)

    assert result["1"]["voltage"] == 230.0
    assert result["1"]["voltage_b"] == 231.0
    assert result["2"] == {"voltage": 3.1}
    client.get_instant_batch.assert_awaited_once_with(["1"], "jwt")


async def test_no_instant_request_without_phobos():
    client = MagicMock()
    client.get_instant_batch = AsyncMock()
    fake = _instant_self(client, {"2": "ladoga_stm"})

    await WaviotMainCoordinator._async_add_instant_data(fake, {"2": {}})

    client.get_instant_batch.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [WaviotApiError(500, "boom"), aiohttp.ClientConnectionError("down"), TimeoutError()],
    ids=lambda e: type(e).__name__,
)
async def test_instant_failure_does_not_break_main_update(error):
    client = MagicMock()
    client.get_instant_batch = AsyncMock(side_effect=error)
    fake = _instant_self(client, {"1": "phobos3_wa_stm"})
    result = {"1": {"voltage": None, "meter": 5.0}}

    await WaviotMainCoordinator._async_add_instant_data(fake, result)

    assert result == {"1": {"voltage": None, "meter": 5.0}}


# --- история зарядок ------------------------------------------------------------------

_RECORD = {
    "timestamp": 100,
    "data": {"data": {"0100000809FF": 60, "0100010801FF": 1.5, "0100010802FF": 0.0}},
}


def _charging_self(client: MagicMock) -> SimpleNamespace:
    async def _async_call(func):
        return await func("jwt")

    account = MagicMock()
    account.async_call = AsyncMock(side_effect=_async_call)
    return SimpleNamespace(_account=account, _client=client, _sessions={})


async def test_session_fetched_when_not_charging_and_cached():
    client = MagicMock()
    client.get_last_charge_session = AsyncMock(return_value=_RECORD)
    fake = _charging_self(client)

    await WaviotChargingCoordinator._async_update_sessions(fake, {"1": {"state": 3}})

    assert fake._sessions["1"]["last_session_energy"] == 1.5
    assert fake._sessions["1"]["last_session_duration"] == 60


async def test_session_not_refetched_while_charging_once_cached():
    client = MagicMock()
    client.get_last_charge_session = AsyncMock(return_value=_RECORD)
    fake = _charging_self(client)
    fake._sessions["1"] = {"last_session_energy": 9.9}

    await WaviotChargingCoordinator._async_update_sessions(fake, {"1": {"state": EVSE_STATE_CHARGING}})

    client.get_last_charge_session.assert_not_awaited()
    assert fake._sessions["1"] == {"last_session_energy": 9.9}


async def test_session_fetched_once_even_while_charging_if_nothing_cached():
    client = MagicMock()
    client.get_last_charge_session = AsyncMock(return_value=_RECORD)
    fake = _charging_self(client)

    await WaviotChargingCoordinator._async_update_sessions(fake, {"1": {"state": EVSE_STATE_CHARGING}})

    client.get_last_charge_session.assert_awaited_once()


@pytest.mark.parametrize(
    "error",
    [WaviotApiError(500, "boom"), aiohttp.ClientConnectionError("down"), TimeoutError()],
    ids=lambda e: type(e).__name__,
)
async def test_session_failure_keeps_previous_value_and_does_not_raise(error):
    client = MagicMock()
    client.get_last_charge_session = AsyncMock(side_effect=error)
    fake = _charging_self(client)
    fake._sessions["1"] = {"last_session_energy": 2.0}

    await WaviotChargingCoordinator._async_update_sessions(fake, {"1": {"state": 3}})

    assert fake._sessions["1"] == {"last_session_energy": 2.0}
