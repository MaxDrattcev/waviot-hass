"""Кнопка «Обновить данные»: какие координаторы обновляются и что происходит по нажатию."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.waviot_home.button import WaviotRefreshButton, refresh_targets
from custom_components.waviot_home.const import DOMAIN
from custom_components.waviot_home.device import WaviotDeviceInfo


def _device(hw_type: str, modem_id: str = "5") -> WaviotDeviceInfo:
    device = WaviotDeviceInfo.from_api({"modem_id": modem_id, "hw_type": hw_type})
    assert device is not None
    return device


def _runtime(leak=True, charging=True) -> SimpleNamespace:
    return SimpleNamespace(
        main_coordinator=MagicMock(name="main"),
        leak_coordinator=MagicMock(name="leak") if leak else None,
        charging_coordinator=MagicMock(name="charging") if charging else None,
    )


def test_regular_device_refreshes_main_coordinator_only():
    runtime = _runtime()
    assert refresh_targets(runtime, _device("aqua2_stm")) == [runtime.main_coordinator]
    assert refresh_targets(runtime, _device("phobos3_wa_stm")) == [runtime.main_coordinator]


def test_leak_sensor_refreshes_main_and_leak_coordinators():
    runtime = _runtime()
    assert refresh_targets(runtime, _device("ladoga_stm")) == [
        runtime.main_coordinator,
        runtime.leak_coordinator,
    ]


def test_charger_refreshes_charging_coordinator_only():
    runtime = _runtime()
    assert refresh_targets(runtime, _device("wbox_nvt_wa_stm")) == [runtime.charging_coordinator]


def test_missing_optional_coordinators_are_skipped():
    runtime = _runtime(leak=False, charging=False)
    assert refresh_targets(runtime, _device("ladoga_stm")) == [runtime.main_coordinator]
    assert refresh_targets(runtime, _device("wbox_nvt_wa_stm")) == []


def test_button_identity_and_device():
    button = WaviotRefreshButton(_device("wbox_nvt_wa_stm", "9"), [])
    assert button.unique_id == "9_refresh"
    assert button.has_entity_name is True
    assert button.name == "Обновить данные"
    assert button.device_info["identifiers"] == {(DOMAIN, "9")}


async def test_press_requests_refresh_from_every_target_coordinator():
    first, second = MagicMock(), MagicMock()
    first.async_request_refresh = AsyncMock()
    second.async_request_refresh = AsyncMock()

    await WaviotRefreshButton(_device("ladoga_stm"), [first, second]).async_press()

    first.async_request_refresh.assert_awaited_once()
    second.async_request_refresh.assert_awaited_once()
