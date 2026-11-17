"""Тесты сущностей зарядки wbox (switch + number) — только моки, без реального API."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.waviot_home.account import WaviotAccount
from custom_components.waviot_home.api import WaviotApiError, WaviotClient
from custom_components.waviot_home.const import (
    CHARGER_FALLBACK_MAX_POWER_KW,
    CHARGER_MIN_POWER_KW,
    EVSE_STATE_CHARGING,
    EVSE_STATE_READY_TO_START,
    EVSE_STATE_WAITING_START,
)
from custom_components.waviot_home.device import WaviotDeviceInfo
from custom_components.waviot_home.number import WaviotChargerMaxPowerNumber
from custom_components.waviot_home.switch import WaviotChargerSwitch


def _status(state: int, **overrides) -> dict:
    data = {
        "state": state,
        "on": state == EVSE_STATE_CHARGING,
        "power": 3.5,
        "max_power": 7.0,
        "max_power_limit": 11.0,
        "meter": 2.1,
        "temperature": 30.0,
    }
    data.update(overrides)
    return data


def _raw_batch(modem_id: str, state: int) -> dict:
    """Сырой ответ get_status_devices_batch — как реально возвращает Waviot API.

    Нужен отдельно от _status(): async_turn_on/async_turn_off теперь делают
    свежий запрос (не берут кэш координатора), см. фикс с _fetch_fresh_state.
    """
    return {
        modem_id: {
            "common": {"time_saved": int(time.time())},
            "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD": {
                "state": state,
                "inst_power": 3.5,
                "max_power": 7.0,
                "max_power_limit": 11.0,
                "count_power": 2.1,
            },
        }
    }


@pytest.fixture
def device() -> WaviotDeviceInfo:
    return WaviotDeviceInfo(modem_id="777", hw_type="wbox_nvt_wa_stm", kind="wbox")


@pytest.fixture
def coordinator() -> MagicMock:
    coord = MagicMock()
    coord.data = {"777": _status(EVSE_STATE_WAITING_START)}
    coord.async_request_refresh = AsyncMock()
    return coord


@pytest.fixture
def client() -> MagicMock:
    return MagicMock(spec=WaviotClient)


@pytest.fixture
def account() -> MagicMock:
    acc = MagicMock(spec=WaviotAccount)
    acc.jwt = "fake-jwt"

    async def _call(func):
        return await func(acc.jwt)

    acc.async_call = AsyncMock(side_effect=_call)
    return acc


def _make_switch(coordinator, client, account, device) -> WaviotChargerSwitch:
    switch = WaviotChargerSwitch(coordinator, client, account, device)
    switch.async_write_ha_state = MagicMock()
    return switch


def _make_number(coordinator, client, account, device) -> WaviotChargerMaxPowerNumber:
    number = WaviotChargerMaxPowerNumber(coordinator, client, account, device)
    number.async_write_ha_state = MagicMock()
    return number


# --- WaviotChargerSwitch: available зависит только от наличия статуса, не от
# состояния EVSE — "не подключено" это обычное состояние, а не ошибка, и не
# должно рисоваться как "Недоступно" в интерфейсе. Блокировка неверной команды
# идёт через понятную ошибку в async_turn_on/async_turn_off (см. ниже). ---


@pytest.mark.parametrize(
    "state",
    [
        EVSE_STATE_READY_TO_START,  # разъём свободен — не подключено, но не ошибка
        EVSE_STATE_WAITING_START,
        EVSE_STATE_CHARGING,
        6,  # ошибка (например УЗО)
        999,  # неизвестное состояние
    ],
)
def test_available_whenever_status_present_regardless_of_evse_state(
    coordinator, client, account, device, state
):
    coordinator.data["777"] = _status(state)
    switch = _make_switch(coordinator, client, account, device)
    assert switch.available is True


def test_unavailable_when_no_status_at_all(coordinator, client, account, device):
    coordinator.data.clear()
    switch = _make_switch(coordinator, client, account, device)
    assert switch.available is False


# --- turn_on: разрешено только из WaitingStart, no-op из Charging, ошибка иначе ---


async def test_turn_on_from_waiting_start_sends_command(coordinator, client, account, device):
    coordinator.data["777"] = _status(EVSE_STATE_WAITING_START)
    client.get_status_devices_batch = AsyncMock(
        return_value=_raw_batch("777", EVSE_STATE_WAITING_START)
    )
    switch = _make_switch(coordinator, client, account, device)
    client.charger_start_stop = AsyncMock()

    await switch.async_turn_on()

    assert switch.is_on is True
    client.charger_start_stop.assert_awaited_once_with("777", "fake-jwt", True)


async def test_turn_on_when_already_charging_is_noop(coordinator, client, account, device):
    """Регрессионный тест на баг: включение сразу после выключения не должно
    молча no-op'иться из-за устаревшего кэша координатора — здесь фикс проверяется
    от противного (свежий фетч тоже говорит Charging, значит no-op обоснован)."""
    coordinator.data["777"] = _status(EVSE_STATE_CHARGING)
    client.get_status_devices_batch = AsyncMock(
        return_value=_raw_batch("777", EVSE_STATE_CHARGING)
    )
    switch = _make_switch(coordinator, client, account, device)
    client.charger_start_stop = AsyncMock()

    await switch.async_turn_on()

    client.charger_start_stop.assert_not_awaited()


async def test_turn_on_ignores_stale_cached_charging_and_uses_fresh_state(
    coordinator, client, account, device
):
    """Регрессионный тест на реальный баг пользователя: кэш координатора ещё
    показывает Charging (устройство не успело отчитаться), но СВЕЖИЙ запрос
    говорит WaitingStart — включение должно пройти, а не молча no-op'иться."""
    coordinator.data["777"] = _status(EVSE_STATE_CHARGING)  # устаревший кэш
    client.get_status_devices_batch = AsyncMock(
        return_value=_raw_batch("777", EVSE_STATE_WAITING_START)  # свежие данные
    )
    switch = _make_switch(coordinator, client, account, device)
    client.charger_start_stop = AsyncMock()

    await switch.async_turn_on()

    client.charger_start_stop.assert_awaited_once_with("777", "fake-jwt", True)


async def test_turn_on_when_not_connected_raises_with_status_text(coordinator, client, account, device):
    from homeassistant.exceptions import HomeAssistantError

    coordinator.data["777"] = _status(EVSE_STATE_READY_TO_START)
    client.get_status_devices_batch = AsyncMock(
        return_value=_raw_batch("777", EVSE_STATE_READY_TO_START)
    )
    switch = _make_switch(coordinator, client, account, device)
    client.charger_start_stop = AsyncMock()

    with pytest.raises(HomeAssistantError, match="Свободна"):
        await switch.async_turn_on()

    client.charger_start_stop.assert_not_awaited()


# --- turn_off: разрешено только из Charging, no-op иначе ---


async def test_turn_off_from_charging_sends_command(coordinator, client, account, device):
    coordinator.data["777"] = _status(EVSE_STATE_CHARGING)
    client.get_status_devices_batch = AsyncMock(
        return_value=_raw_batch("777", EVSE_STATE_CHARGING)
    )
    switch = _make_switch(coordinator, client, account, device)
    client.charger_start_stop = AsyncMock()

    await switch.async_turn_off()

    assert switch.is_on is False
    client.charger_start_stop.assert_awaited_once_with("777", "fake-jwt", False)


async def test_turn_off_when_waiting_start_is_noop(coordinator, client, account, device):
    coordinator.data["777"] = _status(EVSE_STATE_WAITING_START)
    client.get_status_devices_batch = AsyncMock(
        return_value=_raw_batch("777", EVSE_STATE_WAITING_START)
    )
    switch = _make_switch(coordinator, client, account, device)
    client.charger_start_stop = AsyncMock()

    await switch.async_turn_off()

    client.charger_start_stop.assert_not_awaited()


async def test_failed_charger_command_reverts_and_refreshes(coordinator, client, account, device):
    from homeassistant.exceptions import HomeAssistantError

    coordinator.data["777"] = _status(EVSE_STATE_WAITING_START)
    client.get_status_devices_batch = AsyncMock(
        return_value=_raw_batch("777", EVSE_STATE_WAITING_START)
    )
    switch = _make_switch(coordinator, client, account, device)
    client.charger_start_stop = AsyncMock(
        side_effect=WaviotApiError(0, "charger command for 777 failed: FAILED")
    )

    with pytest.raises(HomeAssistantError):
        await switch.async_turn_on()

    assert switch.is_on is False
    coordinator.async_request_refresh.assert_awaited_once()


# --- WaviotChargerMaxPowerNumber ---


def test_number_native_value_from_status(coordinator, client, account, device):
    coordinator.data["777"] = _status(EVSE_STATE_CHARGING, max_power=6.0)
    number = _make_number(coordinator, client, account, device)
    assert number.native_value == 6.0


def test_number_max_ignores_floating_max_power_limit(coordinator, client, account, device):
    """Регрессионный тест: потолок слайдера не берётся из max_power_limit — то поле само "плавает" на платформе (подтверждено
    2026-09-08 на реальном устройстве: менялось с 15.5 на 7.6 без нашего участия),
    из-за чего диапазон визуально "сужался" вместо движения позиции ползунка."""
    coordinator.data["777"] = _status(EVSE_STATE_CHARGING, max_power_limit=9999)
    number = _make_number(coordinator, client, account, device)
    assert number.native_max_value == CHARGER_FALLBACK_MAX_POWER_KW


def test_number_max_comes_from_max_balancing_power(coordinator, client, account, device):
    """Как в приложении: потолок слайдера — max_balancing_power станции (у вас 7,68 кВт)."""
    coordinator.data["777"] = _status(EVSE_STATE_CHARGING, max_balancing_power=7.68)
    number = _make_number(coordinator, client, account, device)
    assert number.native_max_value == 7.68
    assert number.native_min_value == CHARGER_MIN_POWER_KW


def test_number_max_falls_back_when_balancing_power_missing_or_too_low(
    coordinator, client, account, device
):
    for value in (None, 0.0, CHARGER_MIN_POWER_KW - 0.1):
        coordinator.data["777"] = _status(EVSE_STATE_CHARGING, max_balancing_power=value)
        number = _make_number(coordinator, client, account, device)
        assert number.native_max_value == CHARGER_FALLBACK_MAX_POWER_KW


async def test_number_set_value_converts_and_calls_client(coordinator, client, account, device):
    number = _make_number(coordinator, client, account, device)
    client.charger_set_max_power = AsyncMock()

    await number.async_set_native_value(5.0)

    client.charger_set_max_power.assert_awaited_once_with("777", "fake-jwt", 5.0)


async def test_number_set_value_error_raises_and_refreshes(coordinator, client, account, device):
    from homeassistant.exceptions import HomeAssistantError

    number = _make_number(coordinator, client, account, device)
    client.charger_set_max_power = AsyncMock(side_effect=WaviotApiError(0, "failed"))

    with pytest.raises(HomeAssistantError):
        await number.async_set_native_value(5.0)

    coordinator.async_request_refresh.assert_awaited_once()
