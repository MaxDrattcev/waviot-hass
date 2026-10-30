"""Тесты WaviotRelaySwitch — реальный Waviot API не вызывается нигде, только моки."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.waviot_home.account import WaviotAccount
from custom_components.waviot_home.api import WaviotApiError, WaviotClient
from custom_components.waviot_home.device import WaviotDeviceInfo
from custom_components.waviot_home.switch import WaviotRelaySwitch


@pytest.fixture
def device() -> WaviotDeviceInfo:
    return WaviotDeviceInfo(modem_id="123", hw_type="phobos", kind="phobos")


@pytest.fixture
def coordinator() -> MagicMock:
    coord = MagicMock()
    coord.data = {"123": {"on": False, "meter": 0, "voltage": 0, "power": 0, "amperage": 0}}
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


def _make_switch(coordinator, client, account, device) -> WaviotRelaySwitch:
    switch = WaviotRelaySwitch(coordinator, client, account, device)
    switch.async_write_ha_state = MagicMock()
    return switch


def test_is_on_reflects_coordinator_data_by_default(coordinator, client, account, device):
    switch = _make_switch(coordinator, client, account, device)
    assert switch.is_on is False


async def test_turn_on_sets_optimistic_state_immediately(coordinator, client, account, device):
    switch = _make_switch(coordinator, client, account, device)
    client.set_power_with_timer = AsyncMock()

    await switch.async_turn_on()

    assert switch.is_on is True
    client.set_power_with_timer.assert_awaited_once_with("123", "fake-jwt", True)


async def test_successful_command_does_not_force_refresh(coordinator, client, account, device):
    """Регрессионный тест на баг: устройство отчитывается на сервер Waviot редко
    (иногда раз в несколько часов), поэтому немедленный опрос после успешной команды
    почти гарантированно вернёт тот же устаревший кэш и затрёт результат команды —
    реле "включено" в кэше, хотя команда выключения только что подтверждена."""
    coordinator.data["123"]["on"] = True  # устаревший кэш, команда его ещё не догнала
    switch = _make_switch(coordinator, client, account, device)
    client.set_power_with_timer = AsyncMock()

    await switch.async_turn_off()

    assert switch.is_on is False  # оптимистичное значение НЕ затёрто устаревшим кэшем
    coordinator.async_request_refresh.assert_not_awaited()


async def test_turn_off_sets_optimistic_state_immediately(coordinator, client, account, device):
    coordinator.data["123"]["on"] = True
    switch = _make_switch(coordinator, client, account, device)
    client.set_power_with_timer = AsyncMock()

    await switch.async_turn_off()

    assert switch.is_on is False
    client.set_power_with_timer.assert_awaited_once_with("123", "fake-jwt", False)


async def test_optimistic_state_written_before_command_completes(coordinator, client, account, device):
    """Ключевое требование пользователя: UI должен показать "переключается" сразу,
    не дожидаясь ответа Waviot (до ~20 сек)."""
    switch = _make_switch(coordinator, client, account, device)
    seen_optimistic_before_command: bool | None = None

    async def slow_set_power(modem_id, jwt, turn_on):
        nonlocal seen_optimistic_before_command
        seen_optimistic_before_command = switch.is_on

    client.set_power_with_timer = AsyncMock(side_effect=slow_set_power)

    await switch.async_turn_on()

    assert seen_optimistic_before_command is True


async def test_failed_command_reverts_optimistic_state_and_raises(coordinator, client, account, device):
    from homeassistant.exceptions import HomeAssistantError

    switch = _make_switch(coordinator, client, account, device)
    client.set_power_with_timer = AsyncMock(
        side_effect=WaviotApiError(0, "downlink for device 123 failed: FAILED")
    )

    with pytest.raises(HomeAssistantError):
        await switch.async_turn_on()

    assert switch.is_on is False  # откатилось к данным координатора (on=False)
    coordinator.async_request_refresh.assert_awaited_once()


async def test_coordinator_update_clears_optimistic_state(coordinator, client, account, device):
    switch = _make_switch(coordinator, client, account, device)
    switch._optimistic_on = True

    switch._handle_coordinator_update()

    assert switch._optimistic_on is None
