"""Набор сущностей зарядки wbox: согласованность спецификаций с парсером и прежними unique_id."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

from custom_components.waviot_home.binary_sensor import (
    WBOX_BINARY_SENSORS,
    WaviotChargerBinarySensor,
)
from custom_components.waviot_home.const import DOMAIN, HW_TYPE_WBOX
from custom_components.waviot_home.device import WaviotDeviceInfo
from custom_components.waviot_home.parser import parse_device_status
from custom_components.waviot_home.sensor import WBOX_SENSORS, WaviotSpecSensor

# unique_id прежних сущностей зарядки — менять их ключи нельзя.
LEGACY_KEYS = {"power", "meter", "max_power_limit", "temperature", "evse_status",
               "max_power", "charger_switch"}


def _device() -> WaviotDeviceInfo:
    return WaviotDeviceInfo(modem_id="9", hw_type="wbox_nvt_wa_stm", kind="wbox")


def _status() -> dict:
    raw = {
        "common": {"time_saved": int(time.time())},
        "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD": {"state": 3},
    }
    return parse_device_status(HW_TYPE_WBOX, raw)


def test_spec_keys_are_unique_and_do_not_clash_with_legacy_entities():
    keys = [s.key for s in WBOX_SENSORS] + [s.key for s in WBOX_BINARY_SENSORS]
    assert len(keys) == len(set(keys))
    assert not set(keys) & LEGACY_KEYS


def test_every_spec_key_exists_in_parsed_status():
    """Опечатка в ключе спецификации дала бы вечно пустую сущность."""
    status = _status()
    session_keys = {"last_session_energy", "last_session_duration", "last_session_end"}
    for key in [s.key for s in WBOX_SENSORS] + [s.key for s in WBOX_BINARY_SENSORS]:
        assert key in status or key in session_keys, key


def test_sensors_use_entity_names_with_device_prefix_and_main_device():
    sensor = WaviotSpecSensor(MagicMock(), _device(), WBOX_SENSORS[0])
    assert sensor.has_entity_name is True
    assert sensor.device_info["identifiers"] == {(DOMAIN, "9")}
    assert sensor.unique_id == "9_duration"


def test_sensors_default_visibility():
    by_key = {s.key: s for s in WBOX_SENSORS}
    for key in ("last_session_energy", "last_session_duration"):
        assert by_key[key].enabled_default is True
    for key in ("duration", "nfc_cards", "temp_input", "pair_device"):
        assert by_key[key].enabled_default is False


def test_settings_with_controls_are_not_duplicated_as_sensors():
    """Настройку показывает управляющая сущность, повторять её сенсором не нужно."""
    sensor_keys = {s.key for s in WBOX_SENSORS} | {s.key for s in WBOX_BINARY_SENSORS}
    controlled = {
        "balance_enabled", "charge_power_limit_enabled", "charge_time_limit_enabled",
        "use_two_tariff_enabled", "schedule_start_enabled", "schedule_stop_enabled",
        "nfc_confirmation", "max_balancing_power", "max_charge_power", "max_charge_time",
        "hour_start_day_tariff", "hour_start_night_tariff", "schedule_start_time",
        "schedule_stop_time",
    }
    assert not sensor_keys & controlled


def test_binary_sensor_state_and_availability():
    coordinator = MagicMock()
    coordinator.data = {"9": {"cable_connected": True, "problem": None}}
    spec = {s.key: s for s in WBOX_BINARY_SENSORS}
    cable = WaviotChargerBinarySensor(coordinator, _device(), spec["cable_connected"])
    problem = WaviotChargerBinarySensor(coordinator, _device(), spec["problem"])
    assert cable.is_on is True
    assert problem.is_on is None  # нет данных — «неизвестно», а не «выкл»
    coordinator.data = {}
    assert cable.is_on is None


def test_remaining_binary_sensors_are_not_settings():
    by_key = {s.key: s for s in WBOX_BINARY_SENSORS}
    assert set(by_key) == {"cable_connected", "problem"}
    assert by_key["cable_connected"].enabled_default is True
    assert by_key["problem"].enabled_default is False


def test_legacy_session_energy_sensor_disabled_by_default():
    from custom_components.waviot_home.sensor import WaviotChargerEnergySensor

    sensor = WaviotChargerEnergySensor(MagicMock(), _device())
    assert sensor.entity_registry_enabled_default is False
    assert sensor.name == "Энергия сессии"


def test_session_durations_shown_in_minutes_by_default_but_stored_in_seconds():
    by_key = {s.key: s for s in WBOX_SENSORS}
    for key in ("duration", "last_session_duration"):
        spec = by_key[key]
        assert spec.unit == "s"  # облако отдаёт секунды, значение не конвертируем
        assert spec.suggested_unit == "min"
        sensor = WaviotSpecSensor(MagicMock(), _device(), spec)
        assert sensor.suggested_unit_of_measurement == "min"
        assert sensor.suggested_display_precision == 0
